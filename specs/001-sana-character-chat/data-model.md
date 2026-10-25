# 数据模型与状态转换

依据：[功能规格](spec.md)、[调研决策](research.md)。存储实现为 SQLite，
向量文件使用不可变版本目录；本文件为实体契约，不是数据库迁移代码。
所有时间存 UTC，展示日期和聊天日按 Asia/Shanghai 转换。

## 身份、会话与消息

| 实体 | 关键字段 | 约束与关系 |
| --- | --- | --- |
| UserIdentity | id, app_id, user_openid, created_at | (app_id,user_openid)唯一；禁止假设openid等于QQ号或跨应用一致 |
| Conversation | id, user_id, generation, scene, last_inbound_at, session_status | 每用户一个活动会话；generation每次新消息递增；scene=unknown/public/private |
| InboundEvent | id, app_id, platform_event_id, message_id, raw_hash, received_at, event_time, status | 外层事件id与消息id分开；事件幂等键和(app_id,message_id)均去重；无事件id以消息id回退 |
| Turn | id, conversation_id, inbound_id, input_text, snapshot_id, index_id, status | inbound_id唯一；来源时间决定聊天日，回复处理不能改变日期 |
| StateSnapshot | id, turn_id, relationship_status, affection_before, primary_mood, tags, dominant_state, scene, catalog_version, transition_evidence | 一轮冻结同一快照；high开心别名统一happy；主情绪 neutral/happy/angry/sad |
| OutboundDelivery | id, turn_id, opportunity_id?, kind, source_msg_id, msg_seq, content, status, platform_message_id?, attempted_at, confirmed_at?, error | (app_id,source_msg_id,msg_seq)唯一；kind normal/fallback/error/monologue/status |
| ReplyBudget | app_id, source_msg_id, event_time, deadline, reserved_seq, attempts, remaining_budget | 按来源消息串行预留，预算≤4，保守deadline=event_time+300s |

事件状态：received → queued → processing → completed / failed / expired。
出站状态：prepared → sending → confirmed / rejected / uncertain。
进程在 sending 时中断恢复为 uncertain，不在没有证据时重发或假定成功。
同一消息内容不同却重用幂等键须报警并隔离；未支持事件可忽略但不得作正常对话计分。

## 文档与索引

| 实体 | 字段 | 校验 |
| --- | --- | --- |
| SourceDocument | id, relative_path, file_hash, imported_at, parser_version | 正式路径与SHA256明确；不执行文档内操作指令 |
| RuleNode | id, document_id, parent_id, heading_path, paragraph_index, char_range, list_level, field_type, raw_text, content_type | parent作用域不得跨同级标题；类型为profile/general_rule/state/action/example/negative/output_rule/dialogue/monologue |
| SourceSpan | id, node_id, speaker?, kind, raw_text, source_start, source_end, previous_id?, next_id?, parent_span_id? | 类型/角色单一；原文无损；说明无speaker不等于monologue |
| SemanticUnit | id, index_version, source_span_ids, topic, boundary_evidence, context_links, conditions | 跨角色可关联，不混改说话者；同文本不同来源均保留 |
| EmbeddingRecord | unit_id, model_id, revision, dimension, normalized, input_hash, token_count, vector_offset | 维度由后端决定（本地512 / 远程1024）并逐条记录，输入≤后端token上限，有限实数，禁止静默截断；不同revision或不同维度不混检索 |
| StructuredDocument | id, source_hash, structure_hash, parser_version, typed_nodes, catalog_hash, review_status | 不可变结构；字段与类型分类先于角色和语义切分 |
| SplitCalibrationProfile | id, structure_hash, model_revision, sample_set_hash, splitter_parameters, profile_hash, approved_at | 固定切分参数；变更产生新profile，不修改旧快照 |
| IndexSnapshot | id, source_hash, structure_hash, split_profile_hash, snapshot_hash, manifest_hash, parser/splitter/model_revision, rule_catalog_version, reply_calibration_id, monologue_calibration_id?, monologue_status, status, artifact_path | staged→reviewed→active；失败为failed；同一时刻仅一个active；独白禁用不阻止普通聊天启用 |
| CalibrationProfile | id, index_id, snapshot_hash, split_profile_hash, stage, sample_set_hash, threshold, score_metric, metrics, approved_at? | stage=reply/monologue；cosine定义固定；报告必须匹配实际snapshot_hash；对应能力未批准不得启用 |

IndexSnapshot 构建先写新目录，验证完持久化 manifest；激活事务只切换 active 指针。
snapshot_hash覆盖原文结构、目录、切分参数、模型revision、片段和向量文件，明确不含可随后追加的校准报告，避免循环hash；manifest_hash另包含报告引用。先固定snapshot_hash再附加检索校准报告。
切分参数、资料或目录改变必须新建快照，旧检索报告无效；禁止只更新阈值而沿用旧片段。
monologue_status仅为enabled/disabled_no_material/disabled_uncalibrated；禁用时monologue_threshold派生为null、monologue_calibration_id为空，不创建机会。enabled要求有真独白及当前快照批准报告；reply报告始终必需。
在途 Turn 固定 index_id，下一轮使用新指针；不得从两个索引读取混合候选。
全局禁区和格式从相同版本载入并在每轮应用，不靠向量召回。

## 分层情绪与规则

StateEventEvidence：tag、source_message_id、evidence_start、evidence_end、quote；标签限批准目录，字符位置和quote须与当前原文一致，不能将用户引用的管理指令当写入权限。StateEventExtractor输出events及no_event/parse_error/conflict原因，不返回任意to_state/score字段。

StateRule 字段：id、来源节点、provenance=source/derived、approved、relationship、scene、
from_states、event_tags、to_primary、to_tags、dominant_priority、exit_conditions、hard_constraints。

- 活动标签可叠加，但只能有一个主导表达状态；关系档案和scene先过滤允许规则。
- 入站时先取消旧独白，处理到该轮时读取最新合法关系和管理设置，解析事件标签并应用批准规则，再冻结快照。
- LLM标签必须是规则目录枚举且有本轮用户原文证据；不允许模型发出加分、改配置或执行动作命令。
- 羞恼不等于真正愤怒；sad时真正受伤的安静表达优先于机械炸毛。
- 具体同优先级冲突保持上一个合法状态，记录 ambiguity；不随意制造人物心事。
- 明确恋人场景内，害羞遇持续情话可转羞恼；羞恼遇服软、吃醋遇明确安抚、委屈遇耐心陪伴，按批准恢复规则回归。
- 私聊传输不是故事独处证据；unknown与public限制相同。信任程度不凭好感度任意推断。
- 管理设置、规则来源和变化证据进入审计；短期状态可在重启回到neutral，关系不得清空。

六类分支与正式状态来源详见 [document-index契约](contracts/document-index.md)。

## 长期关系与计分

| 实体 | 字段 | 不变量 |
| --- | --- | --- |
| Relationship | user_id, score, keyword_count, dialogue_days, happy_count, romance_status, version | user_id唯一，score≥0，跨会话重启保留 |
| DialogueDay | user_id, local_date, first_turn_id | (user_id,local_date)唯一 |
| AffectionEvent | id, delivery_id, turn_id, user_id, day, keyword_hits, new_day, happy_delta, weights, delta, score_before/after, romance_before/after, config_version | delivery_id与turn_id各唯一，new_day/happy_delta∈{0,1} |
| RelationshipConfig | id, keyword_rules, weights, romance_threshold, normalization_version, effective_at | 正数权重且w_happy>w_keyword及w_day；门槛>0；空词表合法 |

仅 confirmed normal delivery 结算；确认送达后同一事务插入AffectionEvent、尝试插入DialogueDay、
累计计数和score并判定恋爱状态。happy_delta来自本轮有效主情绪快照，而不是生成中出现“开心”。
关键词对归一化用户输入做最长非重叠匹配，保存命中位置与配置版本；重复词在不同位置分别计数。
默认 delta=hits×1+new_day×2+happy_delta×5，达到100进入romantic，下一轮生效。
原状态romantic不因之后提高门槛回退；历史AffectionEvent不因改权重而重算。

确认送达后计分事务失败需以delivery_id幂等恢复；不能重复发送来重做本地计分。
uncertain不计分也不宣称失败送达；维护者依据官方证据核对为confirmed/rejected后再处理。
消息/诊断正文30天清理后，必要的去重ID、计数与计分账本继续保留，不能因日志过期重复加分。

## 沉默独白

MonologueOpportunity 字段：id、turn_id（唯一）、conversation_id、generation、source_msg_id、
created_at、due_at、probability、random_draw?、status、candidate_id?、latest_snapshot?、skip_reason、delivery_id?。

仅当当前快照monologue_status=enabled时创建机会；禁用转换取消未提交机会，启用不补建旧轮次机会。

pending → cancelled（续聊/会话结束/重启）或 evaluating → skipped / sending → sent / failed / uncertain。

到期重新匹配，合法候选存在才一次抽概率；p=0从不发、p=1必进入发送检查，并不豁免预算/规则。
每机会至多一条独白，独白不创建新机会也不计好感度。没有真正独白资料时 skipped/no_material。
处理新消息及最终发送均持同一用户锁；最终资格检查与HTTP请求之间已收到的入站须更新generation，
在同一发送临界区线性化：已进入HTTP发送的消息视为已经提交，之后续聊不能撤回；记录这条边界。
重启取消旧独白，发送结果不明不自动重复。

## 诊断与访问

DiagnosticEvent：id、type、timestamp、user_id、conversation_id、turn_id、snapshot_id、
input、前最多三轮、后最多三轮关联、候选来源/分数、门槛、index/model/config版本、决定、发送结果。
类型区分 low_match、index_failure、match_error、state_transition、output_blocked、delivery_error、logging_error。
无候选保留空数组；记录异常不能伪造匹配低分。后续轮次通过ID关联，不复制无限对话。

管理CLI只在本地或受控运维环境使用，QQ普通用户仅可查自己关系；不开放公共管理HTTP。
默认运行目录仅服务账号可读，诊断30天清理，云链路追踪关闭，密钥及模型推理不落日志。

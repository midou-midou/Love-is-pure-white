# 分层角色资料数据模型

## 存储与版本原则

沿用现有 SQLite 与不可变快照。已有 `0011 structural_index` 迁移中的 structural_*、regression_report、activation_audit、retrieval_evidence 是预留能力，必须补齐应用读写。新增迁移使用下一可用版本号，不能重写已执行的 0011。旧 semantic_v1 的结构、批准、校准及向量记录继续可读。

内容字段哈希使用规范 JSON（键排序、UTF-8、稳定列表顺序）；原文件以原始字节单独 SHA256。排除 created_at、运行 ID、reviewer 时间等非内容属性。相同卡 ID 在不同包版本可复用，在同一包内必须唯一。

## CharacterPackage

- `package_id`：构建运行标识。
- `format_version=structural_v2`、`chunking_strategy=markdown_cards_v1`。
- `character=sana`、`story_stage`、`required_relationship=romantic`。
- `source_manifest`：六份 included 内容文件及明确 excluded 的 00/07；每项含相对路径、用途和原始字节哈希。
- `source_set_hash`：included 文件清单与其字节哈希；excluded 文件修改不应改变生成内容，但检查记录需说明实际版本。
- `structure_hash`：解析后的常驻、26 卡及来源定位；`profile_hash`：解析与容量策略。
- `policy_hash`：逐卡条件、选择顺序、门槛判定方法和预算配置（数值阈值保存在快照绑定的校准报告）；`catalog_hash`：批准的状态目录。
- `encoder_config_hash`：kind、model、revision、dimension、输入上限、tokenizer 标识与 revision、查询前缀、编码文本格式。
- `snapshot_hash`：来源集合、结构、profile、policy、catalog、encoder 及向量内容哈希。
- `bundle_hash`：snapshot_hash 与封存运行文件的有序路径和哈希；不含后加验收报告。
- `monologue_status=disabled_no_material`，独白报告与阈值为空。
- `status`：快照沿用 staged/reviewed/active/failed。解析 pending 和构建 building 是任务状态，不写入快照枚举；成功封存才登记 staged，失败保留原因。

落地：复用 index_snapshot 的格式及绑定字段，扩展 SnapshotRecord 的缺失字段。独立 `package_source` 关联结构版本与各 source_document；`structural_document.source_id` 可指向显式包清单的 SourceDocument，不能替代六份来源关联。新增 `package_resident` 保存常驻全文、定位与顺序。

## SourceFileVersion

- `source_id`、包内路径、原始字节哈希、封存文件路径、原始正文。
- `role`：core/output/emotion/scene/background/example/maintenance。
- `included`；excluded 原因；原始来源说明。
- `source_ranges`：文件标识、H1/H2 标题路径、1-based 行首尾、Unicode 码点的半开字符范围。

原始正文不作 Unicode 归一化后再用于定位。规范化仅用于另建哈希输入或检索，不覆盖原文。禁止路径逃逸或符号链接指向清单根目录外，不自动扩展文件清单。

## ResidentMaterial

- `resident_id`：`sana_core`、`sana_output`、`sana_emotion_general`。
- `ordinal`：核心、输出、状态总则顺序。
- `raw_text`、来源、内容哈希、必要性、生成 token 计数。

01 和 02 是完整正文常驻；03 的总则也是 26 卡之一，但只常驻、不重复编码。首次导入必须正好识别上述三个来源；后续内容增加通过新版本批准。

## ReferenceCard

- `card_id`：文档 id；`type` 为 emotion_rule/scene_rule/background/appearance/profile/dialogue_example。
- `character`、`heading_path`、`source_label`、`scope_raw`、`raw_text`、`source_ranges`。
- `provenance`：source/adapted/generated，示例不是已发生事件。
- `effective_conditions`：批准的关系、场景、媒介、情绪标签与状态守卫。
- `constraint_refs`：共享输出和状态规则；`speaker_turns`：示例说话者、原始文本及位置。
- `embedding_input`：稳定标题、类型、scope 与完整正文；`input_hash`、精确 token_count。

复用 structural_group、structural_field、structural_leaf：一个卡一个 group，完整 body 一个 field，一个 leaf，`token_split=0`、offset 为完整正文范围、eligible_stages 仅 reply。type 元数据不推断心理独白；speaker_turns 保留示例用户与角色，续行沿用前一说话者。

## ApprovedPackagePolicy

- `profile_version`、`card_conditions`（按 card_id 显式映射）、`state_catalog`、`budget`、`retrieval_policy`、批准者和批准时间。
- 条件值必须合法：scene=any/public/private，medium=any/text/co_present，relationship=any/romantic/non_romantic；unknown 为运行时值，不能当 any 或 public。
- 每卡元数据与批准映射必须一致，scope 冲突或缺映射阻止批准。公共可收敛使用的情绪卡设 any，具体亲近行为由常驻约束限制；整卡仅独处的 hurt/secure 设 private。
- 运行时不让模型新建、改写或批准策略；用户消息不能修改 profile。

状态目录继续四个主心情，使用二级标签表达 calm/shy/flustered/jealous_light/jealous_medium/jealous_heavy/hurt/secure，转换条目包含 from_states、事件证据、关系、场景、恢复出口。所有状态标识映射由批准目录决定；不能由相似分数直接设值。

## FrozenTurnContext

- `turn_id`、`user_id`、`conversation_id`、generation。
- `index_id`、snapshot_hash、bundle_hash、policy_hash、catalog_hash、calibration_id/report_hash、resident refs。
- `relationship_status`、scene、medium、location/cohabitation/nickname 的已确认值或 unknown。
- `previous_state`、effective_state、transition_evidence（关联消息标识及可核对引用）。
- 当前用户可用事实只引用既有会话数据，不引入全局共享用户偏好表；缺值保持未知。

冻结发生于读取活动包后、状态判断前。已有 MaterialAvailability、StateSnapshot 扩展版本引用；解析历史旧快照缺少新字段时只允许旧格式路径，不伪造新包绑定。

## SelectionEvidence

- 本轮版本与查询范围；每张候选的卡 ID、类型、条件是否满足、相关程度、选中与排除原因。
- 状态卡与场景卡的规则选择证据，背景和示例的排序依据。
- 歧义、预算排除、核心缺失、能力禁用或错误原因；不保存原始密钥或完整系统提示。

复用 retrieval_evidence，补所需字段或版本化 evidence payload；关联现有 DiagnosticEvent，遵守原有正文保留期。没有候选时保存空列表，不伪造分数。

## EvaluationReport 与 ActivationAudit

- 报告绑定 index_id、snapshot_hash、bundle_hash、profile/policy/catalog/encoder hashes、固定样本哈希、各 SC 结果、评审证据引用、execution_mode=offline/live，另列实际替代依赖清单、批准者。
- 结构报告、检索校准、完整回归分别记录；generation live 报告必须实际产生逐轮结果和评审，不能仅登记通过标记。
- 审计记录 activate/rollback、expected_active、previous/target、绑定哈希、验证报告引用、维护者、原因与结果。

复用 structural_calibration_report、regression_report、activation_audit；以新迁移将预留的每 index/stage 唯一约束改为版本化报告唯一约束，允许不可变报告修订，并在激活记录中绑定所批准的 report_id/hash；活动包不得就地切换报告。向后兼容读取旧报告，但旧报告不能用于启用新格式。

## 状态转换与不变量

1. parse → pending；验证错误保留诊断且不能批准。
2. approve：固定结构、profile、policy、catalog；修改任一内容产生新版本。
3. build：外部编码不在写事务中；临时产物完整写入并复核后封存，再登记 staged。
4. calibrate/evaluate：对封存版本执行，结果不改变 snapshot_hash；全部必要门槛通过后 reviewed。
5. activate：先验证封存产物及报告，再短事务比较 expected_active、保证唯一 active、更新状态及审计。
6. rollback：目标旧版本也重新验证，禁止绕过报告或完整性；新旧包均不写用户关系与消息业务表。
7. 在途轮次持有旧完整引用直到完成；旧产物未被在途轮次释放前不得回收，本功能不提供自动删除旧快照。

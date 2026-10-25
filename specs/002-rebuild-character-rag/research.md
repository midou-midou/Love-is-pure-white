# 分层角色资料索引研究

查阅日期：2026-10-25。本文件记录设计决策，不表示索引已经重建或真实服务已验证。

## 1 完整卡格式与既有结构表

**Decision**: 使用 `structural_v2` 与 `markdown_cards_v1`；保留 `semantic_v1`。补齐新格式仓储、读取、校准、激活和恢复分发。

**Rationale**: `src/sana_bot/ingest/service.py` 现只从 DOCX 进入语义切分，`cli_index.py` 构建强制 split profile。`storage/index_schema.py` 已有 structural_document/group/field/leaf/embedding 等表，但 `index_repository.validate_snapshot_format` 和 `retrieval/vector_store.py` 仍只认可旧格式。不能直接调用旧构建流程假装完成新包。

**Alternatives considered**: 继续按相邻句向量切分会破坏完整恢复机制；新增第三套平行索引表重复已有结构。卡片映射一 group、一正文 field、一完整 leaf，常驻则使用独立表，不人为创建语义 split profile。

## 2 多文件来源与确定性

**Decision**: 显式文件清单声明 01～06；00/07 标记 excluded。每份文件独立 source_document 记录，包外层可有 manifest 来源，但必须保留文件关联。内容哈希和卡 ID 稳定，运行 ID 与时间不纳入内容一致性判断。

**Rationale**: 现 structural_document 仅单 source_id；不能用拼接文本抹掉多文件来源。记录文件路径、字节哈希、H2 标题、行范围、字符范围和原 source 文本；新增示例明确 generated/adapted。

**Alternatives considered**: 自动遍历目录会使维护记录被误加载；仅保存绝对路径无法说明建立版本时读取了哪份内容。

## 3 向量容量与生成预算

**Decision**: 25 张完整可选卡分别编码，不编码三份常驻材料。实际编码文本包含标题、类型、scope 与完整正文；以固定 tokenizer 精确计算包含特殊 token 的真实输入，计数器与模型/revision 绑定。远程默认启发式计数不得作为正式容量证据；应配置相符的本地 tokenizer 或可核对的提供方逐条计数。验证不足则不可启用。

**Rationale**: `retrieval/embeddings.py` 默认远程 `HeuristicTokenCounter`，本地可提取 tokenizer，默认限制 512。提供方返回批次 usage 不足以证明每卡未截断。[Sentence Transformers 官方说明](https://www.sbert.net/examples/sentence_transformer/applications/computing-embeddings/README.html) 指出超长输入会被截断，且不能把长度随意提高到模型支持范围之外，因此必须在编码前验证。

**Alternatives considered**: 自动拆卡违反规格，自动摘要丢条件，提高配置数值不证明模型支持。若完整卡超限，报告卡 ID、实际计数与限制，维护者明确选择更大容量编码器或调整来源后重建；不静默改变模型。

生成预算以所选聊天模型容量验证，完整常驻和状态规则不可裁剪；按排序整张移除低优先背景、示例，并记录原因。固定最近六轮历史为上限，预算不足时按完整旧轮次缩短，保留本轮及状态证据。必要规则和本轮本身超限则受控失败。查询只采用本轮及最近三条用户输入和状态摘要；超限时逐条去除较早完整输入，本轮本身超限则报错，禁止静默按字符裁剪。

## 4 条件映射与状态

**Decision**: 配置中以卡 ID 显式声明 relationship、scene、medium、state_tags、依赖和互斥关系；核对后批准冻结，不在运行时从 scope 自由猜测。复用现有状态 reducer 和受约束分析，加入版本绑定的六状态与吃醋三级映射；不新增主心情枚举或重新定义好感算法。

**Rationale**: scope 是自然语言，“公共场合收敛”不能简单等同整张卡禁止公共使用。完整卡可在公共使用，但亲近动作仍受常驻场景规则限制。`state/models.py` 只有四主情绪及 secondary_tags，足以表达新增二级状态。`StateService` 目录当前来自可变实例，必须改为本轮固定版本读取，避免切换期间规则串版本。

**Alternatives considered**: 仅当前消息关键词无法判断长期冷落与安抚，向量分数也不能决定关系条件。已确认情绪不随一次命中就覆盖，歧义时保留保守表达并记录候选及触发证据。只读检查另发现 `state/event_extractor.py` 当前仅接当轮文本，`state/reducer.py` 未执行目录已有的 from_states 和 exit_conditions；本次须补最近上下文证据与前态／出口守卫。`retrieval/filters.py` 当前将 UNKNOWN 视作可满足 public，需区分明确公共与未知保守表达。

## 5 常驻规则与空检索

**Decision**: 新格式通过额外规则接口注入完整常驻与适用行为卡；背景、示例为有类型引用。对成功空检索或低相关新包允许生成自然回复，编码、生成或缺资料属于错误，不冒充空检索。旧格式保持旧规则以支持恢复。

**Rationale**: `dialogue/prompts.py` 有 extra_rules 入口但角色基线仍硬编码；`dialogue/service.py` 无检索直接生成路径会绕过新规格要求，需阻止；match_decision 空候选/低分现统一兜底，需按格式分发。

**Alternatives considered**: 把规则都当普通引用会令其失去约束地位；检索不到台词就表示不理解使问候失败。新包不是事实问答数据库，已知事实之外的精确问题应澄清而非编造。

关系不匹配直接受控失败，诊断 `package_relationship_mismatch`，不输出恋人包中的恋人身份，也不修改用户 relationship。事实记忆只读既有当前用户记录，不新增长期记忆抽取系统。

## 6 版本与验收

**Decision**: sealed bundle 绑定来源、常驻、完整卡、profile、policy、catalog、encoder 和向量；校准与行为报告绑定 bundle 和固定样本。policy_hash 不含本次校准数值，数值阈值及其 report_hash 在封存快照上另行登记，激活与每轮固定报告引用，避免阈值与快照形成哈希循环。每轮固定上下文引用，activate/rollback 采用同一验证器及 compare-and-swap。

**Rationale**: 现激活主要验证旧结构与阈值，未有新包回归准入；VectorStore 需要重新核对文件哈希与绑定，不能只相信 manifest 自称的 snapshot_hash。目录与资料更改均使旧报告失效。

**Alternatives considered**: 覆盖旧目录或直接更新活动向量无法恢复；复用旧阈值报告不能验证变化后的条件筛选与空检索行为。

## 7 QQ 约束与验证边界

**Decision**: 仅沿用 C2C 文本被动回复，不新增主动消息或独白。保留现有每来源消息最多 4 次、5 分钟的保守限制，未利用本轮查阅到的更宽时效扩大能力。

**Rationale**: 2026-10-25 查阅 [官方消息收发概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)：被动回复携带 msg_id、单聊文本 msg_type=0、重复 msg_id+msg_seq 会失败；页面列出单聊 60 分钟、4 次。旧 [腾讯官方仓库发送文档](https://github.com/tencent-connect/bot-docs/blob/main/docs/develop/api-v2/server-inter/message/send-receive/send.md) 仍有不同条数和主动消息停用说明，应优先对应当前平台页面并保留保守实现。本功能不据此改平台预算。

**Alternatives considered**: 把角色主动性直接变成后台主动发消息超出范围且涉及权限。本轮未读取应用后台，无法确认应用当前授权；实施前核对 C2C 文本权限、沙箱用户、回调地址、验签和发送资格，发布前复查官方规定。缺权限明确记录未执行。

## Research closure

未知技术选择已解决。尚未执行真实 tokenizer 逐卡容量检查、生成结果评审、QQ 授权核对和沙箱联调，均列入实施门槛。当前 Python 版本、锁文件及入口文件来自本地只读检查，不是本轮测试通过声明。

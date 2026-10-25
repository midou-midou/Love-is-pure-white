# 技术调研与决策

查阅日期：2026-10-16。输入为 [spec.md](spec.md)、宪章 v1.1.0 与正式人物设定。
本文是规划决策，尚未安装依赖、调用真实模型或连接 QQ 沙箱。

## 1. Python 与依赖

**Decision**: Python 3.12，项目约束 `>=3.12,<3.13`；uv 创建 `.venv`，以 `uv.lock` 固定解析结果。
候选范围为 `langchain>=1,<2`、`langchain-deepseek>=1.1.1,<2`、
`langchain-huggingface>=1.2.3,<2`、`sentence-transformers>=5.2,<6`；其余选 FastAPI、
Uvicorn、HTTPX、Pydantic 2、cryptography、python-docx、NumPy、pytest、pytest-asyncio、respx。
不在规划阶段虚构已验证的精确锁版本；实施首步解析并验证导入、异步聊天和本地编码。

**Rationale**: 将平台、聊天与向量能力分开，固定单一 Python 次版本以减少复现差异。

**Alternatives considered**: 系统 Python/全局 pip 不满足宪章；直接使用模型 SDK 代替 LangChain 不满足框架要求。

依据：[uv 项目管理](https://docs.astral.sh/uv/guides/projects/)、
[DeepSeek 包声明](https://github.com/langchain-ai/langchain/blob/master/libs/partners/deepseek/pyproject.toml)、
[Hugging Face 包声明](https://github.com/langchain-ai/langchain/blob/master/libs/partners/huggingface/pyproject.toml)。

## 2. 对话模型

**Decision**: LangChain `ChatDeepSeek`，官方 API 地址 `https://api.deepseek.com`；
模型由 `DEEPSEEK_MODEL` 配置，初始选 `deepseek-flash`，可切换 `deepseek-v4-pro`。
只发送校验后的文本 content，不对 QQ 透传推理内容、工具输出或流式草稿。
状态判断和输出语义检查采用受约束 JSON 结果，Pydantic 校验失败则拒绝该判断；
首期不依赖厂商强制 JSON Schema 支持，也不引入 Agent 工具执行。

**Rationale**: 当前厂商文档与 LangChain 老示例模型名存在代际差异，采用厂商现行模型名；
聊天模型不承担向量编码。温度、超时和模型名进入版本化运行配置。

**Alternatives considered**: 硬编码旧模型名容易失效；直接将模型自述的状态/分数写入数据库无法约束权限。

依据：[DeepSeek 当前接入](https://api-docs.deepseek.com/)、
[ChatDeepSeek 集成](https://docs.langchain.com/oss/python/integrations/chat/deepseek)。

## 3. 中文语义切分与索引

**Decision**: 向量编码走可替换的抽象层，两种后端由 `EMBEDDING_KIND` 选择：
`local`（默认）用 `HuggingFaceEmbeddings` 本地编码 `BAAI/bge-small-zh-v1.5`，归一化 512 维；
`openai_compatible` 用 OpenAI 兼容的远程向量 API（当前配置 `BAAI/bge-large-zh-v1.5` @ 硅基流动），归一化 1024 维。
维度与 token 上限进入 `EmbeddingSpec`，每条 `EmbeddingRecord` 记录实际维度，编码前拒绝截断：
本地按实际 tokenizer 计数，远程依赖服务的超长输入拒绝（实测 `bge-large-zh-v1.5` 返回 HTTP 400，而非静默截断）。

**DeepSeek 不提供向量接口**：`GET /models` 仅返回 `deepseek-flash`、`deepseek-v4-pro` 两个聊天模型，
`POST /embeddings` 与 `/v1/embeddings` 均返回 404，官方文档只列 chat/completions。
因此“对话用 DeepSeek、向量用 DeepSeek 向量模型”不可行，向量必须另选提供方。

首次准备（本地）下载记录精确模型 revision，校准与启用均使用同一 revision，不使用可变 main 作为已验收版本。
远程后端的 `revision` 记录为 `model@host`（实测返回的 `model` 字段与请求 id 一致，不带版本后缀），
使不同提供方或不同主机生成的向量不会混用。

正文先按明确标题/场景/角色/字段边界组织，按完整句进行临时窗口编码，比较相邻窗口余弦差异提议
语义边界；超长句必要时在标点和 token 边界再分，记录 parent 与 offset。语义段目标不超过 384 token，
编码输入（含检索指令）始终不超过当前后端的 token 上限（本地 512 / 远程按模型上限，配置为 512）。
跨角色问答用语义单元关联，不能改写片段身份。

单份文档初版使用 SQLite 元数据与文件系统 `.npy` 矩阵进行精确相似度检索，无专用向量数据库。
先按关系、场景、角色与状态过滤合法候选，再取最多 8 个语义单元，包含必要前后关联。
通用设定、禁区和输出规则每轮固定加载，不参加候选最高分计算，避免“全局规则很相似”掩盖低匹配。

**Rationale**: 当前资料规模小，精确检索便于回溯；Embedding 必须参与边界判断，不能只在固定切块后编码。

**Alternatives considered**: 仅字数切块不满足需求；仅按角色聚合丢失情景；外部向量数据库首期增加不必要服务。
远程 API 与本地模型的取舍：远程免去本地模型下载与 CPU 编码开销，但引入凭据、网络与提供方可用性依赖；
两者共用同一抽象层，切换只改配置，不改变索引与校准语义（但维度和 revision 变化必须重建索引并重新校准）。

依据：[本地模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5)、
[本地模型配置](https://huggingface.co/BAAI/bge-small-zh-v1.5/raw/main/config.json)、
[硅基流动向量模型列表](https://docs.siliconflow.cn/cn/userguide/capabilities/embedding)、
[DeepSeek API 文档（无 embeddings 端点）](https://api-docs.deepseek.com/)。

## 4. 门槛与规则确认

**Decision**: 切分边界阈值、即时回复门槛、独白门槛独立版本化；在确认的正负样本上校准，
选择满足 SC-003/009/013 的参数；相等为有效，不将余弦相似度称为概率。不设置未经校准的通用 0.5。
生成报告记录样本集 hash、模型 revision、参数、混淆矩阵和误差案例。先解析不可变结构，再校准切分参数，固定参数重建向量快照，最后在该快照上校准回复/独白门槛；未经对应能力校准不启用该能力。切分参数改变必须重建，旧检索门槛报告失效。检索报告绑定snapshot_hash；该hash覆盖索引内容而不含后加报告，manifest另记报告引用，激活时拒绝hash不一致。
测试可用固定向量和显式门槛验证分支，真实质量验收必须使用实际模型。

**Rationale**: 模型卡指出绝对分值不能作为通用判定依据。

**Alternatives considered**: 用模型生成的“信心百分比”替代可复现分数无法审计；调切分阈值同时改变回复门槛难以归因。

无合法独白资料时 monologue_status=disabled_no_material、monologue_threshold=null，普通聊天可在自身验收通过后启用，不创建独白机会。有独白但未校准为disabled_uncalibrated；enabled必须有合法素材及匹配当前快照的批准报告。补资料生成新版本，不修改活动快照。

## 5. 正式 DOCX 的结构与状态

**Decision**: python-docx 配合只读 OOXML 定位，识别可见编号标题、段落及 numPr，不能只靠 Heading 样式。
标题 → 正文 → 列表/冒号字段 → 内容类型/说话角色 → 语义切分，顺序不可颠倒。
对“触发条件/表现/肢体细节/行为表现/说话风格/消退逻辑/维持”等字段使用白名单语法；
“你：/纱凪：”识别为示例轮次，喜欢/讨厌为属性，禁区例句为 negative_example。
全部源段落映射到至少一个有类型节点，不署名说明不能当独白。

原文件 SHA256：`12fdf78e552bbb54b2bfabb0af20b6c9bfa1c4f64e92905efb0dbf960e90032c`。
含八个主章节和补充约束。第四章恋人状态有明确依据；非恋爱状态及主情绪映射是产品推导。
导入生成待审 catalog，维护者通过本地命令核对并批准；未批准分支不作为动态转换规则。
六类分支不齐时不宣称完整验收通过，继续使用已确认通用性格约束，不套用恋人专属状态。

StateEventExtractor用LangChain/DeepSeek输出有限标签、source_message_id及原文字符证据，Pydantic验证标签在已批准目录中且证据能回定位；解析失败/无证据返回no_event，冲突由reducer保持合法旧态，禁止输出任意状态写入和计分指令。模型只提议有限枚举的事件标签，确定性 reducer 核对已批准转换的来源、关系及场景；
没有证据不切状态。先应用本轮合法转换再冻结快照，回复和开心计分共用快照。
私聊通道不等于故事独处场景；场景未知时采用 public 的保守表达，不推断肢体接触事实。

**Rationale**: 避免把文档写给角色的约束当作代理操作命令或可发送台词。

**Alternatives considered**: 模型自由解析整个文档难以保证负例归属；让模型直接修改好感度越权。

来源：[正式设定](../../doc/乾纱凪角色设定.docx)，尤其第四、五、七、八章及补充约束。

## 6. QQ 官方接入与消息限制

**Decision**: 首期 QQ 单聊 C2C，HTTPS Webhook + 薄 HTTP 适配器，不模拟个人 QQ。
订阅 `GROUP_AND_C2C_EVENT (1 << 25)`，处理 `C2C_MESSAGE_CREATE`，用户取
`author.user_openid`，被动回复使用事件体消息 `d.id`，不是外层事件 ID。
正式 API `POST https://api.bot.qq.com/v2/users/{user_openid}/messages`，纯文本 `msg_type=0`。

鉴权通过 AppID/AppSecret 获取 access token，读取 expires_in 并缓存更新；同时检查 HTTP 状态和业务错误。
Webhook 读取原始 body，用官方 Ed25519 算法验证签名；op=13 按官方挑战应答。
先验签、持久化和去重再迅速 ACK，模型调用由队列处理，不阻塞回调。
首期可用官方 HTTP 契约自行实现薄适配器；botpy 与 WebSocket 为备选，不宣称官方 WS 已停用。

官方在消息概述和发送接口概述写 C2C 被动回复 60 分钟、每条用户消息最多 4 次，
但同页 msg_id 参数仍写 5 分钟。**保守默认以用户消息时间 + 5 分钟为截止，预留 10 秒发送余量**，
且不超过四次预算；正常回复和独白默认各用一条，msg_seq 为同一 msg_id 下稳定递增序号。
独白 60 秒从正常回复确认成功开始计算，发前同时核对上述事件时间窗口；绝不改成无授权主动推送。
官方拒收、额度/权限不足、超时或预算不足时终止并记录。未来扩大窗口需新官方依据及沙箱验证。

**Rationale**: 保守交集处理官方文档冲突，维持默认独白的可实现性，同时不作超出平台能力的承诺。

**Alternatives considered**: 非官方协议违反宪章；独白定时到点直接发忽略发送资格；直接采用 60 分钟可能碰到参数限制。

查阅于 2026-10-16，依据：

- [官方接入入口](https://bot.q.qq.com/wiki/develop/api-v2/)
- [C2C 事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html)
- [消息概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)
- [发送单聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)
- [鉴权凭证](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html)
- [事件投递](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)
- [签名](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html)

## 7. 持久化、幂等与延迟任务

**Decision**: 单实例 SQLite WAL、事务、唯一约束、每用户串行队列；NumPy 向量读入内存。
入站事件先落盘；出站保存内容、来源 msg_id、固定 msg_seq 和状态。成功发送后，
在同一事务中结算好感度、插入当日去重记录、更新关系档案及记录结算事件。
网络发送和本地提交无法形成跨系统事务：不承诺绝对 exactly-once。
发送结果不明标记 uncertain，保留同一序号与核对记录，禁止盲目更换序号重发或直接加分。
明确失败不计分；确认送达但结算暂失败可按 delivery id 重做本地事务，唯一约束防重复。

独白机会在正常送达后创建，保存 conversation generation；新用户消息到达即递增 generation，
取消待发机会。发送前在同一用户锁内核对 generation、最新状态与预算。重启取消旧独白，
普通回复任务仅在窗口内恢复，超过窗口标过期。长事务不包住模型/网络调用。

**Alternatives considered**: 内存计数无法重启保存；Redis/Celery 首期运维过重；对网络重试声称端到端一次发送不诚实。

## 8. 输出约束、日志与发布前条件

**Decision**: 全局规则固定加载；规则化校验拦截句末中文/英文句号、禁句、第三人称自称、
引导句式和用户台词格式，另用受约束语义审核检查用户心理猜测、恶意与关系越界。
最多修订一次；审核异常拒发草稿，转预审疑惑兜底；独白不合规则静默。
输出长度默认最多 160 中文字符、3 个短句，动作神态按场景自然选择，非每句强塞动作。
数字/英文中的点不是句末句号；省略号允许。所有待发正文通过统一最终验证路径。
规则测试覆盖已知禁区，不能把有限测试声称为对开放生成的数学保证。

低匹配、状态切换、输出拦截、发送失败和好感度事件分别分类；日志记录有效版本，禁写凭据、
完整 prompt 或模型推理。默认诊断保留 30 天，最多三轮前后三文，服务启动定期清理；
关系累计与结算必要去重信息长期保留，聊天正文清理不能导致重复计分。云追踪默认关闭。

发布前外部条件：QQ 应用与沙箱授权、HTTPS 回调、模型凭据、确认的六类状态映射、
正式词表或明确采用空表、Embedding revision 与校准报告。缺条件可做离线实现，不能声称真实验收完成。

**Alternatives considered**: 仅提示词无法确保发送前约束；无限自我修订增加延迟并耗尽回复窗口。

## 调研结论

技术选择已作出，无未决技术澄清。待核对状态映射与账号权限是明确的启用/验收门槛，
不是假定已取得的能力。现有规格允许的文本动作、动态状态及长期关系均纳入设计；
索引建设、消息服务和任何账号操作尚未执行。

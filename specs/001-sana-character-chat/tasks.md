---
description: "乾纱凪 QQ 角色聊天实施任务"
---

# Tasks: 乾纱凪资料驱动与心情感知聊天

**Input**: [plan.md](plan.md)、[spec.md](spec.md)、[research.md](research.md)、[data-model.md](data-model.md)、[contracts/](contracts/)、[quickstart.md](quickstart.md)。

**Prerequisites**: 设计完成，宪章 v1.1.0；当前应用未实现，所有任务初始未完成。

**Tests**: 宪章要求聊天链路自动化与失败路径验证，规格 SC-001–021 明确验收目标，因此包含必要的契约、集成与领域测试。测试任务在对应实现之前；编写时确认测试能识别缺失行为，不用空断言或强制失败占位替代。真实服务测试单列，离线测试不依赖付费调用。

**Organization**: 原故事编号不变。同优先级 P1 按结构分类/资料切分→规则→基础聊天→语境→兜底排序，P2 按心情操作→长期关系→延迟独白排序。依赖详见文末，不将文档排列误当“所有故事互不依赖”。

## Format: `[ID] [P?] [Story] Description`

任务使用 `- [ ] Tnnn [P] [USn] 描述与路径`；[P]仅表示在列明先决任务完成后，与同一并行组的不同文件任务可同时做。故事阶段必须带[USn]，基础与收尾不带。

## Path Conventions

以下路径均相对仓库根目录。源代码为 `src/sana_bot/`，测试为 `tests/`，非敏感规则为 `config/`，
不可提交运行产物为 `runtime/`。实现不得修改正式 DOCX、开发Live2D或执行人物动作。
运行操作在仓库根目录执行；本清单只定义工作，不表示命令已经存在。

## Phase 1: Setup (Shared Infrastructure)

**Goal**: 建立可重建的 Python 3.12 项目，不连接任何账号。

**Independent Test**: uv 锁定安装、包导入、CLI --help 成功，环境及运行数据不进入版本控制。

- [x] T001 在 pyproject.toml、.python-version、src/sana_bot/__init__.py 定义 Python `>=3.12,<3.13`、包结构及 sana-bot CLI 入口；保留 doc/ 与 live2d/，不创建其他平台入口

- [x] T002 在 pyproject.toml、uv.lock 锁定 research.md 的 LangChain/DeepSeek/HuggingFace、FastAPI、HTTPX、cryptography、python-docx、NumPy 和 dev 测试依赖；用 uv 创建 .venv 并验证真实导入；不把未解析版本宣称兼容

- [x] T003 [P] 在 .gitignore、.env.example 声明 .venv、runtime、模型缓存、真实凭据与对话数据不提交，示例密钥留空并默认关闭云追踪

- [x] T004 [P] 在 tests/conftest.py、pyproject.toml 配置独立临时存储、fake clock/RNG/model/QQ transport 及 live/embedding 标记；普通测试选择 `not live and not embedding`，真实向量仅用预下载固定revision缓存，禁止隐式下载

- [x] T005 在 src/sana_bot/cli.py 建立命令分组及 JSON 输出/退出码规范：0成功、2配置错误、3资料未就绪、4外部服务失败；未知功能明确未实现，不返回伪成功

**Checkpoint**: 建立可重建的 Python 3.12 项目，不连接任何账号。

---

## Phase 2: Foundational (Blocking Prerequisites)

**Goal**: 完成所有故事共用的配置、事务、协议边界与诊断基础；完成本阶段后才开始故事任务。

**Independent Test**: 配置越界拒绝、SQLite回滚、端口替代与脱敏检查通过。

- [x] T006 [P] 在 src/sana_bot/config.py 定义环境密钥与版本化非敏感配置；落实“正数权重且w_happy>w_keyword及w_day；门槛>0；空词表合法”、独白p∈[0,1]及正等待时长、300秒窗口/10秒余量，非法更新保持旧版本

- [x] T007 [P] 在 src/sana_bot/ports.py 定义 research/contracts 的 IngestService、Retriever、StateReducer、DialogueService、OutputPolicy、QQTransport、AffectionService、MonologueService 协议和带 reason_code/correlation_id 的结果；支持注入替代实现

- [x] T008 [P] 在 src/sana_bot/storage/database.py 建立 SQLite WAL、版本迁移入口、短事务与连接生命周期；网络和模型调用不得位于数据库写事务内

- [x] T009 [P] 在 src/sana_bot/diagnostics.py 定义 low_match/index_failure/match_error/state_transition/output_blocked/delivery_error/logging_error 分类，密钥/推理/prompt不落日志，诊断失败有备用信号但不伪报记录成功

- [x] T010 在 src/sana_bot/storage/schema.py 建立 UserIdentity、Conversation、InboundEvent、Turn 基表；逐字落实“(app_id,user_openid)唯一；禁止假设openid等于QQ号或跨应用一致”“每用户一个活动会话；generation每次新消息递增；scene=unknown/public/private”“外层事件id与消息id分开；事件幂等键和(app_id,message_id)均去重；无事件id以消息id回退”“inbound_id唯一；来源时间决定聊天日，回复处理不能改变日期”

- [x] T011 在 src/sana_bot/scheduling/serial_queue.py 实现每用户串行队列、入站持久化与generation更新接口、容量/超时/重启恢复边界；不同用户可并发，未支持/重复事件不能生成重复轮次

- [x] T012 在 tests/unit/test_foundation.py 验证配置原子生效、数据库唯一约束/回滚、日志脱敏和队列隔离；使用可重现并发屏障而非真实sleep；阶段结束运行现有离线检查

**Checkpoint**: 完成所有故事共用的配置、事务、协议边界与诊断基础；完成本阶段后才开始故事任务。

---

## Phase 3: User Story 6 — 按角色和语义整理资料 (Priority: P1)

**Goal**: 提供无QQ依赖的资料导入、角色切分、向量语义切分与来源核对。

**Independent Test**: 独立导入正式DOCX与合成短样例；角色/类型准确、原文覆盖可追溯，语义边界与问答关联达到SC-012–014。

- [x] T013 [P] [US6] 在 tests/contract/test_document_index.py 编写字段、角色、无名对话/说明区别与来源区间契约测试，覆盖长句、短答、重复文本和部分失败

- [x] T014 [P] [US6] 在 tests/integration/test_semantic_ingest.py 编写 fake 向量语义边界/跨角色关联与禁止静默截断测试；真实本地模型质量使用 embedding 标记分开运行

- [x] T015 [US6] 在 src/sana_bot/ingest/models.py、src/sana_bot/storage/index_schema.py 定义 StructuredDocument、SourceDocument、RuleNode、SourceSpan、SemanticUnit、EmbeddingRecord；落实“类型/角色单一；原文无损；说明无speaker不等于monologue”“跨角色可关联，不混改说话者；同文本不同来源均保留”“512维，输入≤512token，有限实数，禁止静默截断；不同revision不混检索”；SourceDocument 必须满足原设计约束“正式路径与SHA256明确；不执行文档内操作指令”

- [x] T016 [US6] 在 src/sana_bot/ingest/docx_reader.py 实现OOXML有序读取、可见标题/正文及numPr层级和位置保留；不只依赖Heading，不执行文档指令，为同阶段字段分类提供结构输入，US8消费已分类结果

- [x] T017 [US6] 在 src/sana_bot/ingest/rule_parser.py 细化标题→列表→冒号作用域及trigger/overall/action/behavior/speech/recovery/attribute/example/negative字段；落实“parent作用域不得跨同级标题；类型为profile/general_rule/state/action/example/negative/output_rule/dialogue/monologue”；必须在角色切分前完成，作为 T018 的输入前置，US8只消费已分类节点提取规则

- [x] T018 [US6] 在 src/sana_bot/ingest/role_splitter.py 对实际对话提取角色及无名独白，处理已确认别名、父子片段/前后链接、重叠区间覆盖统计；不得把标题/规则/动作说明当独白

- [x] T019 [US6] 在 src/sana_bot/retrieval/embeddings.py 实现本地 BAAI/bge-small-zh-v1.5 的 LangChain 接入、精确revision清单与实际token计数；分段后含检索指令仍≤512token，禁止截断或调用聊天模型造向量

- [x] T020 [US6] 在 src/sana_bot/ingest/semantic_splitter.py 比较同硬边界内相邻句窗向量提出语义边界，目标≤384token；超长句细分记录parent/offset，跨角色问答用关联而非混角色拼接

- [x] T021 [US6] 在 src/sana_bot/ingest/service.py、src/sana_bot/cli_index.py 实现 index parse/review，输出不可变StructuredDocument与structure_hash；角色/语义切分可输出待校准预览但不能激活，保存边界/类型依据和失败范围，正式快照留给US8固定参数build

**Checkpoint**: 提供无QQ依赖的资料导入、角色切分、向量语义切分与来源核对。

---

## Phase 4: User Story 8 — 正式设定索引与分层约束 (Priority: P1)

**Goal**: 从正式设定提取适用状态、全局禁区与格式，建立批准目录、校准和强制发送前策略。

**Independent Test**: 不接QQ，用正式文档八章、补充约束及六类分支样例核对结构；违规草稿全部拦截，未批映射不可执行。

- [x] T022 [P] [US8] 在 tests/contract/test_character_rules.py、tests/fixtures/character_structure.json 编写正式文档八章/补充约束、同段多字段、负例非候选与示例角色契约测试；fixture标记人工核对状态

- [x] T023 [P] [US8] 在 tests/unit/test_output_policy.py 编写句末中文/英文句号、省略号、禁句、第一人称、代用户心理/台词、短句和动作模板测试；包含正常反例避免一律拒发

- [x] T024 [US8] 在 src/sana_bot/state/catalog.py、config/state-catalog.draft.json 提取 StateRule 的来源、provenance=source/derived、approved、关系/场景、触发/退出和优先级；六类推导映射默认未批准，恋人限定硬限制始终生效

- [x] T025 [P] [US8] 在 tests/unit/test_state_event_extractor.py 编写有限标签、原文字符位置/quote一致、无证据/解析错误不转换、标签冲突保持旧态与恶意改分指令被拒绝测试，使用fake结构化模型端口

- [x] T026 [US8] 在 src/sana_bot/state/event_models.py、src/sana_bot/state/event_extractor.py 及 src/sana_bot/ports.py 定义并实现 StateEventExtractor，输出批准目录标签+source_message_id+证据区间/quote；Pydantic核对原文与标签，失败返回no_event并记录原因，禁止直接to_state/score字段；先以可注入LangChain调用端口独立验证，US1接入真实DeepSeek适配

- [x] T027 [US8] 在 src/sana_bot/state/reducer.py 实现经批准事件到主情绪/二级标签/主导表达转换，冲突保持合法旧态，unknown场景采用public限制；模型标签须有当前输入证据，禁止改分数/配置/执行动作

- [x] T028 [US8] 在 src/sana_bot/dialogue/output_policy.py 实现固定加载全局禁区与输出规范、规则化检查和受约束语义审查；最多一次改写，失败使用预审兜底/独白静默，审核异常拒发草稿

- [x] T029 [US8] 在 src/sana_bot/dialogue/fallback_catalog.py 生成可审阅的疑惑/失败/查询回复目录并通过同一策略校验；自然文字动作、第一人称、句末无句号，禁止把草稿直接流给QQ

- [x] T030 [US8] 在 src/sana_bot/storage/index_repository.py 实现 SplitCalibrationProfile、IndexSnapshot 与分阶段CalibrationProfile，结构/切分配置不可变；snapshot_hash覆盖结构/目录/片段/模型/向量而不含后加校准报告，manifest另含报告引用；“同一时刻仅一个active”；对应能力报告未批准或hash不一致不得启用；独白enabled/disabled_no_material/disabled_uncalibrated，禁用阈值为空且不阻塞已验收聊天；CalibrationProfile 必须满足“stage=reply/monologue；cosine定义固定；报告必须匹配实际snapshot_hash；对应能力未批准不得启用”

- [x] T031 [US8] 在 src/sana_bot/retrieval/calibration.py、src/sana_bot/cli_index.py 实现 index approve --structure 与 calibrate-split --structure，保存批准catalog/结构hash、样本hash、模型revision和固定切分profile；SplitCalibrationProfile 必须满足“固定切分参数；变更产生新profile，不修改旧快照”；尚无最终检索报告，不把0.5当已校准默认值

- [x] T032 [US8] 在 src/sana_bot/ingest/service.py、src/sana_bot/cli_index.py 实现 index build --structure --split-profile，按固定参数重新生成片段/向量及snapshot_hash，不复用试切分结果；变更参数必建新版本，旧检索校准报告失效

- [x] T033 [US8] 在 src/sana_bot/retrieval/calibration.py、src/sana_bot/cli_index.py 实现 index calibrate --stage reply/monologue 与 activate：报告绑定实际snapshot_hash，reply必需；无真独白标disabled_no_material且阈值为空，有素材未校准disabled_uncalibrated，只有合法素材和批准报告才能enabled；禁用不创建机会，切换启用不补发历史轮次

- [x] T034 [US8] 在 tests/integration/test_calibration_lifecycle.py 验证切分校准→固定参数重建→门槛校准→启用，旧hash/参数报告被拒绝、片段随参数变化、无独白普通聊天可启用、禁用机会为零及补材料必须新版本

- [ ] T035 [US8] 在 runtime/reviewed-catalog.json、tests/fixtures/approved-calibration.jsonl 准备具体核对材料并取得提供者对映射及样本的确认，记录批准者/hash；缺确认则保留待审不勾完成，不影响继续其他不依赖批准的实现

- [x] T036 [US8] 在 tests/integration/test_character_index.py 验证完整/失败版本启用、全局约束不依赖召回、负例隔离、六类分支guard及SC-019–021；逐字正文原文保留而输出不机械照搬

**Checkpoint**: 从正式设定提取适用状态、全局禁区与格式，建立批准目录、校准和强制发送前策略。

---

## Phase 5: User Story 1 — QQ 连续对话 (Priority: P1) — MVP聊天链路

**Goal**: 实现官方QQ单聊、LangChain/DeepSeek、隔离上下文与统一合规发送；先不承诺高级语境或好感度。

**Independent Test**: fake QQ和模型完成10组三轮对话、用户隔离及失败路径；有凭据时另做官方沙箱验证。

- [x] T037 [P] [US1] 在 tests/contract/test_qq_webhook.py 编写dispatch raw-body验签/签名错误/op13挑战、事件id与d.id区别、重复投递、持久化失败503及ACK契约测试

- [x] T038 [P] [US1] 在 tests/integration/test_basic_chat.py 编写多用户三轮连续对话、媒体/空输入、模型失败、发送失败及unknown结果测试；注入模型和传输，不绑定资料真实审批

- [x] T039 [US1] 在 src/sana_bot/adapters/qq_auth.py 实现官方access-token获取缓存，检查HTTP及业务code，按expires_in刷新；实现挑战与dispatch签名，禁止旧个人QQ模拟登录

- [x] T040 [US1] 在 src/sana_bot/adapters/qq.py、src/sana_bot/app.py 实现HTTPS反代后的 /qq/webhook、op13与op0/C2C处理，先验签落盘/去重再200及op12 ACK；接入健康/就绪检查，不暴露管理HTTP

- [x] T041 [US1] 在 src/sana_bot/storage/delivery_repository.py 建立 OutboundDelivery/ReplyBudget；落实“(app_id,source_msg_id,msg_seq)唯一；kind normal/fallback/error/monologue/status”“按来源消息串行预留，预算≤4，保守deadline=event_time+300s”；保存 prepared/sending/confirmed/rejected/uncertain 及稳定序号

- [x] T042 [US1] 在 src/sana_bot/adapters/qq_sender.py 实现官方C2C文本发送，发前检查10秒余量/权限/拒收/额度，HTTP成功但业务失败不算confirmed；网络不明标uncertain，不换msg_id/msg_seq绕过限制

- [x] T043 [US1] 在 src/sana_bot/dialogue/deepseek.py 实现 ChatDeepSeek 异步生成与结构化JSON结果校验，模型名/地址环境配置、所有事件提取/生成/审核/改写共享整体30秒模型预算；为StateEventExtractor接真实LangChain适配并验证结构化解析；只取回复content，不输出reasoning、工具结果或流式草稿

- [x] T044 [US1] 在 src/sana_bot/dialogue/history.py 实现按会话读取上下文、隔离用户与固定本轮index_id，记录资料不可用原因；无已加载资料的基础聊天仅走明确的受控降级，不假称ready或资料命中

- [x] T045 [US1] 在 src/sana_bot/dialogue/service.py、src/sana_bot/scheduling/worker.py 接通入站队列→关系/场景读取→StateEventExtractor→批准reducer→快照→生成→OutputPolicy→QQTransport→送达事件；所有提示也占预算，缺乏领域服务用明确接口默认值而非伪造计分

- [x] T046 [US1] 在 src/sana_bot/cli_doctor.py、src/sana_bot/cli.py 实现 serve、doctor --offline/--live-model/--qq；服务启动恢复合法未过期入站，sending转uncertain；检查真实导入/模型/账号条件并区分未执行

**Checkpoint**: 实现官方QQ单聊、LangChain/DeepSeek、隔离上下文与统一合规发送；先不承诺高级语境或好感度。

---

## Phase 6: User Story 2 — 语境台词参考与独白分类 (Priority: P1)

**Goal**: 在完整合法索引上进行语境检索并按参考表达，保持出处与角色归属。

**Independent Test**: 人工确认最小资料或fixture独立验证同义触发、假关键词、冲突规则、非本人台词及无名说明；达到SC-002/003。

- [x] T047 [P] [US2] 在 tests/contract/test_retrieval.py 编写关系/状态/场景硬过滤先于打分、全局规则不参与最高分、负例不可候选与相等门槛有效的契约测试

- [x] T048 [P] [US2] 在 tests/integration/test_context_dialogue.py 编写同义表达、关键词同语境不同、示例非固定剧本、其他人物台词及资料歧义回退的测试

- [x] T049 [US2] 在 src/sana_bot/retrieval/vector_store.py 加载不可变NumPy矩阵与SQLite元数据，校验manifest/hash/revision/维度/有限实数，检索时不混索引版本

- [x] T050 [US2] 在 src/sana_bot/retrieval/filters.py 实现主情绪、二级标签、恋爱、独处/公共、角色与类型条件guard，过滤后最多召回8个语义单元，保留必要问答链接和来源位置

- [x] T051 [US2] 在 src/sana_bot/retrieval/service.py 实现统一余弦分数及来源返回、唯一更具体规则选择和冲突原因；候选相等门槛有效，异常分数不伪装为低分

- [x] T052 [US2] 在 src/sana_bot/dialogue/prompts.py 固定加载人物基线、禁区、输出格式，资料作为有类型引用而非工具指令；生成灵活情景回复，不能把设定说明/动作/他人台词当角色独白

- [x] T053 [US2] 在 src/sana_bot/dialogue/service.py 将检索和来源信息接入普通轮次，记录无命中/歧义/处理异常；按OutputPolicy发送且不冒充资料原文，US5后续完善低匹配分支

**Checkpoint**: 在完整合法索引上进行语境检索并按参考表达，保持出处与角色归属。

---

## Phase 7: User Story 5 — 低匹配兜底与调优日志 (Priority: P1)

**Goal**: 低匹配立即疑惑兜底，诊断可回溯前后文；独白低匹配只记录不主动打扰。

**Independent Test**: 20低分/空候选、10门槛边界及日志失败样例有明确分支；维护者可定位事件并关联最多三轮前后文。

- [x] T054 [P] [US5] 在 tests/integration/test_low_match.py 编写全低/空候选/等于门槛、无资料、异常分数、用户澄清后重新匹配以及独白静默测试

- [x] T055 [P] [US5] 在 tests/unit/test_diagnostics_retention.py 编写有限前后文关联、日志失败不阻塞、30天清理不删计分去重信息的测试

- [x] T056 [US5] 在 src/sana_bot/dialogue/match_decision.py 实现即时与独白分别采用已批准门槛的分支：below_threshold/empty_candidates疑惑或静默，no_material/processing_error另记；兜底符合当前心情与输出规范

- [x] T057 [US5] 在 src/sana_bot/storage/diagnostic_repository.py 实现 DiagnosticEvent 规定字段：输入/前后三轮、心情、候选来源分数、门槛、索引模型配置版本、触发原因、决定及发送结果；无候选保留空数组

- [x] T058 [US5] 在 src/sana_bot/cli_logs.py 实现 logs query/prune 按内部user/time/event/reason检索、本地维护权限与默认30天清理，后文只关联发生的消息，不虚构或无限复制内容

- [x] T059 [US5] 在 src/sana_bot/dialogue/service.py 接入低匹配决策和日志，明确fallback不创建独白/计分，后续用户补充正常重检索，输出校验失败与低匹配分开统计

- [x] T060 [US5] 在 tests/integration/test_tuning_trace.py 验证SC-009–011完整追踪、错误类型区分、跨会话不串用与实际发送结果回填，并用已有fallback目录检验疑惑及心情一致性

**Checkpoint**: 低匹配立即疑惑兜底，诊断可回溯前后文；独白低匹配只记录不主动打扰。

---

## Phase 8: User Story 3 — 心情查看、设置与表达 (Priority: P2)

**Goal**: 提供可控初始心情、合法二级状态管理及对话表达，保持有效快照一致。

**Independent Test**: 同题happy/sad/neutral对照和10次切换，无触发连续三轮一致；无效设置不改状态，动态转换仍遵循批准目录。

- [x] T061 [P] [US3] 在 tests/integration/test_mood_dialogue.py 编写默认neutral、合法/非法管理设置、快照一致、关系/公共限制和happy/sad长度与风格样例验证

- [x] T062 [US3] 在 src/sana_bot/state/models.py、src/sana_bot/storage/state_repository.py 实现 StateSnapshot，落实“一轮冻结同一快照；high开心别名统一happy；主情绪 neutral/happy/angry/sad”；高兴/开心统一happy，不把high当额外枚举

- [x] T063 [US3] 在 src/sana_bot/state/service.py 将管理设置、当前关系、已批准本轮事件和场景交给US8 reducer再冻结快照；无触发保持，重启短期neutral但不清长期关系

- [x] T064 [US3] 在 src/sana_bot/cli_state.py 实现 state show/set 使用内部user id，检查合法目录/标签/场景且普通QQ消息不能执行管理命令

- [x] T065 [US3] 在 src/sana_bot/dialogue/service.py、src/sana_bot/dialogue/prompts.py 使用同一快照调整happy/sad/angry语气与自然动作，不覆盖禁区；无资料支持的状态不得补造剧情

- [x] T066 [US3] 在 tests/integration/test_mood_quality.py 执行SC-004/005的确认样例评分和长度统计；记录真实模型名与配置，fake结果不冒充人物风格真实验收

**Checkpoint**: 提供可控初始心情、合法二级状态管理及对话表达，保持有效快照一致。

---

## Phase 9: User Story 7 — 好感度与恋爱进展 (Priority: P2)

**Goal**: 按用户跨会话保存关系，成功正常对话按关键词/聊天日/开心计分并可追溯。

**Independent Test**: 手算8→13→22，重复消息不变；日期、并发、重启与门槛前/等于/之后全正确；用户仅查自己关系。

- [x] T067 [P] [US7] 在 tests/unit/test_affection.py 编写最长非重叠关键词、1/2/5权重、自然日、happy快照、空词表、门槛等于100和改权重不重算测试

- [x] T068 [P] [US7] 在 tests/integration/test_affection_delivery.py 编写发送confirm后事务失败恢复、重投/并发/重启、uncertain不计分及日志清理后仍去重测试

- [x] T069 [US7] 在 src/sana_bot/storage/relationship_repository.py 建立 Relationship/DialogueDay/AffectionEvent/RelationshipConfig；落实“user_id唯一，score≥0，跨会话重启保留”“(user_id,local_date)唯一”“delivery_id与turn_id各唯一，new_day/happy_delta∈{0,1}”“正数权重且w_happy>w_keyword及w_day；门槛>0；空词表合法”

- [x] T070 [US7] 在 src/sana_bot/state/affection.py 实现confirmed normal delivery唯一结算事务：关键词最长非重叠次数+Asia/Shanghai来源接收日期去重+happy快照次数；默认1/2/5，查询/独白/兜底/失败不计分

- [x] T071 [US7] 在 src/sana_bot/state/relationship.py 实现初始0/未恋爱、≥100转恋爱中且下一轮生效，已romantic提高门槛不回退，版本化参数只对新事件生效；禁止直接用户指令改分数关系

- [x] T072 [US7] 在 src/sana_bot/cli_relationship.py、src/sana_bot/cli_config.py 实现 relationship show、config validate/apply；词表未提供为空，非法配置保留旧版，记批准配置版本与结算来源

- [x] T073 [US7] 在 src/sana_bot/dialogue/commands.py、src/sana_bot/dialogue/service.py 接入QQ /关系只读查询与成功送达计分；查询取消旧独白但不改情绪/计分，正常回复按下一轮关系应用分支

- [x] T074 [US7] 在 src/sana_bot/cli_delivery.py 实现uncertain的 delivery review，仅有官方证据的核对可转confirmed/rejected并幂等恢复计分；在 tests/integration/test_romance_quality.py 验证SC-015–018及关系不绕过禁区

**Checkpoint**: 按用户跨会话保存关系，成功正常对话按关键词/聊天日/开心计分并可追溯。

---

## Phase 10: User Story 4 — 沉默后概率心理独白 (Priority: P2)

**Goal**: 正常回复成功后等待一次，按最新语境/心情/关系匹配独白并受QQ预算约束。

**Independent Test**: fake clock/RNG验证p=0/1、60秒等待、续聊取消、最新状态重匹配；1000次30%统计25%–35%，未具备独白材料时静默。

- [x] T075 [P] [US4] 在 tests/integration/test_monologue_timing.py 编写正常送达起算、p=0/1、生成中续聊、发送提交竞态、最新状态变化及无材料/低分静默测试

- [x] T076 [P] [US4] 在 tests/unit/test_monologue_probability.py 编写独立机会一次抽样、1000次seeded概率统计、失败/不命中不重抽及独白不链式触发测试

- [x] T077 [US4] 在 src/sana_bot/storage/monologue_repository.py 建立 MonologueOpportunity；落实“turn_id（唯一）”、p∈[0,1]，保存generation、due_at、probability、random_draw、候选与skip_reason；pending/evaluating/sending/sent/failed/uncertain/cancelled/skipped状态合法转换

- [x] T078 [US4] 在 src/sana_bot/scheduling/monologue.py 仅从confirmed normal delivery创建一次机会，默认60秒/30%；先检查monologue_status，disabled不创建机会且返回具体原因；enabled但无真正独白资料跳过，不将人物说明生成可发送独白，不计好感度

- [x] T079 [US4] 在 src/sana_bot/dialogue/monologue.py 到期重读当前状态/关系/场景，过滤匹配后一次抽概率并调用统一OutputPolicy，展示【心理独白】；无候选/歧义/违规静默记原因

- [x] T080 [US4] 在 src/sana_bot/scheduling/dispatch.py 将最终generation核对、消息预算、稳定seq及提交时间接入QQ发送临界区；提交前续聊取消，提交后不可撤回有记录，网络未知不自动重发

- [x] T081 [US4] 在 src/sana_bot/scheduling/worker.py 实现新入站取消旧机会、会话结束及服务重启取消、不补发、不重复抽概率；保留普通任务恢复与confirmed计分幂等

- [x] T082 [US4] 在 tests/integration/test_monologue_platform.py 验证事件时间+300秒与回复成功+60秒的双时钟边界、10秒余量/四次额度/拒收、跨用户隔离及无日志提示打扰；按SC-007/008记录结果

**Checkpoint**: 正常回复成功后等待一次，按最新语境/心情/关系匹配独白并受QQ预算约束。

---

## Phase 11: Polish & Cross-Cutting Concerns

**Goal**: 完成全链路质量和真实验收证据，不把实现或测试未执行项标完成。

**Independent Test**: 自动化、真实Embedding、真实DeepSeek、QQ沙箱及SC-001–021分别有可核对结果，未满足发布门槛明确列出。

- [x] T083 [P] 在 README.md、specs/001-sana-character-chat/quickstart.md 更新已实现命令、依赖复现、权限/HTTPS部署步骤和运维恢复；先核对全部命令存在，不承诺未验收功能

- [x] T084 [P] 在 tests/integration/test_capacity.py 验证100关系档案、10活跃用户、5入站/秒、热检索p95≤200ms、ACK p95≤1秒与正常回复端到端p95≤20秒；端到端自入站持久化至QQ确认发送，含排队/事件提取/生成/审核/一次改写，另以fake clock验证共享30秒模型预算及超时合规路径；真实模型/网络测量与fake预算证据分开，排队过载明确处理，指标为本服务目标而非官方期限

- [x] T085 在 src/sana_bot/app.py、src/sana_bot/cli_doctor.py 集成启动完整性、任务关闭和周期日志清理，运行目录维护者访问；回归未启用索引不可ready、密钥不泄漏和规则失败拒发

- [ ] T086 在 specs/001-sana-character-chat/validation/official-qq.md 发布前重查官方文档链接/日期，记录5/60分钟矛盾、鉴权、权限、限流、原始body及沙箱回调证据；执行须有用户账号/HTTPS条件，缺条件标未执行且不勾完成

- [ ] T087 在 specs/001-sana-character-chat/validation/results.md 按quickstart执行现有离线检查、真实固定revision编码、DeepSeek及QQ沙箱分开验收，覆盖SC-001–021并记录样本hash/配置/证据；未核对映射、未提供独白材料和未获权限清楚列为未完成门槛

**Checkpoint**: 完成全链路质量和真实验收证据，不把实现或测试未执行项标完成。

---

## Dependencies & Execution Order

### Phase Dependencies

Setup → Foundational 阻塞所有故事；文档P1顺序为US6 → US8 → US1 → US2 → US5，
随后P2为US3 → US7 → US4，最终收尾。该顺序是单人安全执行线，下面列明可替代依赖。

### User Story Dependencies

| 故事 | 实现前置 | 独立验证方式 |
| --- | --- | --- |
| US6 | Foundational | DOCX与向量fixture，不依赖QQ |
| US8 | US6数据契约/结构读取 | 静态目录、fake模型审核、无QQ；未知状态批准不伪造 |
| US1 | Foundational与US8输出规则服务 | fake状态/关系只读端口，三轮C2C，不依赖US7实现 |
| US2 | US6索引、US8有效规则、US1对话接口 | fixture索引与状态独立检索；完成后接QQ |
| US5 | US2评分接口、US1发送分类 | fake输入分数与日志查询，不依赖真实独白调度 |
| US3 | US8状态reducer、US1/US2回复端口 | fake关系/模型，同题情绪对照 |
| US7 | US1送达事件、US3有效快照 | fixture消息/成功送达进行事务验算，不需要US4 |
| US4 | US1预算、US2独白分类、US5诊断、US3/US7最新状态 | fake clock/RNG/transport；真实缺资料时不发 |

US6先完成字段分类再做角色/语义切分；US8在不可变结构上校准切分参数后重建快照再校准检索门槛。US8创建事件提取器和领域reducer，US3才接入管理与轮次快照；US8的恋爱guard接收只读枚举，
不依赖US7计分实现。US2检索与US5诊断通过端口先测，不能引入反向依赖导致循环。
US1无资料的受控降级仅是离线/开发检查路径，不绕过正式readyz及完整索引激活门槛。

### Within Each User Story

先契约/行为测试与模型约束，再领域实现、CLI/服务接线、独立验收；
真实质量/审批任务不能由fake测试结果替代。外部确认缺失时完成所有不依赖它的代码和离线验证，
只保留实际受阻的批准/启用/真实验收任务未勾选，不重复申请已获授权的操作。

### Parallel Opportunities

并行仅限下列组；每组前置为该阶段入口依赖已完成，且测试可先依已定义契约编写。
未列任务串行。共享cli.py、app.py、schema等入口修改由阶段集成任务统一完成。

## Parallel Example: User Story 6

T013 与 T014 可在该故事前置满足后并行，分别写不同测试文件；对应领域实现完成后共同运行。

## Parallel Example: User Story 8

T022、T023 与 T025 可在该故事前置满足后并行，分别写不同测试文件；对应领域实现完成后共同运行。

## Parallel Example: User Story 1

T037 与 T038 可在该故事前置满足后并行，分别写不同测试文件；对应领域实现完成后共同运行。

## Parallel Example: User Story 2

T047 与 T048 可在该故事前置满足后并行，分别写不同测试文件；对应领域实现完成后共同运行。

## Parallel Example: User Story 5

T054 与 T055 可在该故事前置满足后并行，分别写不同测试文件；对应领域实现完成后共同运行。

## Parallel Example: User Story 3

T061 可与同故事不触及该测试文件且不依赖其结果的实现准备并行；正式集成仍串行。

## Parallel Example: User Story 7

T067 与 T068 可在该故事前置满足后并行，分别写不同测试文件；对应领域实现完成后共同运行。

## Parallel Example: User Story 4

T075 与 T076 可在该故事前置满足后并行，分别写不同测试文件；对应领域实现完成后共同运行。

## Implementation Strategy

### MVP First

最小可演示范围为Setup、Foundational、US6、US8的全局规范与完整有效资料路径、US1。
没有独白资料时允许普通聊天独立启用，明确disabled_no_material且不创建机会；完整六类状态审批仍属于正式发布门槛；未批准时只演示已确认规则，不能宣称八故事全部完成。
MVP验证QQ正常三轮、上下文隔离、统一输出规范与失败路径。US2/US5完成前不得声称已具备语境匹配及低匹配调优能力。

### Incremental Delivery

先资料和硬约束，再QQ基础对话；追加语境匹配/兜底，之后接心情管理、长期关系与独白。
每阶段 checkpoint 验收只声称已实现范围，最终全部发布门槛通过才作为完整交付。
不自动部署、申请账号、发布机器人或改动用户原始DOCX。

### Parallel Team Strategy

本清单定义并行机会，不要求自动派发代理。Setup可并行编写忽略/凭据示例与测试夹具；
Foundational的配置、协议、数据库连接、脱敏模块可以并行。故事阶段并行示例均为不同文件的测试，
实现阶段遇同文件接线必须顺序处理；真实样本核对可与不依赖核对结果的基础代码工作并行。

## Requirement & Contract Coverage

| 需求 | 任务归属 | 验证来源 |
| --- | --- | --- |
| FR-001–002 | US1 | QQ契约、SC-001 |
| FR-003–007 | US6/US8结构分类、US2匹配 | document-index契约、SC-002/003 |
| FR-008–010 | US8 reducer、US3管理快照 | SC-004/005/020 |
| FR-011–012 | US1失败处理、US8文本表现 | SC-006/021 |
| FR-013–017 | US4 | SC-007/008 |
| FR-018–023 | US5、US8校准 | SC-009–011 |
| FR-024–028 | US6 | SC-012–014 |
| FR-029–035 | US7、US3快照 | SC-015–018 |
| FR-036–039 | US8 | SC-019/020 |
| FR-040–043 | US8统一策略与核对 | SC-021 |

QQ wire协议由US1/US4覆盖，文档索引契约由US6/US8/US2覆盖，
application-cli的doctor/serve、index、logs、state、relationship/config、delivery分别有相应任务。
所有模型的字段来源均为data-model.md，任务引用的逐字约束必须落到验证或唯一键，不允许随实现偏好放宽。

## Notes

- 本次仅写tasks.md；所有复选框保持未勾选，不表示任何实现或运行验证已完成。
- [P]不是允许忽略前置依赖；任务编号顺序唯一且连续，故事标签保留规格US编号。
- 资料推导状态、样本与模型revision核对需要具体可审阅产物；不得自动批准自己生成的映射。
- 除明确的平台限制与现有发布门槛外，不扩展功能到管理后台、群聊、训练、向量数据库或实际动画。

## Task Summary

| 范围 | 任务数 |
| --- | ---: |
| Setup / Foundational / Polish | 17 |
| US1 | 10 |
| US2 | 7 |
| US3 | 6 |
| US4 | 8 |
| US5 | 7 |
| US6 | 9 |
| US7 | 8 |
| US8 | 15 |
| 总计 | 87 |

共 24 项标记[P]。

## 实施结果（2026-10-16）

- 84/87 项已完成并勾选；离线检查 `uv run pytest tests/unit tests/contract tests/integration -m 'not live and not embedding'` 通过（**333 passed / 4 deselected**）；
  真实服务检查 `uv run --env-file .env pytest tests -m live` 通过（**3 passed**）。
- 真实服务已执行的部分（凭据由维护者提供，仅经环境注入）：
  - DeepSeek：鉴权、文本生成与结构化 JSON 通过（`deepseek-flash`）。
  - 向量：改为**远程 OpenAI 兼容后端**（`BAAI/bge-large-zh-v1.5` @ 硅基流动，1024 维）。
    正式 DOCX 已完成结构化 → 切分校准 → 179 单元快照 → reply 门槛 0.39 → 启用，且
    **同参数重建得到相同 `snapshot_hash`、相同门槛**（可复现性实测）。
  - QQ：取 token 鉴权通过（HTTP 200）。
  - 端到端一轮：命中资料 → 真实模型生成并合规；无关内容 → `below_threshold` 困惑兜底。
  - 详见 `validation/results.md` 第 1b、1c、2、3b 节。
- 本轮真实验收与本地聊天台实测共发现并修复 6 项实现缺陷（`expires_in` 字符串、向量工厂被绕过、
  快照未记录映射版本、批准不校验 catalog 所属结构、**共享模型预算从不复位**、
  **预算耗尽分支抛 TypeError**），均在 `validation/results.md` 第 2 节记录并补了回归测试；
  后两项都配了“移除修复即失败”的验证。
- 未勾选的 3 项需要外部条件，未完成却保持待审：
  - **T035** 六类映射**已由维护者「米豆」批准 17/17 条**（`runtime/reviewed-catalog.json`，已登记 `structure_approval`）；
    但**样本确认仍未取得**：门槛校准样本仍是仓库 fixture（`samples_with_expected=0`），
    不能据此宣称 SC-003/SC-009/SC-013 的质量结论，故不勾完成。
  - **T086** 需要平台应用权限、HTTPS 回调与账号条件（见 `validation/official-qq.md`）。
  - **T087** 除 QQ 沙箱外均已执行并记录；QQ 沙箱回调与 C2C 实际发送仍缺 HTTPS 回调地址与沙箱用户，
    按「缺条件标未执行且不勾完成」处理。
- 材料已就绪但仍待确认：`tests/fixtures/character_structure.json`、`tests/fixtures/approved-calibration.jsonl`、
  `tests/fixtures/approved-monologue.jsonl`。`config/state-catalog.draft.json` 为待审草稿，
  批准版本另存 `runtime/reviewed-catalog.json`（不提交）。
- 离线通过不能替代真实服务质量验收；发布门槛与未执行项见 `validation/results.md` 第 6 节。

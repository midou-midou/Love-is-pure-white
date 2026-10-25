---
description: "基于整理版角色设定重建分层 RAG 索引库的依赖有序实施任务"
---

# Tasks: 基于整理版角色设定重建分层 RAG 索引库

**Input**: `specs/002-rebuild-character-rag/` 中的 [spec.md](spec.md)、[plan.md](plan.md)、[research.md](research.md)、[data-model.md](data-model.md)、[contracts](contracts/)、[quickstart.md](quickstart.md)。

**Prerequisites**: 先读上述设计与 `.specify/memory/constitution.md`。沿用 Python 3.12、uv、LangChain、QQ 官方入口，保持现有依赖与业务数据。

**Tests**: 规格 FR-022、成功标准 SC-001～SC-008 和实施计划明确要求验证，因此包含必要测试与评估任务。故事内测试先编写并确认针对未实现行为失败，再完成实现使其通过；不为低影响文档变更另写镜像测试。

**Organization**: 按用户故事分阶段；所有路径均相对仓库根目录。`[P]` 仅表示同阶段已满足先决条件后可在不同文件并行，不代表免除阶段依赖。所有任务当前未执行；本次只生成任务清单。

## Format and Path Conventions

任务格式为 `- [ ] T编号 [P可选] [US故事标签] 操作与具体路径`。共享阶段无US标签；用户故事阶段必须有US标签。新模块及fixtures路径为实施目标，不表示当前已存在。

## Phase 1: Setup

**Goal**: 固定开发环境，准备隔离运行目录和不依赖真实账号的测试支撑。

**Independent Test / Checkpoint Criteria**: 此阶段不改变运行索引。

- [X] T001 在 `.python-version` 固定 3.12.14，核对 `pyproject.toml` 的 >=3.12,<3.13 与 `uv.lock` 兼容，通过 uv 重建 `.venv` 并把版本及基线检查结果写入 `specs/002-rebuild-character-rag/validation/results.md`；无必要不更新依赖

- [X] T002 [P] 在 `tests/support/package_harness.py` 准备独立临时 SQLite、可控编码器/tokenizer、LangChain 聊天模型和 QQ transport 替代依赖及冻结时钟；不读取真实凭据、不联网，不共享业务库

- [X] T003 [P] 在 `tests/fixtures/character_package/manifest.json` 固定当前六内容来源、三常驻和26卡预期统计，准备 malformed/escape/oversize/legacy 最小材料与原始字节哈希；不修改 `doc/乾纱凪角色设定整理版/` 来源

**Checkpoint**: 完成本阶段条件并记录实际结果后进入下一依赖阶段。

## Phase 2: Foundational

**Goal**: 建立跨故事共用的类型、迁移与错误契约，完成后才进入用户故事。

**Independent Test / Checkpoint Criteria**: 以旧库升级、模型校验与旧语义格式读取验证基础兼容性。

- [X] T004 在 `src/sana_bot/ingest/package_models.py` 定义 CharacterPackage、SourceFileVersion、ResidentMaterial、ReferenceCard、ApprovedPackagePolicy 和报告引用；逐项实现 `data-model.md` 的字段，固定“format_version=structural_v2”“chunking_strategy=markdown_cards_v1”“character=sana”“required_relationship=romantic”，并规定“相同卡 ID 在不同包版本可复用，在同一包内必须唯一”；卡type仅 emotion_rule/scene_rule/background/appearance/profile/dialogue_example，总则为emotion_policy；来源role仅core/output/emotion/scene/background/example/maintenance，provenance仅source/adapted/generated，unknown字段不可伪造事实

- [X] T005 [P] 在 `src/sana_bot/storage/index_schema.py` 与 `src/sana_bot/storage/schema.py` 添加下一可用编号迁移，复用 structural_*，新增 package_source/package_resident；落实“不能重写已执行的 0011”“快照沿用 staged/reviewed/active/failed”“独白报告与阈值为空”，将校准每 index/stage 唯一约束改为版本化报告并增加激活报告引用；不重建用户业务表

- [X] T006 [P] 在 `src/sana_bot/errors.py` 增加资料包、路径、字段、容量、绑定、关系冲突等 reason_code，并在 `tests/unit/test_package_errors.py` 核对 0/2/3/4 退出语义与脱敏错误位置；复用既有异常体系

- [X] T007 在 `src/sana_bot/ports.py`、`src/sana_bot/state/models.py` 定义 FrozenTurnContext 和 SelectionEvidence 接口，保留旧调用兼容；实现“缺值保持未知”和 index/snapshot/bundle/policy/catalog/calibration_id/report_hash、用户会话、前态及证据引用字段，旧记录不可伪造新包版本

- [X] T008 在 `tests/integration/test_package_migrations.py` 验证 T005 对已有0011库和空库的增量迁移、重复打开幂等、旧 semantic_v1 读取、原业务数据不变、校准报告多修订不可变与快照状态合法；依赖 T004～T007

**Checkpoint**: 完成本阶段条件并记录实际结果后进入下一依赖阶段。

## Phase 3: User Story 1 — 从整理版设定建立可核对的资料库 (P1, MVP)

**Goal**: 导入、核对并封存完整角色包，保持旧服务有效。

**Independent Test / Checkpoint Criteria**: 无需 QQ：导入当前资料两次，验证3常驻、26卡/25编码卡、完整来源、未纳入材料及容量故障；产物为 staged，不启用。

- [X] T009 [P] [US1] 在 `tests/contract/test_markdown_package.py` 按 `contracts/character-package.md` 编写输入契约测试：六文件清单、H2五字段任意顺序且非空、重复/缺失/未知字段、未知类型、额外文件、路径逃逸与符号链接；先确认相关测试在功能未实现时失败

- [X] T010 [P] [US1] 在 `tests/unit/test_markdown_card_boundaries.py` 验证吃醋三级与恢复同块、完整01/02、状态总则仅常驻、多轮示例半角/全角冒号及续行说话者、开头不明说话者报歧义、无speaker不推独白和Unicode原文位置；先确认失败

- [X] T011 [P] [US1] 在 `tests/integration/test_character_package_build.py` 编写重复导入一致性、来源追溯、完整25向量、缺失常驻、可信计数缺失、超限、非有限/错误维度向量、封存篡改和旧DOCX构建兼容测试；先确认失败

- [X] T012 [US1] 在 `config/character-package.json` 按资料包契约声明01～06与excluded 00/07、三resident IDs、25卡条件和状态映射、预算及版本；落实“scene=any/public/private，medium=any/text/co_present，relationship=any/romantic/non_romantic”；scope冲突或缺映射列为阻止批准项，禁止模型自动批准

- [X] T013 [US1] 在 `src/sana_bot/ingest/markdown_package.py` 实现显式清单解析、根目录路径校验、完整H2卡边界与元数据验证；保留“1-based 行首尾、Unicode 码点的半开字符范围”“原始正文不作 Unicode 归一化后再用于定位”，输出精确错误位置与未纳入清单

- [X] T014 [US1] 在 `src/sana_bot/ingest/markdown_package.py` 实现两份全文常驻、状态总则去重及示例speaker_turns；保留 source/adapted/generated 和06前言性质；落实“续行沿用前一说话者”“示例不是已发生事件”和“eligible_stages 仅 reply”，吃醋恢复不拆分

- [X] T015 [US1] 在 `src/sana_bot/storage/package_repository.py` 保存多来源关联、常驻及完整卡，映射一个group/field/leaf并固定“token_split=0”；规范内容哈希排除 created_at/运行ID/批准时间，保留六份 source_document，不能只存合成来源

- [X] T016 [US1] 在 `src/sana_bot/retrieval/embeddings.py`、`src/sana_bot/ingest/service.py` 对25卡的标题/类型/scope/完整正文实际输入执行绑定model/revision/tokenizer的精确计数，包含特殊token与前缀；落实“不截断、不自动摘要、不拆分”，可信计数缺失或超限报卡ID与限制，批次usage和启发式计数不得充当正式证明，检查维度、有限实数和模型一致性

- [X] T017 [US1] 在 `src/sana_bot/ingest/service.py` 实现格式分发与 staged 构建：3常驻不编码，25卡完整编码，封存六来源/常驻/卡/profile/policy/catalog/encoder/vectors；计算 source_set/structure/profile/policy/catalog/encoder/snapshot/bundle hashes，数值阈值与后加报告不进封存哈希；外部调用不在写事务内，失败不变更active

- [X] T018 [US1] 在 `src/sana_bot/storage/index_repository.py` 扩展 SnapshotRecord 与双格式格式校验、结构批准和快照写读；新格式不伪造split_profile，旧格式仍要求语义profile；记录“monologue_status=disabled_no_material”，补 `src/sana_bot/storage/package_repository.py` 的批准绑定读取

- [X] T019 [US1] 在 `src/sana_bot/retrieval/vector_store.py` 新增 structural_v2 装载及不可变缓存，重算实际封存文件、来源、向量与DB绑定哈希，不只信manifest；不同模型/revision/维度不可混用，未知格式明确拒绝，保留旧格式读取

- [X] T020 [US1] 在 `src/sana_bot/cli.py`、`src/sana_bot/cli_index.py` 实现 parse --format markdown-package、review、approve 和新格式 build 分发，支持 --runtime-dir；旧命令兼容，新格式禁止 semantic --split-profile，输出覆盖/来源/条件/计数/状态及错误；在US4准入实现前拒绝新格式activate

- [X] T021 [US1] 执行US1新增测试及既有导入/快照回归，将命令和结果写入 `specs/002-rebuild-character-rag/validation/results.md`；证明原文完整、3常驻26卡25向量、00/07零泄入、重复导入一致、四类故障拒绝，原active不变；验证 SC-001/SC-002，失败修复后再进入后续阶段

**Checkpoint**: 本故事验收可在隔离材料与替代依赖下独立运行；完整运行启用仍受US4和真实验证门槛约束。

## Phase 4: User Story 2 — 在连续聊天中稳定保持角色规则 (P1)

**Goal**: 每轮常驻角色约束，成功空检索也可交流；缺规则或关系冲突时受控失败。

**Independent Test / Checkpoint Criteria**: 使用已核对最小包和替代服务单独测试：无命中问候、历史背景、未知事实、关系冲突、不同用户和输出错误。

- [X] T022 [P] [US2] 在 `tests/contract/test_package_turn_context.py` 测试本轮固定所有版本及校准引用、三常驻完整注入、引用/用户消息不可升级配置、六轮历史上限和未知字段；不依赖生产activate，使用显式测试包，先确认失败

- [X] T023 [P] [US2] 在 `tests/integration/test_package_dialogue.py` 测试成功空候选/低分仍自然生成、旧格式原兜底兼容、核心缺失不调用正常生成、关系冲突不写账本、历史敌意不覆盖当前、示例不造用户事实及双用户隔离；先确认失败

- [X] T024 [P] [US2] 在 `tests/unit/test_package_output_policy.py` 测试第一人称、句末无句号、禁止代写用户动作/台词、示例当记忆、远程同地假设、可纯台词及审核失败有限重试；先确认失败

- [X] T025 [US2] 在 `src/sana_bot/dialogue/history.py` 与 `src/sana_bot/storage/package_repository.py` 将 MaterialAvailability 扩为冻结包引用，加载三常驻、policy/catalog和指定calibration报告；落实“冻结发生于读取活动包后、状态判断前”，同轮不重读active，缺常驻或绑定无效返回package_unavailable

- [X] T026 [US2] 在 `src/sana_bot/dialogue/service.py` 正常生成前校验包适用性：non_romantic返回package_relationship_mismatch，不调用恋人正常生成、不改好感或关系；复用当前会话事实，不新增全局偏好/长期记忆系统，地点/同居/称呼未知保持unknown

- [X] T027 [US2] 在 `src/sana_bot/dialogue/prompts.py` 将01、02和状态总则完整接入extra_rules，区分批准行为配置与有类型背景/示例引用；替代新格式硬编码短基线，历史是过去、示例非真实经历，用户和检索正文不能修改配置

- [X] T028 [US2] 在 `src/sana_bot/dialogue/match_decision.py`、`src/sana_bot/dialogue/service.py` 按格式区分ready_with_references、ready_without_references、ambiguous_reference和processing_error；空补充可正常生成，未知具体事实保持未知，编码/存储/生成异常不伪装成低分，常驻缺失不走无资料生成

- [X] T029 [US2] 在 `src/sana_bot/dialogue/output_policy.py` 扩展规则与受约束语义审核类别，覆盖代写、示例记忆和同地假设；从固定版本取输出约束，保留既有篇幅与重试上限，违规最终受控失败，允许纯台词且不机械动作组合

- [X] T030 [US2] 在 `src/sana_bot/cli_doctor.py`、`src/sana_bot/dialogue/monologue.py`、`src/sana_bot/scheduling/monologue.py` 按能力与格式检查就绪：常驻缺失not-ready，无素材独白不阻塞reply，在创建独白机会前跳过；不新增平台主动消息

- [X] T031 [US2] 在 `src/sana_bot/dialogue/service.py` 与 `src/sana_bot/storage/diagnostic_repository.py` 串接固定版本、常驻ID、未命中原因、生成审核及发送阶段诊断；保留消息去重、会话隔离、发送和关系结算边界，不记录密钥或完整系统提示并沿用正文保留期

- [X] T032 [US2] 执行US2新增及既有聊天/关系/独白回归，把至少24个无补充普通交流、未知事实和错误路径的结果写入 `specs/002-rebuild-character-rag/validation/results.md`；对24例至少23个相关回复留证，检查所有格式与严重错误为0；替代服务结果注明offline，真实自然性留待T060

**Checkpoint**: 本故事验收可在隔离材料与替代依赖下独立运行；完整运行启用仍受US4和真实验证门槛约束。

## Phase 5: User Story 3 — 依据场景和上下文使用完整参考卡 (P1)

**Goal**: 按完整状态闭环与条件选择规则，背景和示例按相关性补充，保留排除理由。

**Independent Test / Checkpoint Criteria**: 脱离QQ对标注上下文选卡，验证六状态、吃醋三级、安抚恢复、公共/独处/未知及多轮示例。

- [X] T033 [P] [US3] 在 `tests/unit/test_package_state_transitions.py` 测试from_states、exit_conditions、恢复事件、历史引用证据、四主心情与二级标签映射、真正受伤不升级尖锐、冲突保守保留；先确认失败

- [X] T034 [P] [US3] 在 `tests/contract/test_package_selection.py` 测试硬条件优先、unknown不满足public/private、medium/relationship、完整状态卡独立于分数、示例最多2、补充最多8、歧义及整卡预算排除；先确认失败

- [X] T035 [P] [US3] 在 `tests/integration/test_package_context_retrieval.py` 测试高相似但不适用卡被排除、安抚多轮连续、卡与动作适用区别、编码异常与无命中区分、完整恢复同卡；记录选中/排除理由，先确认失败

- [X] T036 [US3] 在 `tests/fixtures/character_package/retrieval.jsonl` 固定至少36个正例覆盖6情绪/6场景/6背景/7示例及12个条件负例，包含查询/历史/关系/scene/medium/前态/预期集合/排除集合和固定样本哈希；运行前冻结，不按结果反改预期

- [X] T037 [US3] 在 `src/sana_bot/state/catalog.py`、`src/sana_bot/state/service.py` 载入每轮固定包批准目录，与 `config/character-package.json` 的六状态、吃醋三级映射一致；保持四主心情且不改好感算法，目录版本与前态绑定，旧格式目录路径兼容

- [X] T038 [US3] 在 `src/sana_bot/state/event_extractor.py`、`src/sana_bot/state/event_models.py` 扩输入为最近历史和previous_state，引用具体source_message_id/偏移/quote并验证证据来自当前会话；模型仅提事件，不能直接写关系/情绪数值或批准目录

- [X] T039 [US3] 在 `src/sana_bot/state/reducer.py` 真正执行from_states及exit_conditions、关系场景守卫和已批准优先级；根据安抚恢复、重度吃醋/委屈转安静，冲突保留合法前态并记歧义；不以向量分数直接设置状态

- [X] T040 [US3] 在 `src/sana_bot/retrieval/package_selector.py` 实现三常驻优先、有效完整情绪卡与场景规则选择；条件固定“unknown 为运行时值，不能当 any 或 public”，private/public-only只接受明确值；通用公共情绪可收敛但hurt/secure仅private，未知/不匹配不补造条件

- [X] T041 [US3] 在 `src/sana_bot/retrieval/service.py`、`src/sana_bot/retrieval/filters.py` 为新格式实现背景/示例先条件过滤再余弦排序与快照绑定阈值；查询用本轮+最近3用户消息+状态摘要，超限仅移除完整较早消息，本轮超限报错，不改变旧语义格式行为

- [X] T042 [US3] 在 `src/sana_bot/dialogue/prompts.py`、`src/sana_bot/retrieval/package_selector.py` 实现生成预算：计入完整常驻/必要状态/本轮/历史/输出余量，最多2示例/8补充，整卡排除低优先示例与背景记budget_excluded；必要内容超限context_over_limit，历史按完整旧轮次移除且不丢恢复证据后假称有据

- [X] T043 [US3] 在 `src/sana_bot/dialogue/service.py`、`src/sana_bot/storage/package_repository.py` 将前态提取→归约→条件规则→补充检索→预算→生成接入固定上下文，持久化SelectionEvidence到retrieval_evidence并关联DiagnosticEvent，记录所有卡的条件/选择/排除/歧义，不重读active或伪造分数

- [X] T044 [US3] 执行US3测试和固定36正例/12负例，将选卡结果及状态连续性证据写入 `specs/002-rebuild-character-rag/validation/results.md`：命中率至少90%、不合条件采用为0，公共/未知不擅自亲近、安抚可恢复；此阶段仅使用测试绑定阈值，正式校准由T049实现

**Checkpoint**: 本故事验收可在隔离材料与替代依赖下独立运行；完整运行启用仍受US4和真实验证门槛约束。

## Phase 6: User Story 4 — 验证新版本并安全切换 (P2)

**Goal**: 将校准、行为评审、批准、原子切换与恢复连成不可绕过的准入流程。

**Independent Test / Checkpoint Criteria**: 两个有效包与一个坏包在隔离库演练五类切换，测试在途固定及业务数据不变，假报告不可启用。

- [X] T045 [P] [US4] 在 `tests/contract/test_package_index_cli.py` 覆盖calibrate/evaluate/approve-evaluation/activate/rollback参数、JSON和0/2/3/4错误契约；仅offline报告不可正常启用、无独白拒绝monologue校准，先确认失败

- [X] T046 [P] [US4] 在 `tests/integration/test_package_activation.py` 测试构建失败、未验收启用、成功切换、在途切换、旧版恢复，以及篡改文件/报告错绑/active竞争/幂等expected校验、旧报告新包拒绝、业务记录不变；先确认失败

- [X] T047 [P] [US4] 在 `tests/integration/test_package_evaluation.py` 测试固定样本哈希、真实逐轮产物、人工评分待审、offline/live分离、未执行不算通过、伪passed=true拒绝、报告修订不可变和阈值不改变bundle；先确认失败

- [X] T048 [US4] 在 `tests/fixtures/character_package/evaluation.jsonl` 与 `tests/fixtures/character_package/rubric.md` 固定24普通未命中、12类各2段至少3轮行为、全部输出检查、五类切换和5定位案例；书面定义各类通过/严重错误、>=22/24、>=23/24及普通日常>=80%不反转否认，评估前冻结并记录哈希

- [X] T049 [US4] 在 `src/sana_bot/retrieval/calibration.py`、`src/sana_bot/cli_index.py` 用同一运行选择器对T036全候选实际评分与条件过滤，生成结构化检索校准报告；不只对expected IDs评分，不拿预期反向造分，阈值/report_hash独立绑定snapshot/policy/encoder/sample，不改封存哈希，拒绝monologue

- [X] T050 [US4] 在 `src/sana_bot/ingest/package_evaluation.py` 实现evaluate的完整性/选择/无命中/24段行为/输出/运维分类结果与逐轮产物，mode仅offline/live，列替代依赖与未执行原因；live真实调用配置编码器及DeepSeek，QQ结果单列，评分先pending_review

- [X] T051 [US4] 在 `src/sana_bot/storage/package_repository.py` 实现不可变校准/回归报告修订与 approve-evaluation，绑定index/snapshot/bundle/profile/policy/catalog/encoder/sample及原始产物/逐项评分/严重错误/批准者；不能只写passed=true，完整必要门槛未过不能reviewed，活动报告不可就地替换

- [X] T052 [US4] 在 `src/sana_bot/storage/index_repository.py` 实现activate/rollback共用目标验证器：重算封存与来源、匹配批准和报告，新包要求离线门槛与真实编码/生成证据；旧格式按旧合法规则恢复，旧报告不得用于新包，所有检查在短写事务外完成

- [X] T053 [US4] 在 `src/sana_bot/storage/index_repository.py` 与 `src/sana_bot/storage/package_repository.py` 实现事务内expected_active比较、唯一active切换和ActivationAudit，固定所批准calibration_id/report_hash；即使目标已active也校验expected，失败保留原active，不修改业务表，不删除在途旧包

- [X] T054 [US4] 在 `src/sana_bot/cli.py`、`src/sana_bot/cli_index.py` 接入evaluate、approve-evaluation、rollback及新版activate校验，保留legacy兼容并实现--runtime-dir；不新增force绕过，不公开管理HTTP，报告路径与失败原因脱敏

- [X] T055 [US4] 在 `src/sana_bot/dialogue/history.py`、`src/sana_bot/cli_doctor.py` 与 `src/sana_bot/retrieval/vector_store.py` 集成激活报告固定和能力就绪校验：运行中只持有原包引用，后续轮次读取新包，缓存更新不使旧引用失效；新包常驻/报告无效not-ready，独白禁用不阻塞reply

- [X] T056 [US4] 执行US4自动化与5类隔离切换演练，按标识和摘要核对历史/关系/发送数据不变，记录版本及5个问题各<=3分钟定位结果到 `specs/002-rebuild-character-rag/validation/results.md`；检查缺材料、错绑和offline伪批准均不能启用，未真实调用不得写live通过

**Checkpoint**: 本故事验收可在隔离材料与替代依赖下独立运行；完整运行启用仍受US4和真实验证门槛约束。

## Phase 7: Polish & Cross-Cutting Concerns

**Goal**: 完成跨链路回归、真实质量与运维留证，准备显式切换交付。

**Independent Test / Checkpoint Criteria**: 满足SC-001～SC-008后才具备完整启用资格，真实平台结果与替代验证分开。

- [X] T057 在 `tests/integration/test_package_qq_regression.py` 与 `tests/contract/test_qq_webhook.py` 复核新包完整QQ链路：验签失败、重复事件、权限不足、限流、编码/模型/审核/发送失败、uncertain及会话隔离；发送和关系结算不重复，保守预算和时效不扩展

- [X] T058 [P] 更新 `README.md` 与 `specs/002-rebuild-character-rag/quickstart.md` 为已实现的双格式、完整卡导入/校准/评审/启用/恢复说明，移除新增命令尚未实现的提示须以实际完成为准；保留无独白、未知配置和容量失败处理，命令逐项核对CLI

- [X] T059 通过uv运行普通单元/契约/集成测试及quickstart离线流程，核对锁文件可重建，结果与未执行项目写入 `specs/002-rebuild-character-rag/validation/results.md`；普通测试不得隐式下载模型、读取账号或依赖付费服务

- [ ] T060 在隔离runtime按 `specs/002-rebuild-character-rag/quickstart.md` 实际构建25完整向量并运行真实DeepSeek固定评估，逐卡记录精确token计数与revision，在 `specs/002-rebuild-character-rag/validation/model-results.md` 留SC-001～SC-006证据并完成逐段人工评分；无可信计数/凭据/审核时明确未通过而非降门槛

- [ ] T061 按 `specs/002-rebuild-character-rag/quickstart.md` 核对QQ官方当前依据、应用实际C2C文本权限和沙箱条件，保持恋人关系适用性，不为演示改账本；在 `specs/002-rebuild-character-rag/validation/qq-results.md` 分别记录真实收发与替代失败路径，缺授权或匹配用户标未执行，不新增主动消息

- [ ] T062 汇总 `specs/002-rebuild-character-rag/validation/results.md` 的FR/SC逐项证据与未执行项，核对所有报告版本并准备显式启用/恢复操作记录；仅在匹配版本报告已获维护者批准及切换获授权时按quickstart切换目标runtime并做QQ回归，失败恢复合格旧版；不把待批准、未执行或仅离线成果记为已完成上线

**Checkpoint**: 完成本阶段条件并记录实际结果后进入下一依赖阶段。

## Dependencies & Execution Order

### Phase and Story Dependencies

```text
Setup T001–T003
       ↓
Foundation T004–T008
       ↓
US1 T009–T021 — 完整资料包构建（MVP，不启用）
       ↓
US2 T022–T032 — 常驻规则与正常交流
       ↓
US3 T033–T044 — 状态闭环与条件选择
       ↓
US4 T045–T056 — 校准、验收、批准与版本切换
       ↓
Cross-cutting T057–T062 — 回归、真实验证、操作交付
```

- T001 先固定环境；随后 T002/T003 可并行。基础阶段 T004 完成后 T005/T006 可并行，T007 消费 T004 类型，T008 等待基础实现完成。
- US1 依赖全部基础阶段，T009～T011 使用 T002/T003 支撑可并行先编写。T012→T013→T014→T015→T016→T017→T018→T019→T020→T021 依序集成，避免同文件并发。
- US2 独立验证使用已核对的最小包和替代服务，不要求 US4 已实现生产启用；完整集成依赖 US1 的仓储、快照和读取。T022～T024 可并行，T025～T032 串行集成。
- US3 验证可直接传入冻结状态与固定包，真实连续聊天集成依赖 US2。T033～T035 可并行，T036 固定样本后依序实现 T037～T044。
- US4 依赖 US1 的封存、US2 的生成与US3的运行选择器；不能在US1阶段绕过验收启用。T045～T047 可并行，T048 样本冻结后执行 T049～T056。
- T057 等待 US4；T058 文档可与T057并行，因其文件不重叠。T059 完成后进入 T060 真实模型验证，再 T061 QQ 检查，T062 汇总。所有真实调用、人工审核、权限不足或缺凭据均按实际状态记录。
- 校准阈值在 T049 的独立报告中产生，不能倒写 T017 封存哈希。T025/T055 必须固定报告引用，避免报告修订使在途轮次改变门槛。
- 实现与测试可逐故事独立验收，但运行环境只有在US4和跨链路验收完成后才可按完整准入流程切换。

### Parallel Opportunities

共 17 项标记 `[P]`，包括环境后的测试支撑与材料、基础迁移/错误契约、四个故事各三项独立测试，以及跨阶段文档更新。并行只针对指定文件，后续集成会修改共同服务，必须串行处理。任务清单提供并行机会，不要求自动创建子代理。

## Parallel Example: User Story 1

T002/T003 与基础阶段完成后，可同时编写：

- T009：`tests/contract/test_markdown_package.py` 输入契约。
- T010：`tests/unit/test_markdown_card_boundaries.py` 卡边界与续行。
- T011：`tests/integration/test_character_package_build.py` 构建与故障。

三者先表达行为并确认失败，T012之后依次实现，不在这些测试中各自复制完整业务逻辑。

## Parallel Example: User Story 2

US1可用后，可同时编写T022的 `tests/contract/test_package_turn_context.py`、T023的 `tests/integration/test_package_dialogue.py` 和T024的 `tests/unit/test_package_output_policy.py`。随后T025～T031按服务依赖集成，T032统一验证。

## Parallel Example: User Story 3

US2可用后，可同时编写T033的 `tests/unit/test_package_state_transitions.py`、T034的 `tests/contract/test_package_selection.py` 和T035的 `tests/integration/test_package_context_retrieval.py`。T036的固定样本先于选择器校准，不把预计卡集合当成实际检索结果。

## Parallel Example: User Story 4

US3可用后，可同时编写T045的 `tests/contract/test_package_index_cli.py`、T046的 `tests/integration/test_package_activation.py` 和T047的 `tests/integration/test_package_evaluation.py`。T049～T055串行补齐校准、报告、批准、激活与运行读取；不同命令不得各自实现一套不一致的准入规则。

## Implementation Strategy

### MVP First

完成 T001～T021：维护者可从整理版资料得到可核对、可追溯且完整封存的 staged 包，旧服务继续使用原版本。MVP验收是US1，不包含生产替换或自然对话质量已达标的声明。

### Incremental Delivery

1. US1先证明资料正确，满足来源和容量约束。
2. US2用最小包证明常驻规则、空检索、关系冲突及输出约束。
3. US3补全前态、恢复与场景选择，固定样本验证真实运行选择路径。
4. US4让校准与完整验收形成不可绕过的启用门，验证并发切换和恢复。
5. 跨链路阶段完成普通回归、实际模型评审和QQ授权/沙箱留证，最后准备显式操作交付。

### Requirement & Outcome Coverage

| Requirement | Implementation / verification tasks |
| --- | --- |
| FR-001 | T003、T012、T013、T020、T021 |
| FR-002 | T004、T014、T015、T025、T027、T030 |
| FR-003 | T010、T014、T015、T017、T021 |
| FR-004 | T004、T014、T018、T030 |
| FR-005 | T009、T012、T013、T020、T021 |
| FR-006 | T003、T013、T014、T015、T019、T021 |
| FR-007 | T004、T011、T015、T016、T017、T021 |
| FR-008 | T033～T044 |
| FR-009 | T023、T034、T038、T039、T040、T044 |
| FR-010 | T010、T014、T033、T038、T039、T043、T044 |
| FR-011 | T025～T029、T037～T043 |
| FR-012 | T010、T014、T023、T027、T029、T034、T041 |
| FR-013 | T023、T028、T032、T043、T060 |
| FR-014 | T007、T023、T026、T027、T029、T057 |
| FR-015 | T004、T012、T023、T026、T061 |
| FR-016 | T024、T027、T029、T032、T048、T060 |
| FR-017 | T006、T020、T031、T043、T050、T056 |
| FR-018 | T017～T020、T045～T056 |
| FR-019 | T005、T025、T046、T052、T053、T055、T056 |
| FR-020 | T014、T018、T030、T045、T049、T055 |
| FR-021 | T023、T026、T031、T057、T059、T061 |
| FR-022 | T021、T032、T036、T044、T048～T062 |
| SC-001 | T003、T009～T021、T060 |
| SC-002 | T008、T011、T015～T021、T046、T056 |
| SC-003 | T034～T044、T049、T050、T060 |
| SC-004 | T023、T028、T032、T048、T050、T060 |
| SC-005 | T024、T029、T033～T044、T048、T050、T060 |
| SC-006 | T024、T029、T048、T050、T060 |
| SC-007 | T008、T046、T052～T056 |
| SC-008 | T031、T043、T048、T050、T056 |

### Contract & Entity Coverage

- `contracts/character-package.md` → T004、T009～T021：来源、常驻、完整卡、策略及封存。
- `contracts/turn-context.md` → T007、T022～T044、T055：冻结上下文、状态、选择证据与生成审核。
- `contracts/index-cli.md` → T020、T045～T056：管理、校准、评审、启用和恢复。
- CharacterPackage/SourceFileVersion/ResidentMaterial/ReferenceCard → T004、T005、T012～T019。
- ApprovedPackagePolicy → T004、T012、T018、T037、T040、T049。
- FrozenTurnContext/SelectionEvidence → T007、T025、T031、T037～T043、T055。
- EvaluationReport/ActivationAudit → T005、T045～T056。

## Notes and Generation Validation

- 本次检查前后均未发现 `.specify/extensions.yml`，没有 before_tasks/after_tasks hook 可执行。
- 62项任务：Setup 3、Foundational 5、US1 13、US2 11、US3 12、US4 12、Polish 6；17项可并行。
- 每项有未勾选复选框、连续唯一ID、具体文件路径；用户故事阶段有对应US标签，其他阶段无US标签。覆盖全部22条FR与8条SC。
- 任务完成必须依据实际结果勾选；规格、规划和本清单本身不证明模型质量、实际授权、索引启用或QQ发送成功。

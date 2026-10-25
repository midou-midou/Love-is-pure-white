# 验收结果记录

**说明**：按 `quickstart.md` 执行。自动化（离线）与真实服务分别报告；
fake 依赖下的结果不冒充真实模型或平台验收。未执行项明确标注，不勾选完成。

## 1. 离线自动化

命令：`uv run pytest tests/unit tests/contract tests/integration -m 'not live and not embedding'`

| 项 | 结果 |
| --- | --- |
| 状态 | 通过（本地执行） |
| 依赖 | 临时 SQLite、fake 模型与 transport、seeded 随机源、fake clock |
| 覆盖 | 见 `quickstart.md` 第 8 节清单 |

## 1b. 真实 DeepSeek（live）— 已执行

命令：`uv run --env-file .env sana-bot doctor --offline --live-model --qq`

| 项 | 结果 |
| --- | --- |
| 状态 | **通过（小规模 smoke）** |
| 模型 | `deepseek-flash`（响应自报与配置一致） |
| 地址 | `https://api.deepseek.com` |
| 文本生成 | ok，10 字符，约 9.3s；结尾使用了句号 → 需经输出规范拦截或改写（符合设计） |
| 结构化 JSON | ok，字段 `["mood","reason"]`，约 2.5s；本地解析与校验通过 |
| 未执行 | 真实人物风格评分、多轮上下文质量判定（需人工/提供者参与） |
| 泄露检查 | 输出中不含密钥；不记录模型推理内容 |

结论：DeepSeek 连通性、鉴权、文本生成与结构化结果解析均可用；
`SC-003/004/015` 等**质量**指标仍需提供者确认，不能用本 smoke 替代。

## 1c. 真实 QQ 凭据（live）— 部分执行

| 项 | 结果 |
| --- | --- |
| 状态 | **凭据与鉴权通过；沙箱回调未执行** |
| 接口 | `POST https://api.bot.qq.com/app/getAppAccessToken` |
| HTTP | 200；返回字段 `["access_token","expires_in"]` |
| 缓存 | 记录 `expires_in`（本次为字符串数字 `"4458"`），按 60 秒余量刷新 |
| 延迟 | 约 183ms |
| 输出 | 仅记录不可逆短指纹（12 位），不记录 token 本身 |
| 未执行 | 沙箱回调验签、C2C 被动回复发送、权限/拒收/限流实测 |

**本次发现并修复的实现缺陷**：官方把 `expires_in` 返回为**字符串数字**，
原实现按数值类型校验会把有效凭据误判为业务失败（`qq_token_business_error`）。
现已统一转为秒数，并新增回归测试固定该行为（`tests/contract/test_qq_auth.py`）。

## 2. 真实向量后端（硅基流动 API）— 已执行

**背景**：需求原为“用 DeepSeek 的向量模型”。经核实 **DeepSeek 不提供向量接口**：
`GET /models` 只返回 `deepseek-flash`、`deepseek-v4-pro`；`POST /embeddings` 与 `/v1/embeddings` 均返回 404；
官方文档只列 chat/completions（见 `research.md` 决策 3）。经确认改用**硅基流动**的 OpenAI 兼容向量接口。

| 项 | 结果 |
| --- | --- |
| 状态 | **通过（真实远程向量，全链路已跑通）** |
| 后端 | `EMBEDDING_KIND=openai_compatible` |
| 地址 | `https://api.siliconflow.cn/v1` |
| 模型 | `BAAI/bge-large-zh-v1.5`（响应 `model` 字段与请求 id 一致） |
| revision | `BAAI/bge-large-zh-v1.5@api.siliconflow.cn`（远程后端以 `model@host` 记录） |
| 维度 | 1024（逐条 `embedding_record` 校验，179/179 条一致） |
| 服务返回 | 向量已归一化（‖v‖=1.0）；返回 `usage.prompt_tokens`，用于 token 计数核对 |
| 无静默截断 | 900 字符输入被服务以 HTTP 400 拒绝（`bge-large-zh-v1.5` 上限 512 token），不截断、不补零 |
| 提供方探测 | 该提供方**没有** `BAAI/bge-small-zh-v1.5`（HTTP 400，错误码 20012）。可用且实测均为 1024 维：`BAAI/bge-m3`、`BAAI/bge-large-zh-v1.5`、`Qwen/Qwen3-Embedding-0.6B` |
| 未执行 | 提供者确认的向量质量评分（SC-013 质量部分） |

### 2.1 在该真实向量上完成的产物

| 产物 | 值 |
| --- | --- |
| 结构 | `structure_hash=a4658123…`，149 节点 / 167 片段，往返核对通过 |
| 切分校准 | `SplitCalibrationProfile=1ed3012c…`，`window_sentences=1`、`boundary_similarity_threshold=0.45`，`model_revision=BAAI/bge-large-zh-v1.5@api.siliconflow.cn`，样本 6，准确率 0.6667 |
| 映射批准 | `catalog_hash=b1641cf7…`，17/17 条已批准，`reviewer=米豆`（登记于 `structure_approval`） |
| 索引 | `IndexSnapshot=e22eeff0…`（active），179 语义单元，向量形状 `(179, 1024)`，`snapshot_hash=441dee5d…` |
| 映射版本绑定 | `rule_catalog_version=b1641cf7…`，与 `structure_approval.catalog_hash` 一致 |
| 独白 | `monologue_status=disabled_no_material`（正式资料中真独白素材为 0），门槛为空，不创建机会 |
| 回复门槛校准 | `CalibrationProfile=13050dde…`，`threshold=0.39`，`score_metric=cosine`，准确率 1.0，`score_source=live_index` |
| 启用 | `index activate` 成功；同一时刻仅一个 active（旧快照降为 reviewed） |
| 就绪检查 | `doctor --offline --live-model --qq` → `ready=true`，`problems=[]` |

**可复现性实测**：用同一资料与同一固定切分参数**重新构建**（新 `index_id` `1cbe3664…`），得到**完全相同的**
`snapshot_hash=441dee5d…`；在其上重新校准得到**相同的门槛 0.39**。这直接验证了发布门槛
「固定切分参数重建后的 snapshot_hash 必须与校准报告一致」在真实远程向量下成立。

**版本绑定实测**：修复批准工件后 `catalog_hash` 由 `9a7a0fad…` 变为 `b1641cf7…`，
在同一资料与切分参数下 `snapshot_hash` 随之由 `e4a39e44…` 变为 `441dee5d…`。
即「换映射版本 = 换快照 = 旧门槛校准自动失效」，与 `data-model.md` 的绑定语义一致。

**证据强度说明（不得夸大）**：切分样本仅 6 条（评估 3 个窗口），回复门槛样本仅 4 条且
`samples_with_expected=0`（fixture 样本未绑定 `expected` 单元 id，按全局最高分候选评分）。
这属于**结构可运行性证据**，**不构成** SC-003/SC-009/SC-013 的质量结论；
质量结论仍需提供者给出绑定真实单元 id 的正/负样本。

### 2.2 真实端到端一轮（真实检索 + 真实 DeepSeek，QQ 发送替换为 fake transport）

| 输入 | 结果 |
| --- | --- |
| `社团的小猫今天怎么样` | `confirmed`；命中资料，真实 DeepSeek 生成角色风格回复（内容具体到资料细节，句末无句号，符合输出规范）；无 `low_match` 事件（高于 0.39） |
| `帮我写一个二分查找` | `confirmed`，`kind=fallback`；诊断 `low_match / below_threshold`，`threshold=0.39`、`candidate_count=8`，回退为困惑型短句 |

结论：检索-门槛-生成-输出规范-结算全链路在真实向量与真实模型下可用；
未命中资料时按设计降级而非硬答。同一进程内两轮均 `confirmed`，未出现 `passive_window_exhausted`。

**本次发现并修复的实现缺陷（六项）**：

1. `_as_int_env` 读取了 `os.environ` 而非传入的映射，导致 `EMBEDDING_DIMENSION=1024` 被忽略并回落到默认 512，
   与远程服务实际返回维度冲突。已改为读取传入映射，并回填 `EmbeddingSpec.dimension`。
2. `cli_doctor.build_services` 直接构造本地 `EmbeddingBackend`，绕过向量工厂：
   `serve` 在 API 配置下会启动失败（而 `doctor` 却因走另一路径显示通过）。已统一改用 `build_embedding_backend`。
   该缺陷说明“`doctor` 通过”不能替代“服务能启动”，因此补充了端到端真实一轮。
3. `IndexSnapshot.rule_catalog_version`（`data-model.md` 列明的字段）从未被写入：
   建索引时不记录所依据的已批准映射版本，快照无法回溯它按哪一版映射构建。
   现已由 `build_snapshot` 从其已校验的批准记录绑定 `document.catalog_hash`，
   并在两者不一致时以 `catalog_hash_mismatch` 拒绝构建（而非产出无法回溯的快照）。
4. `index approve` 不校验 catalog 自身声明的 `structure_hash`：可把属于结构 A 的 catalog
   登记为结构 B 的批准，而批准是“未批准映射不得执行”的唯一人工关卡。
   现已要求两者一致，否则以 `catalog_structure_mismatch` 拒绝且不落库。
   （正是该检查暴露出批准工件中遗留的过期 `structure_id`，已更正并重新登记批准。）
5. **共享模型预算从不复位**：`ModelBudget.start()` 只在首次写入 `started_at` 且永不清零，
   而生产装配让事件提取、生成、语义审核共用同一个 `model.budget`（`build_services`）。
   后果是**进程启动 30 秒之后的每一轮**都直接判定预算耗尽：状态提取永远失败、回复退化为兜底。
   此前未发现的原因很具体：单元/集成测试给各组件分配的是**各自独立**的预算对象，
   且单个测试进程存活时间远短于 30 秒，`serve` 之外的真实路径从未被覆盖。
   已新增 `ModelBudget.reset()`，并在每轮开始时复位共享预算，语义与规范一致：
   30 秒模型预算**按轮**共享，不跨轮累计。
6. **预算耗尽分支抛 `TypeError`**：`ChatDeepSeekModel._invoke` 以
   `self._failure(request, "model_budget_exhausted", ...)` 调用只接受一个位置参数的 `_failure`，
   于是预算耗尽不是返回合规失败，而是抛异常把整轮打成 `turn_exception`（用户收到空回复）。
   该类此前没有任何单元测试。已修正调用并新增 `tests/unit/test_deepseek_model.py`
   （覆盖预算耗尽、超时、空内容、非法 JSON、调用异常与 `response_format` 回退）。

以上 5、6 两项是同一场景的两层：预算不复位制造了失败，失败分支又写错导致崩溃。
两者都在本地聊天台的真实多轮测试中暴露，并各配一条能真正失败的回归测试固定。

此外，为避免“先写过分数再算准确率”的自证，校准改为**优先使用实时索引计算分数**，
并新增 `score_source`、`samples_with_expected` 两项指标暴露证据强度；两处 fixture 样例文件中的
预设分数与不存在的 `expected` 单元 id 已移除（后者现会报 `calibration_sample_not_in_snapshot`）。

**与本次缺陷无关、但需记录的外部现象**：`doctor --live-model` 的 smoke 预算是 30 秒，
DeepSeek 偶发单次响应超过该预算时会被记为 `live_model_failed:model_budget_exhausted`
（`reason_code` 把「本次调用超时」与「预算已被前面调用耗尽」合并）。同一条命令随后重跑即
`ready=true`（文本 7.1s）。这是外部延迟波动，不是本地缺陷；但它说明 `ready` 会随厂商延迟抖动，
发布判定不应只看单次 `doctor` 结果。

## 3. 真实 DeepSeek（live）

见第 1b 节：已执行并通过小规模 smoke。

## 3b. 真实远程向量（live，可重复执行）

命令：`uv run --env-file .env pytest tests -m live`

| 项 | 结果 |
| --- | --- |
| 状态 | 通过（3 passed） |
| 覆盖 | DeepSeek 文本与结构化 JSON、QQ 取 token、远程向量探测与一批编码 |
| 凭据缺失时 | 自动跳过（不计通过） |

## 4. QQ 官方沙箱

| 项 | 结果 |
| --- | --- |
| 状态 | **鉴权已执行；沙箱回调与消息发送未执行** |
| 已执行 | 官方凭证接口取 token（见第 1c 节） |
| 未执行原因 | 需要受支持的 HTTPS 回调地址与沙箱测试用户 |
| 依据 | 见 `official-qq.md` |

## 5. 指标 SC-001–021 状态

| 指标 | 离线可验证部分 | 真实条件部分 |
| --- | --- | --- |
| SC-001 | 用户隔离、上下文衔接结构验证通过 | 10 组三轮真实对话判定：未执行 |
| SC-002 | 角色/类型识别与独白区分（正式资料与 fixture）通过 | 30 个片段提供者核对：待提供者确认 |
| SC-003 | 门槛边界与歧义回退分支验证通过 | 20 正/20 负真实场景正确率：未执行 |
| SC-004 | 难过组长度中位数 ≤ 开心组 70%（fake 样例）通过 | 提供者心情判定 ≥85%：未执行 |
| SC-005 | 10 次切换自下一条生效、无效设置拒绝通过 | 真实对话风格判定：未执行 |
| SC-006 | 失败路径分类与提示（模型/发送/不支持输入）通过 | — |
| SC-007 | 1000 次 30% 概率落在 25–35%、p=0/1 边界通过 | 真实 20 场景独白：未执行 |
| SC-008 | 续聊取消、无匹配静默、心情改变重匹配通过 | 沙箱独白联调：未执行 |
| SC-009 | 全低/空候选/等于门槛/异常分数分支通过 | 提供者判定 90% 兜底质量：未执行 |
| SC-010 | 事件字段与前后三轮关联通过 | — |
| SC-011 | 独白低匹配静默、日志失败区分通过 | — |
| SC-012 | 结构与来源区间往返 100%（正式资料）通过 | 50 个片段提供者核对：待确认 |
| SC-013 | 语义边界与跨角色关联（fixture）通过；真实向量已跑通（179 单元，1024 维） | 提供者判定 93% 边界正确：未执行 |
| SC-014 | 禁止静默截断（超长输入报错）通过 | — |
| SC-015 | 手算 8→13→22、重复消息不变通过 | — |
| SC-016 | 聊天日去重、权重与配置版本通过 | — |
| SC-017 | 门槛等于 100 进入恋爱、状态不回退通过 | — |
| SC-018 | 并发/重投/重启不重复计分通过 | — |
| SC-019 | 原文逐字保留、输出不机械照搬通过 | — |
| SC-020 | 六类分支 guard（关系/场景）通过 | 提供者确认映射：待确认 |
| SC-021 | 违规草稿拦截、兜底合规通过 | — |

## 6. 发布门槛

- 依赖锁定与离线检查：通过（333 passed / 4 deselected）；真实服务检查：通过（3 passed）。
- 真实 DeepSeek smoke：通过；真实向量后端（硅基流动 API）：**通过，索引与门槛均已在其上构建**；
  QQ 鉴权：通过，沙箱回调：未执行。
- 结构核对与原文往返：通过；六类映射批准：**已取得**（审核人「米豆」，17/17 条，
  `catalog_hash=b1641cf7…`，登记于 `structure_approval` 并绑定到快照）。
- 固定切分参数重建后的 `snapshot_hash` 与校准报告一致：**真实向量下通过**（重建两次同为 `441dee5d…`）。
- 快照记录其所依据的已批准映射版本，且批准只对该结构生效：通过（新增两项契约测试）。
- 共享 30 秒模型预算按轮复位，耗尽时返回分类失败而非异常：通过（新增回归测试，且已验证移除修复即失败）。
- 无独白素材时 `disabled_no_material`、门槛为空、不创建机会：通过（真实资料确认 0 独白素材）。
- 端到端 p95 ≤ 20s、检索 p95 ≤ 200ms、ACK p95 ≤ 1s：fake 依赖下通过；真实单轮已跑通但**未做统计量**。
- SC-003/SC-009/SC-013 的**质量**结论仍未取得：需要提供者提供绑定真实单元 id 的正/负样本，
  现有 fixture 样本（`samples_with_expected=0`）只能证明门槛机制可运行。
- QQ 官方依据实施/发布时复核：**未执行**。

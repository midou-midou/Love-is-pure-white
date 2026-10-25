# 分层 RAG 实施后验证指南

本指南描述已实现的双格式索引的核对、构建、校准、评审、启用与恢复步骤。
`config/character-package.json`、资料包解析/构建/选择、CLI 子命令与离线测试均已实现，
实际执行结果见 [validation/results.md](validation/results.md)。真实编码、真实 DeepSeek 评审
与 QQ 沙箱联调仍需凭据，本仓库未执行，不记为通过。接口以 [CLI 契约](contracts/index-cli.md)
为准，输入见 [资料包契约](contracts/character-package.md)，字段见 [数据模型](data-model.md)。

## 1 环境与隔离运行目录

在项目根目录执行，Python 固定 3.12.14，使用 uv 与锁文件：

```bash
uv python install 3.12.14
uv sync --frozen
uv run python --version
```

预期 Python 为 3.12.14；`.python-version` 已固定为 3.12.14。测试数据库和产物使用 `runtime/rag-rebuild-validation`，不对现有业务库直接演练。runtime 不提交。凭据沿用既有环境注入方式，不复制到样本、报告或命令输出。

以下文件已就位，可直接使用：

- `config/character-package.json`：六来源清单、26 卡条件映射、状态目录、预算和策略版本。
- `tests/fixtures/character_package/retrieval.jsonl`：36 正例 + 12 条件负例（已冻结）。
- `tests/fixtures/character_package/evaluation.jsonl`：24 普通未命中用例 + 12 类各两段三轮行为对话（已冻结）。
- `tests/fixtures/character_package/rubric.md`：逐类通过标准、严重错误定义与门槛。
- `tests/fixtures/character_package/manifest.json`：当前资料版的字节哈希与冻结统计。
- 配置哈希由 `index parse` 输出，样本哈希写入校准/评估报告；评估结果不反向修改样本。

## 2 离线自动验证

```bash
uv run pytest tests/unit tests/contract tests/integration -m 'not live and not embedding'
```

预期：旧 semantic_v1 仍可读、可正常处理对话；新包解析与条件选择通过。至少覆盖多文件定位、示例续行、完整卡容量、公共／未知守卫、from_states和恢复、双用户隔离、低分自然回复、格式审核、五类切换及业务数据不变。

故障材料分别制造重复 ID、缺字段、缺文件和完整卡超限，逐一验证失败且不改变 active。另覆盖编码器计数不可验证、向量非法、文件篡改、报告哈希不符、expected-active 冲突。

## 3 导入并核对

按各命令输出填写后续 `STRUCTURE_ID`、`INDEX_ID` 和 `REPORT_FILE`；不要使用猜测或旧运行的 ID。

```bash
uv run sana-bot index parse --format markdown-package --source config/character-package.json --output runtime/rag-rebuild-validation/parsed --runtime-dir runtime/rag-rebuild-validation
```

记录返回的结构 ID 后设置变量：

```bash
STRUCTURE_ID='填写本次解析返回的structure_id'
uv run sana-bot index review --structure "$STRUCTURE_ID" --runtime-dir runtime/rag-rebuild-validation
```

核对：3 常驻项，26 张卡，25 张编码卡；00/07 不进入生成；每卡来源、scope、完整恢复、示例轮次可见。核对状态转换目录的前态、条件、出口及证据范围。确认后：

```bash
uv run sana-bot index approve --structure "$STRUCTURE_ID" --catalog config/character-package.json --reviewer maintainer --runtime-dir runtime/rag-rebuild-validation
uv run sana-bot index build --structure "$STRUCTURE_ID" --output runtime/rag-rebuild-validation/snapshots --runtime-dir runtime/rag-rebuild-validation
```

预期：新快照 staged，不自动启用；25 卡向量、3 常驻；monologue_status=disabled_no_material。
每条真实编码输入都有精确计数和 tokenizer 绑定。没有可信计数时预期拒绝
（`tokenizer_unverified`），不能将启发式计数或批次 usage 冒充逐卡证明。
构建需要固定了 `EMBEDDING_REVISION` 的本地模型或可核对的远程后端；
缺少时构建以退出码 3 明确失败，这是预期行为而不是可以跳过的步骤。

完整卡超限时记录卡 ID 与限制。本规格禁止拆卡；选择经过验证的兼容更大容量配置或重新整理来源后，从新配置版本重建，不能修改上限数值后假称模型支持。

## 4 校准、评估与审核

```bash
INDEX_ID='填写本次构建返回的index_id'
uv run sana-bot index calibrate --index "$INDEX_ID" --stage reply --samples tests/fixtures/character_package/retrieval.jsonl --runtime-dir runtime/rag-rebuild-validation
uv run sana-bot index evaluate --index "$INDEX_ID" --samples tests/fixtures/character_package/evaluation.jsonl --mode offline --output runtime/rag-rebuild-validation/evaluation-offline --runtime-dir runtime/rag-rebuild-validation
```

离线测试使用替代依赖，不能批准为真实运行证据。实际构建所用编码器和实际生成模型评估在凭据可用且获授权时执行：

```bash
uv run sana-bot index evaluate --index "$INDEX_ID" --samples tests/fixtures/character_package/evaluation.jsonl --mode live --output runtime/rag-rebuild-validation/evaluation-live --runtime-dir runtime/rag-rebuild-validation
```

使用既有真实配置注入，不默认读取秘密文件。逐段审核输出并登记实际评分，不能只填 passed=true。将真实编码、生成结果与离线完整性、切换结果合并成引用原始证据的完整报告；合并不能改变各执行模式标签。

```bash
REPORT_FILE='填写已完成逐项评分的报告路径'
uv run sana-bot index approve-evaluation --index "$INDEX_ID" --report "$REPORT_FILE" --reviewer maintainer --runtime-dir runtime/rag-rebuild-validation
```

验收阈值严格使用 [规格成功标准](spec.md) SC-001～SC-008。输出至少包括：36正例命中率、12负例违规数量、24未命中回复相关数量、24段行为通过数量、严重错误数量、全部回复格式结果、五类切换结果、5个问题定位耗时。未执行、失败、待人工审核均不得记为通过。

## 5 版本启用、恢复和在途一致性

隔离目录首次启用（不是生产切换）：

```bash
uv run sana-bot index activate --index "$INDEX_ID" --expected-active none --runtime-dir runtime/rag-rebuild-validation
uv run sana-bot doctor --offline --runtime-dir runtime/rag-rebuild-validation
```

预期 reply 就绪，独白明确禁用不阻塞。若只有替代服务报告，正常运行启用返回
`package_offline_only`（退出码 3）；离线演练须显式加 `--allow-offline-rehearsal`，
且只在测试替代依赖中验证状态转换，没有生产 `--force` 之类的绕过开关。

在测试中预先登记一个满足旧格式验收的旧快照，将其 ID 记为 OLD_INDEX_ID；演练旧→新→旧。存在活动版本时必须用实际 ID 作 expected-active，不能填写 none。恢复命令：

```bash
OLD_INDEX_ID='填写隔离环境中已验证的旧版本ID'
uv run sana-bot index rollback --index "$OLD_INDEX_ID" --expected-active "$INDEX_ID" --reason validation-rollback --runtime-dir runtime/rag-rebuild-validation
```

验证目标完整性失效、报告错绑和并发 active 变化时均拒绝。切换前后的业务数据标识与摘要一致；一轮生成延迟跨越切换时，常驻、卡、目录、阈值和输出规则仍全属原版本。

## 6 QQ 沙箱回归

- 维护者在官方后台核对应用 C2C 文本能力、沙箱用户、回调地址、鉴权与当前权限，记录日期及结果。未核对即标未执行，不推断已授权。
- 以原有入口进行文本对话；恋人资料包要求会话已满足 romantic，不能为了通过演示更改累计关系账本。没有匹配用户则在替代依赖验证关系分支，并报告真实恋人对话未执行。
- 普通问候、资料问题、公共／独处／未知、吃醋后安抚、真正难过、未知偏好和远程文字聊天逐一验证。
- 鉴权失败、重复事件、权限不足、限流、生成失败、输出审核失败、发送失败及 uncertain 通过替代服务可控复现；真实平台已发生的结果单列。
- 沿用保守消息预算与时效，不新建主动消息、不制造无素材独白。真实发送失败不能标记为回复已经送达。

官方依据及版本差异见 [research.md](research.md)。生产使用相同审批与切换流程另行显式启用，通过前保留原活动版本；启用后沙箱回归失败恢复仍合格的旧版本。本轮规划不执行任何真实发送或生产变更。

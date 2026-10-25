# 索引管理 CLI 契约

以下是待实现的目标接口，当前项目尚不支持新增参数与子命令。所有管理操作限本地或受控维护环境，不新增公共管理 HTTP 接口。

## 导入与核对

`index parse --format markdown-package --source CONFIG --output DIR`

- CONFIG 为显式包配置文件；旧 `index parse --source DOCX --output DIR` 保持原行为。
- 返回结构 ID、source_set_hash、structure_hash、profile_hash、常驻数量、卡片用途统计、排除文件与 issues。
- 成功只表示解析完成，不自动批准。错误不能报成功或启用半包。

`index review --structure ID` 或 `index review --index ID`

- 按格式显示多来源、完整卡、条件映射、未定义事实、来源性质、容量证明、批准及验收状态。
- 旧语义格式继续显示现有核对结果。

`index approve --structure ID --catalog CONFIG --reviewer NAME`

- 新格式 CONFIG 必须绑定 structure/profile/policy/catalog hashes，逐卡条件无未处理冲突。维护者批准不可由模型分析结果替代。
- 返回批准记录及仍未满足的构建/验收门槛；批准不等于启用。

## 构建与检索校准

`index build --structure ID --output DIR`

- 新 structural_v2 从批准配置构建，无需也不允许 semantic split profile；超容量拒绝，不转入语义切分。
- 旧 semantic_v1 仍要求 `--split-profile ID`，旧命令行为保持。
- 返回 index_id、snapshot_hash、bundle_hash、常驻和卡片数量、维度、编码输入计数、monologue_status。
- 编码器配置和报告绑定实际输入格式，禁止混模型或 revision。

`index calibrate --index ID --stage reply --samples FILE`

- 新格式用与运行一致的条件优先选择器校准背景及示例门槛；规则选择不由向量门槛决定。
- 样本需有固定 ID、查询和历史、关系、scene、medium、前态、期望卡集合、应排除集合及标签。
- 至少 36 正例和12 条件负例；先冻结样本，再执行评分。不得只用 expected IDs 的向量反过来自证命中。
- 返回绑定全版本的阈值与选择指标以及 calibration_id/report_hash。校准数值不改 policy_hash、snapshot_hash 或 bundle_hash；激活冻结报告引用，后续校准另建报告，不就地更改活动包使用的报告。禁止对本包运行 monologue 校准，返回 `monologue_material_absent`。

## 评估

`index evaluate --index ID --samples FILE --mode offline|live --output DIR`

- 新增命令，执行完整性、条件选择、空检索聊天、24段行为、输出检查和运维演练所需的分类结果；按样本类别报告，不把未执行项计为通过。
- offline 使用替代依赖，禁止隐式联网；live 实际调用配置的向量与聊天模型，QQ 结果另行标注以免混淆。
- 行为评审导出逐段产物和评分表，需要后续人工审核。初次生成报告为 pending_review。

`index approve-evaluation --index ID --report FILE --reviewer NAME`

- 新增命令；读取实际执行结果、逐段评分、严重错误、样本哈希与版本绑定，拒绝凭一个 passed=true 通过。
- 生产启用需要全部离线必需门槛和真实编码、生成行为证据。仅 offline 报告允许演练数据库验证，不可用于正常运行的启用。

## 启用与恢复

`index activate --index ID --expected-active ID|none [--reason TEXT]`

`index rollback --index ID --expected-active ID --reason TEXT`

- rollback 新增，两个操作使用同一个目标验证器。旧格式恢复按旧格式完整性、批准及原验收规则，不强求旧包补新卡片报告；不得将旧校准用于新包。
- 先只读验证封存产物和报告；事务中比较 expected-active、更新唯一 active 并写审计。比较不一致返回 `active_version_conflict`，不得先关闭现 active。
- 目标坏文件、配置不匹配、报告过期/缺失、未批准均拒绝；同一版本已 active 的幂等操作仍核对 expected-active。
- 在途轮次持有原包，后续轮次使用新包；操作不清理历史、关系或发送数据。

## 通用退出与诊断

延用现有退出码：成功 0、参数/配置错误 2、未就绪或验收不合格 3、外部或处理异常 4；实现时与现有异常映射统一，不新增矛盾退出语义。结构化输出包括 reason_code、对象 ID、报告路径，不打印完整提示词或密钥。

`doctor --offline` 对当前格式验证必要能力；无独白不阻塞 reply。新包仅核心缺失必须 not-ready，不能作为普通空检索 ready。未知格式拒绝，默认缺格式的历史快照按 semantic_v1 解释。

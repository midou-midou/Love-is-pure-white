# 实施后的启动与验证指南

本文件定义后续实现必须提供的可执行验证路径。当前处于规划阶段，应用、依赖锁文件、测试及
`sana-bot` 命令尚未创建；以下命令需在 `$speckit-implement` 完成相应内容后执行，不能视为本次已运行。
接口与字段以 [contracts/application-cli.md](contracts/application-cli.md)、
[QQ契约](contracts/qq-webhook.md)和[data-model.md](data-model.md)为准。

## 1. 环境与依赖

前提：uv 可用、Python 3.12 可安装或已安装，实施提交包含 pyproject.toml 与 uv.lock。

```bash
cd /Users/midou/GitHub/Love-is-pure-white
uv python install 3.12
uv venv --python 3.12
uv sync --locked --group dev
uv run sana-bot doctor --offline
```

预期：解释器位于 `.venv`，Python 3.12，存储可访问；首次本地模型准备允许下载后固定revision。
离线doctor不调用DeepSeek/QQ，不向云追踪上传内容。未准备模型或索引时明确列出未就绪项，不能假报成功。
实施第一次创建锁文件时使用 `uv lock`，后续复现必须使用 `uv sync --locked`。

## 2. 准备配置与正式资料

维护者在运行环境注入 `DEEPSEEK_API_KEY`、`QQ_APP_ID`、`QQ_APP_SECRET`，不把真实值写入示例或本指南。
本地可 `cp .env.example .env` 填写后以 `uv run --env-file .env <命令>` 注入（`.env` 不入版本控制）。
DEEPSEEK_MODEL、模型revision及运行目录等非敏感配置见契约；首期关键词词表可以明确为空。

```bash
uv run sana-bot index parse --source 'doc/乾纱凪角色设定.docx' --output runtime/structures
uv run sana-bot index review --structure STRUCTURE_ID
```

用命令实际返回的ID替换STRUCTURE_ID、SPLIT_PROFILE_ID和INDEX_ID，保留报告并核对八章、补充约束、列表、字段、示例说话者、
禁用例句和原文定位。索引不得将角色说明当独白，禁止跳过核对后直接激活。

生成的catalog由维护者核对，其中非恋爱二级状态与主情绪映射为推导而非原文；
六类分支须有足够确认样例，不允许自动批准模型自己的推断。

```bash
uv run sana-bot index approve --structure STRUCTURE_ID --catalog runtime/reviewed-catalog.json --reviewer maintainer
uv run sana-bot index calibrate-split --structure STRUCTURE_ID --samples tests/fixtures/approved-calibration.jsonl
uv run sana-bot index build --structure STRUCTURE_ID --split-profile SPLIT_PROFILE_ID --output runtime/indexes
uv run sana-bot index calibrate --index INDEX_ID --stage reply --samples tests/fixtures/approved-calibration.jsonl
uv run sana-bot index activate --index INDEX_ID
uv run sana-bot doctor --offline
```

上述catalog和样本文件是实施/人工核对产物，本次没有虚构其内容。切分profile固定后才构建最终快照，检索校准报告必须绑定其snapshot_hash、样本hash和模型revision；不一致拒绝激活。
无独白素材时monologue_status=disabled_no_material、threshold=null、不创建机会，回复能力验收通过可独立启用。有素材未校准时为disabled_uncalibrated。若要开启独白，在激活前另执行 `uv run sana-bot index calibrate --index INDEX_ID --stage monologue --samples tests/fixtures/approved-monologue.jsonl`；只有真实确认素材及样本通过才能enabled。
改切分参数须重新calibrate-split、build和检索校准；不得复用旧报告或修改活动快照。

## 3. 无账号的自动化验证

```bash
uv run pytest tests/unit tests/contract tests/integration -m 'not live and not embedding'
```

预期：使用独立临时SQLite与索引目录、fake模型和transport，不需API密钥、网络监听或真实等待60秒。
fake clock与seeded RNG可重现计时/概率，输入输出fixture脱离真实聊天身份。

| 场景 | 操作 | 预期 |
| --- | --- | --- |
| 资料结构 | 正式DOCX及长句/多字段fixture导入 | 标题/正文正确，无漏字、无错归、编码不截断 |
| 语义切分 | 确认的话题变化与跨角色问答 | 话题边界和必要关联达SC-013 |
| 状态规则 | 六类组合及触发/恢复，公共场景 | 只应用已批准分支，恋人依赖不越界 |
| 低匹配 | 全低、空候选、等于门槛、异常分数 | 低分疑惑兜底；等于门槛有效；异常独立分类 |
| 输出规范 | 禁句、句末句号、第三人称、代用户言行 | 草稿拦截；改写失败用预审兜底；独白违规不发 |
| 心理独白 | fake clock推进60秒，p=0/1及固定随机序列 | 每轮至多一次，匹配有效且预算足才发 |
| 续聊取消 | 到期前、生成中、提交前注入新消息 | 旧generation取消；已提交HTTP不可撤回边界有记录 |
| 平台预算 | 事件老化、额度不足、拒收、限流 | 不换msg_id绕过，结果可诊断 |
| 原子启用 | 新版本部分失败或检索时切指针 | 保持旧完整索引，在途轮次不混版本 |
| 发送不明 | 注入网络超时及发送后进程中断 | uncertain，不盲重发、不直接加分 |
| 日志 | 低匹配后续3轮、写入失败和30天清理 | 关联正确，无凭据，必要去重与关系仍保留 |

### 手算好感度

在测试fixture配置关键词“谢谢”，权重1/2/5；不能把该测试词表当作正式词表。

1. 第一天，happy正常回复成功，输入含一次“谢谢”：分数8，keyword=1/day=1/happy=1。
2. 同条消息重投：仍为8，所有计数不变。
3. 同日第二轮happy、无关键词且成功：分数13，仅happy再加1。
4. 第二天happy正常轮次含两次非重叠“谢谢”：再加9，总分22；day=2。
5. 兜底、独白、管理操作、明确发送失败：不增加三项计数。
6. 首日消息23:59:59收到、次日才回复：计首日一次；同用户并发消息不重复聊天日。
7. fixture从门槛前进入恰好100：结算后romantic，下一轮生效；关系重启仍保留。
8. confirmed发送后结算失败，重做同delivery事务仅加一次，不再发送；uncertain需证据核对。

## 4. 真实向量与真实模型

向量后端由 `EMBEDDING_KIND` 选择：`local` 用本地 HuggingFace 模型（默认 `BAAI/bge-small-zh-v1.5`，512 维），
`openai_compatible` 用 OpenAI 兼容的远程向量 API（当前为 `BAAI/bge-large-zh-v1.5` @ 硅基流动，1024 维）。

```bash
uv run --env-file .env pytest tests -m live          # 远程向量 + DeepSeek + QQ 鉴权（缺凭据自动跳过）
uv run --env-file .env sana-bot doctor --live-model --qq
uv run pytest tests/integration -m 'embedding and not live'   # 本地后端（需已预下载固定 revision）
```

本地后端测试必须使用预下载且固定revision的真实模型；运行时本地缓存缺失即明确失败，禁止隐式联网下载。
远程后端凭据只经环境注入（`EMBEDDING_API_KEY` 或 `SILICONFLOW_API_KEY`），不写入仓库。
两种后端都不得以fake向量代替质量指标，也不得静默截断超长输入（本地按 tokenizer 计数拒绝，远程依赖服务拒绝）。

**切换后端或模型会改变维度与 revision，必须重建索引并重新校准门槛**；`snapshot_hash` 会随之变化，
旧校准报告自动失效，`index activate` 会拒绝不匹配的报告。

live-model需实际DeepSeek凭据，仅执行小规模smoke，记录模型名/版本配置与验证结果而不输出密钥或推理。
模型限制、结构化结果解析、输出审核失败均应有清晰结果；网络失败不得宣称真实验证通过。

## 5. QQ 官方沙箱

前提：在腾讯开放平台完成应用、单聊能力及沙箱测试用户配置，并提供受支持的HTTPS回调。
用维护者自己的域名部署反向代理，转发到本机127.0.0.1:8000，不在文档中伪造真实公网URL。

```bash
uv run sana-bot doctor --qq
uv run sana-bot serve --host 127.0.0.1 --port 8000
```

在平台配置 `/qq/webhook` 和 C2C 事件，验证挑战成功、非法签名被拒绝；发送普通问候与三轮追问，
确认消息归属、来源message id及发送序号。查看 `/关系` 查询，不应加分或触发独白。

真实默认资料无合格独白时应安静，不是失败。独白端到端验收需额外经用户确认的独白测试资料，
在沙箱配置100%概率、可匹配状态，观察回复后等待一次独白；续聊应取消旧机会。
同时检查事件起算的5分钟窗口、发送余量与额度；平台不允许时跳过并说明原因，不绕过限制。
沙箱配置恢复30%后，用可重现离线1000次概率样本核对SC-007，不能靠付费/真实消息轰炸做统计。

发送失败不加分、unknown不假报、反向代理保留原始body、令牌过期刷新均须留下脱敏证据。
权限或账号缺失时此节标为未执行，不阻止前面离线测试，但阻止上线验收。

## 6. 性能及处理预算

端到端时延从入站持久化开始到QQ确认发送结束，包含排队、事件提取、生成、审核和最多一次改写；正常回复p95≤20秒。模型工作共享30秒总预算，不为每次审核/改写重新计30秒；超预算终止模型工作并走合规失败路径。以fake clock测预算与超时，以记录实际模型/网络条件的受控样本测真实时延；两类证据分别记录，fake延迟不冒充真实p95。

## 7. 最终验收记录

按SC-001–021逐项记录样本数量、预期/实际、资料提供者核对、配置与模型/索引hash。
角色质量用预先确认的场景评审，分数只是检索依据；映射虽已批准，但样本未经提供者确认时不能声称质量达标。
自动化、真实向量后端、真实DeepSeek、QQ沙箱分别报告。通过全部适用门槛后才报告可交付。

---

## 8. 实施状态（2026-10-16 完成后更新）

上述命令已全部实现，可用 `uv run sana-bot <命令> --help` 核对；离线检查如下执行方式：

```bash
uv run pytest tests/unit tests/contract tests/integration -m 'not live and not embedding'
```

覆盖内容：配置原子生效与非法拒绝、SQLite 唯一约束/回滚、日志脱敏与失败降级、每用户串行队列与容量延后、
DOCX 结构与字段分类、角色切分与来源区间往返、向量语义切分与跨角色关联、
输出规范（禁句/句末标点/第一人称/代用户言行）与一次改写、状态事件证据校验与确定性归约、
索引切分校准→固定参数重建→门槛校准→原子启用、QQ 验签/挑战/去重与失败状态码、
基础聊天链路与用户隔离、好感度手算样例与幂等结算、心情管理与切换生效、
独白时机/概率/取消/预算与平台限制、诊断追踪与清理策略、容量与延迟目标。

尚未执行（需要外部条件，未勾选完成）：

- 提供者确认的门槛校准样本（需绑定真实语义单元 id 的正/负样本，替换仓库 fixture）。
- DeepSeek 人物风格与多轮上下文质量验收（需提供者判定；smoke 已通过）。
- QQ 官方沙箱联调（需应用、单聊能力与 HTTPS 回调）。
- 六类状态映射的**提供者样板确认**：映射本身已由维护者「米豆」批准 17/17 条，
  但随仓库发布的 `config/state-catalog.draft.json` 仍是待审草稿（未批准规则不会被执行），
  批准版本按运行目录保存，不提交仓库。
- 真实样本质量指标 SC-001–021 的完整记录见 `validation/results.md`。

依赖复现：`uv lock` 生成 `uv.lock`，后续一律 `uv sync --locked --group dev`。

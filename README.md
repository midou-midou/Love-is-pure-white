# 乾纱凪资料驱动与心情感知聊天

QQ 单聊（C2C）角色机器人：以 `doc/乾纱凪角色设定.docx` 为唯一正式人物设定来源，
经 LangChain 接入 DeepSeek 生成回复与受约束的状态判断，向量模型负责资料语义切分与语境检索。

规格、计划、数据模型与契约见 [specs/001-sana-character-chat/](specs/001-sana-character-chat/spec.md)；
完整启动与验收流程见 [quickstart.md](specs/001-sana-character-chat/quickstart.md)。

## 环境与依赖复现

需要 uv 与 Python 3.12（`>=3.12,<3.13`）。

```bash
uv python install 3.12
uv venv --python 3.12
uv sync --locked --group dev      # 首次创建锁文件时改用 uv lock
# 要用本地向量后端（EMBEDDING_KIND=local）时再加：--extra local-embeddings
uv run sana-bot doctor --offline
```

依赖范围见 `pyproject.toml`，精确解析结果见 `uv.lock`（核心：LangChain 1.x、
langchain-deepseek、NumPy、python-docx、FastAPI、HTTPX、Pydantic 2、cryptography；
可选组 `local-embeddings`：langchain-huggingface、sentence-transformers 5.x；
dev 组：pytest、pytest-asyncio、respx）。本地向量后端是可选的，默认安装不拉 torch。

## 向量后端配置

向量编码有本地与远程两种后端，由 `EMBEDDING_KIND` 选择；两者共用同一适配层，
切换只改配置。**换模型或换后端会改变维度与 revision，必须重建索引并重新校准门槛**。

- `local`（未设置 `EMBEDDING_KIND` 时的默认值）：`HuggingFaceEmbeddings` 加载
  `BAAI/bge-small-zh-v1.5`（512 维，512 token）。需先装可选组
  （`uv sync --extra local-embeddings`，Docker 用 `--build-arg INSTALL_LOCAL_EMBEDDINGS=1`），
  并按精确 commit 预下载缓存、填 `EMBEDDING_REVISION`；运行时缺依赖或缓存直接失败，
  不隐式联网。
- `openai_compatible`：OpenAI 兼容的远程向量 API（纯 HTTPX）。当前配置为
  `BAAI/bge-large-zh-v1.5`（1024 维，512 token）@ `https://api.siliconflow.cn/v1`。
  需要 `EMBEDDING_API_KEY`（`SILICONFLOW_API_KEY` 为兼容别名）与 `EMBEDDING_BASE_URL`。

注：DeepSeek 只提供聊天接口，**没有向量接口**（`/embeddings` 返回 404），
因此“对话用 DeepSeek”与“向量用 DeepSeek”不能并存，向量须另行选择提供方；
本地后端可为完全离线部署提供退路（无需任何向量服务凭据）。

密钥只经运行环境注入，仓库内 `.env.example` 保持为空模板：

```bash
uv run --env-file .env sana-bot doctor --offline --live-model --qq
```

真实凭据相关检查与向量探测见 [validation/results.md](specs/001-sana-character-chat/validation/results.md)。

## 索引格式与切分方式

当前支持两种索引格式，读路径并存，旧格式继续可用：

| 格式 | 来源 | 切分 | 用途 |
| --- | --- | --- | --- |
| `semantic_v1` | 正式 DOCX | 相邻句向量判边界（`semantic`） | 既有线上版本与恢复路径 |
| `structural_v2` | `doc/乾纱凪角色设定整理版/` 六份 Markdown | 一张完整卡一个切分单元（`markdown_cards_v1`） | 新资料包：常驻 + 按条件检索 |

`structural_v2` 不按长度切分完整卡：01 核心设定、02 输出规范与 03 状态总则每轮常驻，
其余 26 张卡中的 25 张各编码一次；吃醋三级与恢复机制、多轮示例都保持在同一张卡内。

按文档结构切分的早期实现曾因正例 top1 只有 2/24 被移除；本次的 `markdown_cards_v1`
不是对旧实现的恢复，而是以「不可拆分的完整规则卡」为前提重建，验收口径见
[specs/002-rebuild-character-rag/validation/results.md](specs/002-rebuild-character-rag/validation/results.md)。

## 已实现的命令

统一入口 `uv run sana-bot`，默认输出 JSON，退出码 0 成功 / 2 输入或配置错误 /
3 资料或校准未就绪 / 4 外部服务失败。

| 命令 | 作用 |
| --- | --- |
| `doctor --offline` | 配置、存储、向量缓存与索引就绪检查（不打印凭据） |
| `doctor --live-model` / `--qq` | 真实模型、QQ 账号条件检查；缺条件时标「未执行」 |
| `serve --host 127.0.0.1 --port 8000` | 单实例服务：`/qq/webhook`、`/healthz`、`/readyz` |
| `index parse --source P --output D [--format docx\|markdown-package]` | 解析 DOCX 或资料包配置为不可变结构与核对材料 |
| `index review --structure ID` / `--index ID` | 标题树、字段归属、缺失、状态与负例清单（资料包显示逐卡条件） |
| `index approve --structure ID --catalog P --reviewer N` | 登记结构与 catalog 批准（不等于校准通过）；资料包传包配置 |
| `index calibrate-split --structure ID --samples P` | 旧 `semantic_v1`：固定切分参数并给出 profile |
| `index build --structure ID --output D [--split-profile ID]` | 按固定参数构建快照；资料包禁用 `--split-profile` |
| `index calibrate --index ID --stage reply --samples P` | 校准检索门槛，绑定实际 snapshot_hash；资料包拒绝 monologue |
| `index approve-calibration --calibration ID --reviewer N` | 批准与快照绑定的校准报告 |
| `index evaluate --index ID --samples P --mode offline\|live --output D` | 完整性、选择、空检索、行为段与输出检查，产物供人工评审 |
| `index approve-evaluation --index ID --report R --reviewer N` | 按逐轮产物与必要门槛批准报告，拒绝伪 `passed=true` |
| `index activate --index ID --expected-active ID\|none [--reason R]` | 校验批准、校准、报告与封存产物后原子启用 |
| `index rollback --index ID --expected-active ID --reason R` | 重启校验后恢复仍合格的旧版本，业务数据不变 |
| `state show/set --user ID [...]` | 查看/设置该用户活动会话的合法状态 |
| `relationship show --user ID` | 分数、恋爱状态、三项累计与配置版本 |
| `config validate/apply --file P` | 校验并原子应用运行配置 |
| `logs query/prune` | 诊断检索与 30 天正文清理 |
| `delivery review --id ID --result confirmed\|rejected --evidence P` | 仅处理 uncertain 发送 |

## 权限与部署

- 管理命令只在本地或受控运维环境执行；服务不暴露管理 HTTP 接口，QQ 普通用户只可查询自己的 `/关系`。
- 真实运行需要 HTTPS 回调：用维护者自己的域名做反向代理，转发到 `127.0.0.1:8000`，
  并保持原始 body 字节不变（签名校验依赖 raw body）。
- 密钥只从环境注入（`DEEPSEEK_API_KEY`、`QQ_APP_ID`、`QQ_APP_SECRET`），
  `.env.example` 只提供空模板；日志与诊断不记录密钥、prompt 与模型推理。
- 本地开发推荐：`cp .env.example .env`，填入真实值后用 `uv run --env-file .env <命令>`。
  `.env` 已被 `.gitignore` 忽略；**不要把真实值写回 `.env.example`**（该文件会被提交）。

```bash
uv run --env-file .env sana-bot doctor --offline --live-model --qq
```

真实检查会区分「未执行」与「通过」：缺少凭据时标为未执行，不会假报成功。
沙箱回调与消息发送仍需 HTTPS 回调与沙箱用户，见
[validation/official-qq.md](specs/001-sana-character-chat/validation/official-qq.md)。
- 云链路追踪默认关闭；`runtime/` 不提交，仅服务账号可读。

## 运维与恢复

- 启动时：`sending` 状态发送记为 `uncertain`；未处理且未过期的入站重新入队；待发独白取消；
  短期状态覆盖清空（回到 neutral，长期关系不受影响）。
- 素材或切分参数变化必须重新执行 `calibrate-split → build → calibrate → activate`，
  旧 `snapshot_hash` 的校准报告会被拒绝。
- 资料包（`structural_v2`）的准入门是 `parse → approve → build → calibrate →
  approve-calibration → evaluate → approve-evaluation → activate`；任一环节缺失都不能启用。
  仅 `offline` 报告只允许在隔离演练库切换（`--allow-offline-rehearsal`），
  正常运行必须使用真实编码与生成的 `live` 报告。
- 切换与恢复先只读重算封存文件与报告，再在短事务内比较 `--expected-active`；
  不一致返回 `active_version_conflict`，原活动版本保持有效，历史/关系/发送数据不被改写。
- 资料包没有独白素材，`monologue_status=disabled_no_material`：不创建独白机会，
  但不阻塞普通回复。
- 诊断正文默认保留 30 天，`logs prune` 只清理正文，关系账本与去重凭据长期保留。
- 输出审核：待审文本用 `<待审核输出>` 分隔标注，违规项只接受固定代码
  （`user_mind_guess` / `abusive` / `over_control`，资料包模式另有四项）；
  协议不符会重试一次，仍不符按「审核未完成」拒发。审核未通过使用独立的受控提示
  （「刚才那句不算，我重新说」），不再复用「我不明白」，避免把审核问题显示成检索未命中。
- 第一人称规则允许自报姓名（「我叫乾纱凪」「叫我纱凪就好」），只拦截「纱凪觉得…」这类第三人称叙述。
- 独白默认等待 60 秒、概率 30%；无合格独白素材时保持 `disabled_no_material` 且不创建机会。

## 目录

| 路径 | 说明 |
| --- | --- |
| `src/sana_bot/` | 应用包：入口薄适配，领域流程可用 fake 依赖独立验证 |
| `config/` | 经核对的非敏感词表、分支与状态目录（`state-catalog.draft.json` 为待审草稿） |
| `runtime/` | 不提交：SQLite、向量快照、模型清单、核对与校准报告 |
| `doc/`、`live2d/`、`vrm/` | 既有资料，保留不改动 |

## 本地聊天台（开发用，可选）

不想配公网回调时，可以在浏览器里直接和 Bot 对话：

```bash
uv run --env-file .env sana-bot serve --port 8000        # 终端 A
uv run --env-file .env python tools/chat_server.py       # 终端 B → http://127.0.0.1:8100
```

[tools/chat_server.py](tools/chat_server.py) 在服务端用 `QQ_APP_SECRET` 签名后把消息投给本机
`/qq/webhook`，再只读查询运行库把回复交给 [tools/chat.html](tools/chat.html) 显示；
密钥不下发到浏览器。消息会走完整的真实链路（验签 → 去重 → 排队 → 向量检索 → DeepSeek →
输出规范 → 发送），但 `openid` 不是真实沙箱用户时 QQ 平台会拒绝发送，
页面会明确标注「未送达」，不会伪装成成功。页面上每条消息都可用「查看检索」看到真实命中的
语义单元与分数（含门槛对比），便于判断是资料命中还是低匹配兜底。

## 验证

```bash
uv run pytest tests/unit tests/contract tests/integration -m 'not live and not embedding'
```

真实远程向量、真实 DeepSeek 与 QQ 沙箱验收需凭据，单独执行（缺凭据自动跳过）：

```bash
uv run --env-file .env pytest tests -m live
```

结果记录见 [specs/001-sana-character-chat/validation/results.md](specs/001-sana-character-chat/validation/results.md)
与 [specs/002-rebuild-character-rag/validation/results.md](specs/002-rebuild-character-rag/validation/results.md)，流程见 quickstart。

普通测试完全离线：使用替代编码器、聊天模型与 QQ transport，不下载模型、不读取账号、不产生付费调用。
资料包的真实编码、真实 DeepSeek 评审与 QQ 沙箱联调需凭据，未执行项在验证记录中明确标注，不记为通过。

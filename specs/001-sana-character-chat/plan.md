# Implementation Plan: 乾纱凪资料驱动与心情感知聊天

**Branch**: `main`（实际分支；setup 返回的逻辑标识为 `001-sana-character-chat`） | **Date**: 2026-10-16 | **Spec**: [spec.md](spec.md)

**Input**: `specs/001-sana-character-chat/spec.md`，43 项需求及宪章 v1.1.0。

## Summary

实现 Python 单服务 QQ C2C 角色机器人：正式 DOCX 先做标题/列表/字段结构解析，
再做角色归属与向量语义切分，构建可核对、可追溯的本地索引。
LangChain 编排 DeepSeek 对话，明确规则筛选与低匹配兜底，所有发送经过人物规范校验。
情绪按已批准状态规则动态转换，长期好感度按用户持久化；沉默独白通过有资格限制的被动回复发送。

本计划只完成设计，不创建应用代码、依赖环境或 tasks.md。具体决策与官方依据见 [research.md](research.md)。

## Technical Context

**Language/Version**: Python 3.12，`>=3.12,<3.13`；uv 管理 `.venv` 与锁文件。

**Primary Dependencies**: LangChain 1.x、langchain-deepseek、langchain-huggingface、
Sentence Transformers、NumPy、python-docx、FastAPI/Uvicorn、HTTPX、Pydantic 2、cryptography。
向量后端可选本地 HF 模型或 OpenAI 兼容的远程向量 API（纯 HTTPX，无需额外 SDK）。
具体候选范围见 research，实施首步 `uv lock` 后验证，尚未宣称实际兼容。

**Storage**: SQLite WAL 存元数据、消息、状态、关系与日志；文件系统保存不可变索引快照和向量矩阵。

**Testing**: pytest、pytest-asyncio、respx；离线 fake clock/fake RNG/fake model/QQ transport；
真实向量后端校准和带凭据的 DeepSeek/QQ 沙箱 smoke 分开执行。

**Target Platform**: macOS 本地开发；Linux CPU 单实例服务，公网 HTTPS 回调，经反向代理接入。

**Project Type**: Python package + 管理 CLI + Webhook 服务，无管理网页。

**Performance Goals**: 初版 100 个关系档案、10 个并发活跃用户、每秒 5 条入站；
热模型情况下本地检索 p95 ≤200ms，回调落盘 ACK p95 ≤1s，正常回复端到端 p95 ≤20s 为验证目标；
模型整体处理预算 30s，超预算走合规失败路径，平台发送预算另行核对。

**Constraints**: 仅官方 QQ C2C；保守 5 分钟被动回复窗口、每来源用户消息不超过 4 次，
默认正常回复和独白各一条；文档硬约束不能被语义分数覆盖；实际动作/动画不开发。
默认独白等待 60 秒、概率 30%；好感度权重 1/2/5、恋爱门槛 100，均版本化可配置。

**Scale/Scope**: 单份已提供设定，先按 ≤10,000 个片段设计；单进程用户串行调度，
不引入向量数据库、分布式队列或训练系统。容量超出目标时明确拒绝或延后，不静默丢消息。

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| 原则 | Phase 0 初检 | Phase 1 复检与证据 |
| --- | --- | --- |
| Python 3 | 通过：Python 3.12 | 通过：版本范围、验证路径明确 |
| uv 环境依赖 | 通过：只规划 uv 管理 | 通过：quickstart 使用 locked sync，环境由实施创建 |
| LangChain | 通过：ChatDeepSeek 与 Embeddings 集成 | 通过：编排与适配器分离，无模型绕过主流程；向量后端可本地或远程 API，共用同一抽象层 |
| QQ 入口分离 | 通过：C2C 薄适配器 | 通过：contracts 分离 QQ wire、应用事件与管理命令 |
| 官方规范优先 | 通过：先调研官方能力 | 通过：research 记录日期、URL、5/60 分钟矛盾及保守资格检查 |
| 密钥及失败处理 | 通过：运行配置注入 | 通过：日志脱敏、拒收/限流/未知发送结果独立处理 |

设计无未获批准的宪章例外。实际发布门槛仍包括账号权限、完整资料校准、状态映射批准及真实联调；
这些未满足时不得上线或宣称验收通过。没有改写宪章或悄悄修改既有规格。

## Project Structure

### Documentation (this feature)

```text
specs/001-sana-character-chat/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── checklists/requirements.md
└── contracts/
    ├── qq-webhook.md
    ├── document-index.md
    └── application-cli.md
```

`tasks.md` 由后续 `$speckit-tasks` 生成。

### Source Code (repository root)

以下为实施目标结构，尚未创建：

```text
pyproject.toml
uv.lock
.python-version
.env.example
src/sana_bot/
├── config.py
├── cli.py
├── app.py
├── adapters/qq.py
├── ingest/                 # DOCX结构、角色、语义切分、核对清单
├── retrieval/              # 版本化向量编码、过滤、匹配与校准
├── dialogue/               # LangChain编排、输出验证、兜底
├── state/                  # 情绪规则reducer与关系计分
├── storage/                # SQLite事务、去重、快照仓储
└── scheduling/             # 用户串行队列、发送记录、沉默机会
config/                     # 经核对的词表、分支与规则映射
runtime/                    # 不提交：SQLite、向量、模型清单、核对报告
tests/
├── unit/
├── contract/
├── integration/
└── fixtures/
```

**Structure Decision**: 单 Python 包，入口薄适配，领域流程可用 fake 依赖独立验证；
现有 `doc/` 与 `live2d/` 保留，Live2D 不进入实现范围。

## Complexity Tracking

无宪章违例，不引入例外复杂度。SQLite+NumPy 支持当前规模，不提前建设分布式系统。

## Phase 0 — Research

已生成 [research.md](research.md)。明确官方 QQ 接入、模型分工、解析优先级、状态批准、
校准算法、日志策略及不可消除的发送结果不确定性。具体账号凭据不是文档内容。

## Phase 1 — Design

设计顺序为：

1. 固定 Python/依赖和向量后端模型与 revision，建立离线验证入口。
2. 正式 DOCX 结构化并完成字段/类型分类，核对标题树与目录，然后角色切分；先校准切分参数，再重建固定参数的片段与向量快照。
3. 建立 SQLite 实体/约束，在实际快照上校准回复与可用独白门槛；报告与snapshot_hash一致才原子启用。无独白资料禁用该能力，普通聊天独立启用。
4. 实现有限事件标签与原文证据的提取器，再接批准规则reducer、每用户长期计分和统一输出规范。
5. QQ 验签/挑战/鉴权与持久入站队列；模型生成完成后统一发送、结算及创建独白机会。
6. 使用隔离测试验证时序、并发、计分和不合规拦截，再执行真实服务验收。

入站流程：验签 → 去重落盘/使旧独白失效 → ACK → 用户串行处理 →
事件提取与批准规则转换/冻结快照 → 过滤检索/低匹配判断 → 生成或疑惑兜底 → 输出规则验证 → 发送资格检查 → 发送 →
确认成功后事务计分与独白机会。网络结果不明进入 uncertain，不能冒充成功。

独白流程：到期且无续聊 → 最新关系/状态/场景核对 → 合法独白候选 → 一次概率抽取 →
生成与输出检查 → 用户锁内最终取消/预算核对 → 单次发送；无候选/低匹配/失败不扰民。
当前正式设定不是独白剧本，索引若无合法独白，真实环境保持安静。

对应设计：[data-model.md](data-model.md)、[contracts/](contracts/)、[quickstart.md](quickstart.md)。

## Requirement Coverage

| 需求 | 设计落点 | 主要验证 |
| --- | --- | --- |
| FR-001–002 | QQ适配、Conversation/Turn | C2C契约、三轮上下文及隔离 |
| FR-003–007 | 文档分类、规则过滤、合规生成 | 来源与语境匹配、说明非独白 |
| FR-008–010 | StateSnapshot与批准reducer | 动态状态、心情与关系组合 |
| FR-011–012 | 失败分类、文本动作 | 无凭据泄漏、不执行动作 |
| FR-013–017 | MonologueOpportunity与消息预算 | fake clock/RNG、续聊取消、重启不补发 |
| FR-018–023 | CalibrationProfile、DiagnosticEvent | 门槛边界、日志完整性、访问与保留 |
| FR-024–028 | 结构/角色/语义多层索引 | 覆盖与关联、编码失败不启用 |
| FR-029–035 | Relationship、AffectionEvent | 加权、日期、并发去重、重启恢复 |
| FR-036–039 | RuleNode、StateRule | 正式标题字段、六类分支核对 |
| FR-040–043 | 全局规范与统一输出验证 | 禁句与语义审核、拦截、核对报告 |

## Release Gates

- 依赖真实锁定、编码不截断、文档核对覆盖通过；六类状态映射获批准。
- 固定切分参数重建后的snapshot_hash与已启用能力校准报告一致；无独白时disabled_no_material、threshold=null、不创建机会，普通聊天可独立启用。
- 端到端p95≤20秒从入站持久化到QQ确认发送，包含排队、事件提取、生成、审核/一次改写；整体模型处理30秒预算耗尽时终止模型工作并走合规失败路径。
- 指标 SC-001–021 有样本集与结果；有限样本通过不能替代持续质量审查。
- QQ 官方接口依据在实施/发布时复核；有可用账号与 HTTPS 回调并通过沙箱。
- 新配置不重算历史分数；日志清理不删除必要去重凭据；未决发送结果已可核对。
- 所有硬规范失败均拒发草稿；所有平台限制都在发送前及失败处理时执行。

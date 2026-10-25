# Implementation Plan: 基于整理版角色设定重建分层 RAG 索引库

**Branch**: Git 当前为 `main`，本轮未创建或切换分支；Spec Kit 逻辑功能名为 `002-rebuild-character-rag` | **Date**: 2026-10-25 | **Spec**: [spec.md](spec.md)

**Input**: `specs/002-rebuild-character-rag/spec.md`

## Summary

将整理版六份内容文件导入为不可变角色包，完整保存 26 张卡：状态总则常驻，剩余 25 张按条件引用；01、02 全文另作常驻材料。00、07 只参与维护。新包采用已有预留格式 `structural_v2`，切分策略 `markdown_cards_v1`；保留旧 `semantic_v1` 读写与恢复路径，不把结构表存在误认为功能已经实现。

每轮固定一个完整包，先检查关系和场景，再根据对话状态选择规则、检索背景与示例。将常驻约束、条件规则、背景和示例分层送入现有 LangChain 对话流程；新包空检索时仍可普通交流，处理失败不能伪装成空检索。新包独白能力为 `disabled_no_material`。索引启用前验证完整性、条件选择和对话表现，切换与恢复不写用户关系账本。

## Technical Context

**Language/Version**: Python >=3.12,<3.13；本地解释器 3.12.14，实施时将 `.python-version` 固定为 3.12.14，使用 uv 管理 `.venv`。

**Primary Dependencies**: 沿用 uv.lock：LangChain 1.4.6、langchain-core 1.6.11、langchain-deepseek 1.1.1、langchain-huggingface 1.2.3、sentence-transformers 5.7.0、NumPy 2.5.4、FastAPI 0.143.2、HTTPX 0.28.1、Pydantic 2.14.1。Markdown 使用受限语法解析，无需引入通用 Markdown 渲染器。

**Storage**: 沿用 SQLite WAL 与不可变磁盘快照、NumPy 向量；补齐既有 structural_* 仓储，新增包来源关联及常驻材料存储，不另引入向量数据库。

**Testing**: pytest、pytest-asyncio、respx；普通验证使用替代编码器、聊天模型及 QQ transport，真实模型与平台验证单独记录；固定评估集与人工行为审阅。

**Target Platform**: 现有本地 macOS 开发及 Linux 部署环境，QQ 官方 C2C 文本入口；索引管理仅本地 CLI。

**Project Type**: 既有 Python 服务及 CLI 的增量功能。

**Performance Goals**: 达成规格 SC-001～SC-008；25 张候选卡在内存做余弦排序，不额外增加重排模型调用；每轮规则选择复用已有状态分析结果。维护者按诊断在 3 分钟内定位来源。真实延时作为观察指标，不虚构当前基线。

**Constraints**: 常驻内容及完整卡不截断、不自动摘要、不拆分；精确检查模型输入容量；无明确独白素材；不读取或输出密钥；同轮不可混版本；真实 QQ 发送仍受既有授权与保守发送预算限制。

**Scale/Scope**: 一名角色、六份内容文件、两份排除材料、3 项常驻材料、25 张可选卡。核心文件与全部卡完整封存，按卡检索不按句窗重切。既有用户数量与部署拓扑不扩展。

## Constitution Check

*Phase 0 前检查与 Phase 1 后复核均通过，无需宪章例外。*

| 宪章约束 | 设计及验证 | 前检查 | 后复核 |
| --- | --- | --- | --- |
| Python 3 | 沿用 3.12 范围，实施固定 3.12.14 | PASS | PASS |
| uv 管理 | 使用现有锁文件与本地环境；不新增依赖产品 | PASS | PASS |
| LangChain | 复用 DeepSeek 适配及消息编排，编码器保持配置驱动 | PASS | PASS |
| QQ 入口隔离 | CLI 导入与独立测试不依赖 QQ，现有适配层继续处理协议 | PASS | PASS |
| 官方平台约束 | 查阅官方消息概述，保持现有 C2C 被动回复及限制，不扩展主动消息 | PASS | PASS |
| 凭据与失败路径 | 诊断仅存引用和原因；覆盖生成、编码、验签、限流、拒发与不确定发送 | PASS | PASS |
| 可验证交付 | 离线与真实结果分别留证，未知授权不宣称已获权 | PASS | PASS |

官方来源、查阅日期和差异记录见 [research.md](research.md)。本轮未登录平台核对应用实际权限，真实启用必须完成 [quickstart.md](quickstart.md) 的授权核对。

## Project Structure

### Documentation (this feature)

```text
specs/002-rebuild-character-rag/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── checklists/requirements.md
└── contracts/
    ├── character-package.md
    ├── index-cli.md
    └── turn-context.md
```

`tasks.md` 留给后续 `$speckit-tasks`，本轮不生成。

### Source Code (repository root)

```text
config/character-package.json                  # 新增显式文件清单和已核对条件策略
src/sana_bot/
├── ingest/markdown_package.py                 # 新增受限 Markdown 解析与完整卡构建
├── ingest/package_models.py                   # 新增包、来源、常驻及卡模型
├── ingest/service.py                          # 格式分发，保留 DOCX 语义入口
├── storage/index_schema.py                    # 新增迁移，复用预留 structural_* 表
├── storage/index_repository.py                # 双格式快照及激活/恢复
├── storage/package_repository.py              # 新增包来源与完整卡仓储
├── retrieval/package_selector.py              # 新增条件优先选择与排除证据
├── retrieval/vector_store.py                  # structural_v2 装载及完整性验证
├── retrieval/service.py                       # 新格式检索，隔离旧行为
├── dialogue/{history,prompts,service,match_decision,output_policy}.py
├── state/{catalog,models,reducer,service}.py   # 版本绑定及条件证据，不改好感算法
├── cli.py / cli_index.py / cli_doctor.py       # 管理契约与就绪检查
└── ports.py                                   # 版本化上下文与选择结果
tests/
├── unit/                                      # 解析、计数、条件、输出检查
├── contract/                                  # 包格式、CLI、消息与版本契约
├── integration/                               # 导入、选择、聊天、切换恢复
└── fixtures/character_package/                 # 固定正负例与样本清单
```

**Structure Decision**: 复用现有层次和接口，新增包专用模型与选择器，旧语义格式独立分发。预留 structural_v2 表通过增量迁移补多来源、常驻和版本关系，不删除或重建业务表。

## Phase 0 Research Decisions

已完成代码证据、依赖及官方容量行为核对。决定采用完整卡向量、批准的条件映射、版本绑定的状态目录、无命中正常交流以及完整包校验。详见 [research.md](research.md)。全部设计未知项已解决；真实容量测量、凭据可用性和生成质量属于实施验收，不能作为已经通过的事实。

## Phase 1 Design

1. 导入明确六文件清单；解析 H2 卡和续行，验证元数据及来源，保存原始字节与定位，生成 3 常驻项和 25 可选卡。
2. 复用 structural_v2，一个卡对应一个 group/field/leaf，`token_split=0`；多来源通过关联表记录，不只留下合成来源。
3. 构建时计数全部实际编码输入，包括标题和条件前缀；向量只针对 25 张卡。常驻独立测生成预算。超限失败并输出卡与模型限制，不沿用旧语义拆分器。
4. 维护者核对自然语言 scope 到条件策略及状态目录的映射；批准后冻结。关系限定和私密条件先筛，状态规则由有效状态加载，背景与示例再按相关程度取舍，示例最多 2 张。
5. 01、02、状态总则常驻，当前状态完整卡及适用场景规则作为角色行为配置；背景和示例作为有类型资料。任意检索正文或用户消息不能升级为配置。
6. 每轮固定 index_id、bundle_hash、policy_hash 和目录版本；关系不匹配则受控失败，不强制恋人、不覆盖账本；没有常驻材料也失败。规则状态载入必须独立于向量调用成功与否，但编码异常仍走错误路径而非伪造成功。
7. 同一验证器服务 activate/rollback，校验完整包、来源、编码器配置、批准、校准和行为报告。事务仅做预期活动版本比较、单 active 更新及审计，不执行网络或长评估。

字段与状态见 [data-model.md](data-model.md)，输入、管理和本轮选择契约见 contracts。

## Verification and Delivery Gates

- 资料门：3 常驻项、26 卡及来源 100% 覆盖；00/07 不进入生成内容；四类故障全部阻止启用。
- 选择门：固定至少 36 正例与 12 条件负例，相关参考命中率 >=90%，不合条件采用为 0。实际选择与校准使用相同策略。
- 对话门：24 普通未命中用例至少 23 个相关回复；12 类行为各两段三轮，至少 22/24 段通过，严重错误为 0；所有输出格式要求通过。
- 运维门：五类切换演练及业务数据不变核对通过；5 个问题的维护定位均 <=3 分钟；双用户隔离、重复事件、鉴权失败、模型失败、限流、发送失败与 uncertain 仍受现有约束。
- 发布顺序：离线验证 → 真实编码及 DeepSeek 评估 → 维护者批准报告 → 显式启用 → QQ 沙箱验收。离线通过不构成生产激活资格；QQ 验收失败立即恢复上一完整版本并留证。

## Complexity Tracking

无宪章违规。复用现有存储、框架与平台，不引入新服务。双格式分发用于保留旧快照恢复能力；新增来源关联是多文件追溯的必要部分。

## Planning Validation

已检查七份设计产物的标题、代码围栏、内部链接与未替换占位符，并复核规格 FR-001～FR-022 和成功标准的设计覆盖。研究复核修正了校准数值与封存哈希的循环依赖、快照状态与现有枚举不一致，以及报告执行模式命名。真实容量、行为质量和 QQ 授权均未测试，不能据本轮文档检查启用索引。

规划前后均未发现 `.specify/extensions.yml`，无 before_plan 或 after_plan hook 可执行。当前 Git 为 main，setup_plan 返回的逻辑 BRANCH 为 002-rebuild-character-rag，不表示已创建同名 Git 分支。

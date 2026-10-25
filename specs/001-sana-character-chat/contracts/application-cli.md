# 应用接口、配置和管理 CLI 契约

本文件定义未来实现的命令与应用边界。当前只存在规划文档；命令尚未实现。
管理命令限运行主机授权账号使用，不映射为QQ任意用户可调用的管理接口。

## 管理命令

统一入口 `uv run sana-bot`，默认输出JSON（服务日志除外），失败退出非零。

| 命令 | 输入 | 输出与副作用 |
| --- | --- | --- |
| doctor | --offline 或 --live-model / --qq | 配置、存储、编码及可选真实连接检查，绝不打印凭据 |
| index parse | --source PATH --output DIR | 不可变STRUCTURE_ID、类型节点及核对报告；不生成可启用快照 |
| index calibrate-split | --structure ID --samples PATH | 已确认样本上的切分profile及hash；不修改结构/旧索引 |
| index build | --structure ID --split-profile ID --output DIR | 按固定profile重新生成片段/向量，返回INDEX_ID及snapshot_hash；不自动启用 |
| index review | --structure ID 或 --index ID | 标题树、字段归属、缺失、状态/负例清单 |
| index approve | --structure ID --catalog PATH --reviewer NAME | 核对过的结构/catalog及hash批准记录；不等于校准通过 |
| index calibrate | --index ID --stage reply/monologue --samples PATH | 绑定实际snapshot_hash的该阶段门槛与报告；失败不启用对应能力 |
| index activate | --index ID | 校验完整+结构/切分批准+回复校准，及所有enabled能力报告hash，原子激活；独白禁用不阻止聊天 |
| state show/set | --user ID [--mood ... --tag ... --scene ...] | 查看/设置该用户活动会话合法状态；不改好感度 |
| relationship show | --user ID | 分数、状态、三项累计与配置版本 |
| config validate/apply | --file PATH | 检查权重/词表/门槛/概率/时长，原子应用新版本 |
| logs query | --user ID / --event ID / --since TIME / --reason VALUE | 有限前后文与诊断；权限仅维护者 |
| delivery review | --id ID --result confirmed/rejected --evidence PATH | 仅处理uncertain，有官方证据并审计；confirmed后幂等结算 |
| logs prune | --older-than-days 30 | 清理诊断正文，不删除关系与必要去重信息 |
| serve | --host 127.0.0.1 --port 8000 | 单实例服务与任务处理器，不向公网暴露管理接口 |

本机ID与openid分别命名，CLI --user使用内部UserIdentity.id，避免泄露外部身份。
index命令基于库版本返回ID；退出码0成功、2输入配置错误、3资料/校准未就绪、4外部服务失败。

## 普通用户

QQ正常文本进入对话，保留 `/关系` 为只读查询，返回自己的好感度及恋爱状态。
查询不触发模型、计分、独白机会或情绪变化；它作为新入站同样取消旧独白。
查询文案也遵守句末无句号等适用格式。未授权用户输入管理命令不能产生状态或配置写入。

## 配置

| 配置 | 初版值或规则 |
| --- | --- |
| DEEPSEEK_API_KEY / QQ_APP_ID / QQ_APP_SECRET | 仅运行时环境注入，仓库仅提供空模板 |
| DEEPSEEK_MODEL | deepseek-flash，可配置；对接上线前核对厂商当前可用模型 |
| DEEPSEEK_BASE_URL | https://api.deepseek.com，仅维护者配置 |
| EMBEDDING_MODEL / EMBEDDING_REVISION | 本地：BAAI/bge-small-zh-v1.5 / 精确commit必填；远程：模型id / 留空（revision自动记为 model@host） |
| EMBEDDING_KIND | local(默认) 或 openai_compatible；远程需同时给出 API_KEY、BASE_URL、DIMENSION |
| EMBEDDING_API_KEY / EMBEDDING_BASE_URL | 仅运行时环境注入，仓库仅提供空模板；SILICONFLOW_API_KEY 作为兼容别名 |
| EMBEDDING_DIMENSION / EMBEDDING_MAX_TOKENS | 本地 512/512；远程按模型（bge-large-zh-v1.5 为 1024/512）。实际维度不符即报错，不静默接受 |
| RUNTIME_DIR | runtime，不提交数据库/对话/凭据 |
| AFFECTION_WEIGHTS | keyword=1,day=2,happy=5，正数且happy严格最大 |
| ROMANCE_THRESHOLD / KEYWORDS | 100 / 初始空列表，不杜撰用户词表 |
| MONOLOGUE_WAIT_SECONDS / PROBABILITY | 60 / 0.30，正时长，p∈[0,1] |
| QQ_PASSIVE_WINDOW_SECONDS / SAFETY_MARGIN | 300 / 10，变更需官方证据及验收 |
| MATCH_THRESHOLDS | 切分来自批准SplitCalibrationProfile；回复/独白来自绑定当前snapshot_hash的CalibrationProfile，不设通用数值默认 |
| MONOLOGUE_STATUS | enabled/disabled_no_material/disabled_uncalibrated；禁用threshold=null且不创建机会 |
| DIAGNOSTIC_RETENTION_DAYS | 30，保持原有计分账本与去重凭据 |

公开配置不含密钥，修改有版本和生效时间；非法配置保持旧版本。

## 应用内接口与结果

- IngestService：source → staged manifest、typed nodes、review issues。
- Retriever：query+StateSnapshot+index_id+stage → qualified candidates、cosine scores、reason。
- StateEventExtractor：当前消息+批准事件目录 → 标签+原文证据位置，解析失败/无证据返回no_event，禁止返回直接状态/计分指令。
- StateReducer：当前状态+已批准事件证据+关系/场景 → 有效快照或冲突原因；不接受任意模型写入。
- DialogueService：message+history+snapshot+context → normal/fallback/error草稿与来源，不直接发QQ。
- OutputPolicy：draft+source constraints+state → approved text 或拒绝原因；多轮修订最多一次。
- QQTransport：content+source_msg_id+msg_seq+user_key → confirmed/rejected/uncertain；无加分逻辑。
- AffectionService：confirmed normal delivery → exactly-once本地结算或待恢复错误。
- MonologueService：opportunity+clock+RNG → cancelled/skipped/delivery，支持测试注入。

所有服务错误有reason_code、correlation_id；内部诊断不作为角色回复原样发出。
模型返回只能包含允许的事件/回复字段，不能包含可执行管理操作。

# 手机猫娘（Project N.E.K.O.）代码结构与关键链路分析

> 分析对象：`/Users/midou/GitHub/手机猫娘`  
> 分析日期：2026-09-23  
> 方法：以源码、项目内架构文档和运行时协议为证据，重点追踪 VRM 动作、表情、语音说话、LLM 生成四条端到端调用链。

## 1. 结论摘要

该项目是一个“浏览器角色前端 + 三个 Python 服务 + 多供应商模型/语音适配层”的桌面陪伴应用。主进程不是单体逻辑：Main Server 负责 UI、WebSocket、每角色会话与外部 TTS；Memory Server 管持久记忆；Agent Server 管可选工具任务。核心会话控制器 `LLMSessionManager` 通过 mixin 组合生命周期、流输入、回合、TTS、主动消息等能力。

四条重点链路彼此解耦，但以同一助手回合和 `speech_id`/`turnId` 关联：

1. LLM 生成先输出流式文本。
2. TTS 同时消费文本增量，输出 48 kHz 单声道 PCM。
3. 浏览器在回合结束后调用独立情绪模型，将完整回复分类为情绪，再映射到 VRM 表情。
4. VRM 动作运行时监听回合生命周期、文本更新和情绪结果，在本地做语义分析、姿态规划和 VRMA 播放。
5. 浏览器播放音频时以 Web Audio `AnalyserNode` 驱动五元音口型，因此“说话口型”不依赖情绪模型。

这意味着：动作、表情、语音并非由一个统一的“表演计划”原子提交，而是多条异步链最终在前端汇合。设计灵活、供应商可替换，但也带来时序一致性和重复解析成本。

## 2. 项目代码结构

| 目录 | 主要职责 | 关键入口/文件 |
|---|---|---|
| `app/main_server/` | Main Server 启动、FastAPI 应用、角色运行时 | `__main__.py`, `web_app.py`, `character_runtime.py` |
| `app/memory_server/` | 记忆服务 HTTP API 与后台整理 | `__main__.py` |
| `app/agent_server/` | Agent 任务评估、执行、ZeroMQ 桥 | `__main__.py` |
| `main_routers/` | Main Server REST/WebSocket 路由 | `websocket_router.py`, `system_router/emotion.py` |
| `main_logic/core/` | `LLMSessionManager` 及会话、回合、TTS、主动消息 mixin | `manager.py`, `lifecycle.py`, `streaming.py`, `turn.py`, `tts_runtime.py` |
| `main_logic/omni_offline_client/` | 文本/图像会话，OpenAI-compatible/Anthropic/Gemini SDK 流式生成 | `_client.py`, `_streaming.py`, `_tools.py` |
| `main_logic/omni_realtime_client/` | 实时语音会话、原生音频、ASR 与工具调用 | `_client.py`, `_transport.py`, `_responses.py` |
| `main_logic/tts_client/` | 外部 TTS worker 选择、队列协议、供应商实现 | `__init__.py`, `_infra.py`, `workers/` |
| `utils/llm_client/` | 统一聊天模型客户端抽象与工厂 | `factory.py`, `openai_client.py`, `anthropic_client.py` |
| `memory/` | 最近记忆、事实、反思、人格、召回、证据链 | `recent.py`, `facts.py`, `reflection/`, `persona/` |
| `static/vrm/` | VRM 加载、渲染、动作、表情、口型、VMC | `vrm-manager.js`, `vrm-animation.js`, `vrm-expression.js`, `motion/` |
| `static/app/` | 浏览器 WebSocket、音频、聊天适配与生命周期事件 | `app-websocket.js` 等 |
| `frontend/react-neko-chat/` | React 聊天界面 | `src/` |
| `templates/` | 服务端 HTML 外壳和模型/情绪配置页面 | `index.html`, `vrm_emotion_manager.html` |
| `plugin/` | 插件 SDK、运行时和用户插件服务 | `sdk/`, `server/`, `message_plane/` |

### 2.1 进程边界

- Main Server `:48911`：浏览器 HTTP/WebSocket、每角色 `LLMSessionManager`、外部 TTS。
- Memory Server `:48912`：对话写入、召回、事实/反思/人格等持久状态。
- Agent Server `:48915`：能力状态和任务执行；另在线程中托管用户插件 HTTP `:48916`。
- Main 与 Agent 之间以 ZeroMQ `:48961/:48962/:48963` 传递生命周期、可靠分析请求和任务结果。

源码依据：`docs/architecture/index.md:3-38`、`app/main_server/character_runtime.py`、`main_logic/core/manager.py:50-150`。

## 3. VRM 模型动作控制

### 3.1 组件分层

动作系统分为五层：

1. **生命周期桥**：`static/vrm/motion/bridge.js` 监听用户文本、助手回合开始/结束、流式文本闭合阶段、情绪就绪、语音取消，并通过页面事件/BroadcastChannel 转发统一消息。
2. **动作运行时**：`motion/runtime.js` 维护活动回合、情绪持续态、角色画像、动作所有权以及初始化状态。
3. **语义分析器**：`motion/core.js` 使用 `semantics.json` 中的多语言短语、否定、假设、姿态和动作规则，把对话文本转为最多三个动作决策。它是规则/证据驱动，不是每回合调用 LLM。
4. **动作播放器**：`motion/player.js` 读取 `manifest.json` 的动作资产元数据，按意图、强度、角色风格、当前姿态选择 VRMA，并串行执行计划。
5. **VRMA 执行器**：`vrm-animation.js` 加载 `.vrma.gz`，生成 AnimationClip/AnimationAction，处理 VRM 版本、骨骼根节点、四元数同半球对齐、crossfade 和 SpringBone。

### 3.2 核心数据结构和控制策略

- `activeTurn` 聚合一轮中的用户文本、助手文本、正式情绪和阶段状态。
- `emotionState` 不是一次性事件，带 `expiresAt`、`pulseUntil`、连续证据计数，用于让情绪身体动作持续一段时间。
- `MotionPlayer` 维护 `posture`、`phase`、`restAsset`、`poseAsset`、`queueGeneration`。
- `queueGeneration` 是取消/抢占令牌；异步加载完成前后都检查世代，防止过期动作覆盖新动作。
- 动作资产标注进入/退出姿态；坐/躺状态下若无兼容短动作，系统宁可跳过，也不会无故站起。
- `externalPlaybackOwners` 为点唱机/教程等外部播放提供所有权，避免与自动待机/对话动作争抢同一个 mixer。
- 最终播放通过 `window.vrmManager.playVRMAAnimation()` 下沉到 `VRMAnimation.playVRMAAnimation()`。

### 3.3 动作数据流

`用户/助手事件 → NekoMotionBridge → activeTurn 聚合 → MotionCore.analyze()/analyzeSpeech() → 动作计划（≤3）→ MotionPlayer.select() → 姿态兼容与队列调度 → VRMAnimation → Three.js AnimationMixer → humanoid bones`。

正式情绪结果还会映射为身体意图，例如 `happy → happy`、`surprised → surprise`、`sad → sad`、`fearful → overwhelm`，与文本动作证据共同影响计划。

### 3.4 重要观察

- 优点：动作决策完全在前端本地完成，延迟低、无额外模型成本；资产选择具备姿态和人格约束；异步世代控制较严谨。
- 风险：动作语义规则、情绪分类枚举和模型表情枚举分散在多个文件，新增情绪/动作时容易出现不完整映射。
- 风险：动作系统依赖 DOM CustomEvent 和 BroadcastChannel 的隐式协议，类型约束弱；桥接事件字段变化主要靠回归测试守护。
- 建议：为 `motion_lifecycle`、动作决策和播放结果定义共享 JSON Schema/TypeScript 类型，并增加“输入文本 + 情绪 → 确定动作计划”的黄金用例。

源码依据：`static/vrm/motion/bridge.js:6-195`、`motion/runtime.js:9-85,121-180`、`motion/core.js:695-1757`、`motion/player.js:713-760,963-1152`、`vrm-animation.js:669-759`。

## 4. VRM 表情控制

### 4.1 情绪识别链路

助手回合结束时，`static/app/app-websocket.js` 的 `finalizeAssistantTurn()` 获取完整回复，剥离音乐指令，然后异步调用 `window.analyzeEmotion(fullText)`，设置 5 秒超时。成功后：

- `window.applyEmotion(emotion)` 更新当前头像；
- 发出 `neko-assistant-emotion-ready`，供动作系统和反应气泡消费。

`analyzeEmotion` 对应 Main Server 的情绪分析 API；`main_routers/system_router/emotion.py` 从配置读取独立的 `emotion` 模型槽位并调用小模型。它与主对话模型配置分离。

### 4.2 VRM 表情映射与混合

`VRMExpression` 是运行时表情控制器：

- `loadMoodMap(modelName)` 读取模型专属 emotion mapping；
- 默认情绪集合映射到 VRM expression 名称/权重；
- `setMood()` 设置目标 mood，`update()` 每帧平滑插值权重；
- 自动眨眼由独立逻辑维护；
- `neutralReturnTimer` 支持非持续表情回归 neutral；
- `setBaseExpression()` 支持直接设置基础 expression。

配置 UI 位于 `templates/vrm_emotion_manager.html`，通过 `/api/model/vrm/emotion_mapping/{model}` 读写模型情绪映射。默认示例在 `static/vrm/configs/sister1.0_emotion.json`。

### 4.3 表情与口型的关系

情绪表情和说话口型最终都写入同一个 VRM `expressionManager`，但职责分开：

- `VRMExpression` 管 mood、基础表情、眨眼和权重平滑；
- `VRMAnimation` 的 lip-sync 每帧覆盖 `aa/ih/ou/ee/oh` 口型；
- 五元音写入时会显式清零未激活元音，防止待机 VRMA 的口型轨道残留叠加。

### 4.4 重要观察

- 情绪分类发生在完整文本生成之后，因此情绪表情通常晚于文字首屏和 TTS 首包；动作运行时通过等待窗口和 neutral fallback 缓解不同步。
- 情绪 API 超时/失败只记录警告，不阻塞回合收尾，属于合理的表现层降级。
- 主对话回复会被浏览器再次发送给情绪模型，产生额外延迟与费用，也使浏览器成为情绪编排责任方。
- 建议：让后端在主模型流完成时并行产出结构化情绪元数据，或在服务端统一触发情绪分类，并在 WebSocket 回合协议中携带 `emotion` 和置信度；前端仅负责映射和渲染。

源码依据：`static/app/app-websocket.js:1472-1539`、`main_routers/system_router/emotion.py:880-960`、`static/vrm/vrm-expression.js:108-341,424-627`、`templates/vrm_emotion_manager.html:815-1385`。

## 5. 语音说话与口型同步

### 5.1 两条语音输出路径

系统明确区分：

- **实时模型原生音频**：语音会话且当前供应商/声线支持原生音频时，由 `OmniRealtimeClient` 直接接收音频。
- **项目外部 TTS**：文本会话必走；自定义/克隆声线或选定外部供应商的语音会话也走该路径。

`LLMSessionManager._resolve_session_use_tts()` 决定路径，不是简单按“文本/语音”二分。

### 5.2 外部 TTS 管线

1. LLM 文本 delta 进入 `handle_text_data()`，经过 Markdown/括号舞台指令清洗、空白规范化。
2. `_enqueue_tts_text_chunk()` 将 `(speech_id, text)` 写入线程安全请求队列；未 ready 时先进入 `tts_pending_chunks`。
3. `get_tts_worker()` 结合核心供应商、显式 TTS provider、voice metadata、克隆信息、凭证和优先级选择 worker。
4. WebSocket 双向流 worker（Qwen/Step/CosyVoice 等）直接推增量；HTTP 按句 worker（OpenAI/Gemini/MiniMax/MiMo/Doubao 等）使用 `SentenceBuffer` 和共享并发骨架。
5. worker 将不同源采样率重采样为 48 kHz、mono、signed int16 little-endian PCM，写入响应队列。
6. `tts_response_handler()` 异步消费，`send_speech()` 先发送 `{type:"audio_chunk", speech_id}` JSON 头，再发送二进制 PCM 帧。
7. 前端按 `speech_id` 调度 Web Audio 播放，并发出 `voice_play_start/end` 作为真实播放边界。

控制哨兵：`(None,None)` 完成当前话语但保留 worker；`__interrupt__` 中止当前合成并屏蔽晚到回调；`__shutdown__` 退出线程；soft flush 用于空闲时刷出尾句。

### 5.3 口型同步

前端播放链创建 `AnalyserNode` 后调用 `vrmManager.animation.startLipSync(analyser)`：

- 优先使用 `FormantLipSyncAnalyzer` 从频谱估算五元音权重；
- 分析器不可用时降级为低/中频能量控制 `aa` 开口度；
- 每个渲染帧在 AnimationMixer 更新后写入 expression，覆盖动作文件残留口型；
- 停止播放时将五元音权重全部归零。

### 5.4 重要观察

- 优点：供应商层统一 worker 签名与 PCM 合同，路由与凭证解析绑定，避免跨供应商误用 Key。
- 优点：`speech_id`、中断哨兵和世代守卫处理了旧音频晚到、切会话和用户抢话等关键竞态。
- 风险：外部 TTS 使用线程 + `queue.Queue`，Main 会话本身又是 asyncio；桥接逻辑复杂，测试必须覆盖 teardown/respawn/interrupt。
- 风险：响应队列无文档化硬上限，极慢客户端或供应商突发音频可能形成内存压力；应结合实现确认并设置有界队列/丢弃或背压策略。
- 建议：把音频头、PCM 格式、完成/中断事件形成版本化 wire contract；增加端到端延迟指标（首文本、首音频、首口型、播放结束）。

源码依据：`main_logic/core/tts_runtime.py:286-448,1013-1123,1198-1254,1477-1569,1717-1950`、`main_logic/tts_client/__init__.py:274-498`、`main_logic/tts_client/_infra.py:309-767`、`static/vrm/vrm-animation.js:864-1048`。

## 6. 大模型生成

### 6.1 会话协调层

`LLMSessionManager` 是每角色实例，通过 mixin 组合：

- `LifecycleMixin`：启动、热切换、结束、清理和熔断；
- `StreamingMixin`：文本/图片/PCM 输入、缓存和背压；
- `TurnMixin`：转录、输出文本、回合完成、中断和消息镜像；
- `TtsRuntimeMixin`：语音路由与 worker；
- 工具、主动消息、通知、ASR 等其他 mixin。

文本模式创建 `OmniOfflineClient`，音频模式创建 `OmniRealtimeClient`；切模式会重建客户端。启动依赖 Memory Server 返回新对话上下文，随后绑定带“预期 session”守卫的回调，避免旧客户端晚到事件污染新会话。

### 6.2 离线文本生成链

1. WebSocket `stream_data(text/image)` 进入 Main Server。
2. 未 ready 的有序文本/图片暂存在 `pending_input_data`；图片触发视觉模型路由。
3. `OmniOfflineClient.stream_text()` 组合 system instructions、历史、当前文本、主动截图、插件图像和用户附件。
4. `create_chat_llm()` 按 `provider_type` 创建 OpenAI-compatible 或 Anthropic 客户端；Gemini 可走原生 Google GenAI SDK。
5. `_astream_with_tools()` 支持多轮工具调用，工具结果追加回历史后继续生成。
6. 流式 chunk 经过 thinking/tool 泄漏过滤、前缀处理、长度/重复保护，回调至 `LLMSessionManager`。
7. `TurnMixin` 同时把文本推送给浏览器、TTS、记忆/插件总线，并在完成时发出 turn end。

### 6.3 实时语音生成链

浏览器通常上传 48 kHz mono PCM；会话输入路径降噪/转换为 16 kHz 后交给 `OmniRealtimeClient`。实时客户端维护供应商 WebSocket、响应仲裁、ASR transcript、文本/原生音频回调及工具调用。若声线路由要求外部 TTS，则使用模型的文本输出而非原生音频。

### 6.4 上下文、视觉和热切换

- Memory Server 上下文是启动依赖，失败进入标准启动失败/熔断路径。
- 图片按来源分配预算，超限依次归一化、抽样、压缩、最后丢弃；视觉模型可使用独立 endpoint/Key。
- endpoint 变化时清理历史中供应商私有 `tool_calls[].extra_content`，避免把 Gemini thought signature 发给其他厂商。
- 长会话可预热同模式 pending client；切换时原子提升，并把切换窗口内的 16 kHz 用户输入缓存刷入新客户端。

### 6.5 重要观察

- 优点：提供商抽象、流式工具调用、视觉路由、热切换和回调身份守卫较完整。
- 优点：图像所有权与失败回滚逻辑细致，避免异步 await 间附件被错误消费或丢失。
- 风险：`OmniOfflineClient._streaming.py` 与 `_lifecycle.py` 体积大、状态密度高，主动消息、用户消息、工具轮和图像队列共享对象，理解/修改成本高。
- 风险：历史中混有供应商私有字段，当前靠切 endpoint 时清洗；建议将内部规范消息与 provider wire payload 完全分层。
- 建议：把“构造本轮输入”“执行 provider stream”“工具循环”“输出治理”“提交历史”拆成显式阶段对象，并为每阶段定义不可变输入输出。

源码依据：`main_logic/core/manager.py:50-150`、`core/lifecycle.py:853-2413`、`core/streaming.py:102-480`、`omni_offline_client/_client.py:53-250`、`omni_offline_client/_streaming.py:526-1100`、`utils/llm_client/factory.py:29-180`。

## 7. 四模块协同时序

```text
用户输入
  └─ WebSocket → LLMSessionManager → Offline/Realtime LLM
                                  ├─ text delta → 浏览器聊天/字幕
                                  ├─ text delta → 外部 TTS（需要时）→ PCM → 浏览器播放
                                  │                                      └─ Analyser → VRM 五元音口型
                                  └─ response done → turn end
                                                        ├─ 情绪小模型 → applyEmotion → VRMExpression
                                                        └─ MotionBridge → 文本/情绪语义计划 → VRMA → 骨骼
```

关键关联键：

- `turnId/requestId`：关联用户输入、助手回合、动作语义上下文和情绪事件。
- `speech_id`：关联 TTS 文本、PCM chunk、完成标记、中断和浏览器播放。
- session generation / expected session：隔离旧会话异步回调。
- motion `queueGeneration`：隔离旧动作加载和播放任务。

## 8. 建议优先级

### P0：协议和可观测性

- 为 WebSocket 消息、motion lifecycle、TTS queue item 建立版本化 schema 和生成类型。
- 统一记录 `turnId + speech_id + session generation`，打通首 token、情绪完成、首音频、播放完成、动作开始的 tracing。

### P1：表演编排一致性

- 将服务端生成的情绪元数据、前端动作计划、语音播放状态汇总为显式 `PerformanceTurn` 状态机。
- 明确 mood expression、口型 expression、VRMA expression track 的混合优先级和所有权。

### P1：LLM 管线拆分

- 把超大的流式方法拆成输入组装、provider 调用、工具循环、输出治理、提交五个阶段。
- 内部消息使用供应商无关模型；仅适配器构造 thought signature/reasoning content 等 wire 字段。

### P2：动作资产与规则工具化

- 为 `manifest.json`、`semantics.json` 提供静态校验：资产存在、姿态可达、情绪枚举完备、多语言规则一致。
- 建立确定性动作规划快照测试与 VRMA 兼容性批测。

## 9. 图表索引

| 模块 | 架构图 | 数据流图 |
|---|---|---|
| VRM 动作控制 | [HTML](diagrams/vrm-motion-architecture.html) / [JSON](diagrams/vrm-motion-architecture.json) | [HTML](diagrams/vrm-motion-dataflow.html) / [JSON](diagrams/vrm-motion-dataflow.json) |
| VRM 表情控制 | [HTML](diagrams/vrm-expression-architecture.html) / [JSON](diagrams/vrm-expression-architecture.json) | [HTML](diagrams/vrm-expression-dataflow.html) / [JSON](diagrams/vrm-expression-dataflow.json) |
| 语音说话 | [HTML](diagrams/speech-architecture.html) / [JSON](diagrams/speech-architecture.json) | [HTML](diagrams/speech-dataflow.html) / [JSON](diagrams/speech-dataflow.json) |
| 大模型生成 | [HTML](diagrams/llm-architecture.html) / [JSON](diagrams/llm-architecture.json) | [HTML](diagrams/llm-dataflow.html) / [JSON](diagrams/llm-dataflow.json) |

HTML 图为独立可交互文件，支持缩放、搜索、主题切换和导出；JSON 是可维护的图源。

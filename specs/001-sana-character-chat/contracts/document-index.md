# 文档解析、状态目录与索引契约

输入 `doc/乾纱凪角色设定.docx`；先输出不可变结构与类型节点，再输出staged索引、标题树、片段清单、状态目录、
约束目录、校准报告、manifest。源文件只读；文档内容不具有执行工具或修改系统指令的权限。

## 结构解析顺序

1. 枚举OOXML正文顺序，保留段落index、字符区间、列表level与原文；表格若出现按单元格保留路径。
2. 根据可见中文章节编号、状态序号和上下文识别逻辑标题；不依赖Heading样式。正文中的数字不自动升格。
3. 同级/上级标题终止作用域，列表子项继承当前标题；明确冒号字段细化其父节点。
4. 字段词典区分trigger、overall、action、behavior、speech、recovery、attribute、example、negative。
   一段多个字段可产生多个节点，来源char_range不丢失。
5. 示例内“你：/纱凪：”切分角色发言；仅实际对话无名内容为monologue。
6. 同一适用范围内执行Embedding语义边界识别，形成SemanticUnit与跨角色context_links。

源片段无损覆盖正文；上下文关联可重叠，原文覆盖统计按源区间并集，避免重复关联掩盖漏字。
空白与结构标记可忽略，但须在导入报告区分，不将句末标点从原文中删除。

## 节点逻辑字段

每节点至少包含：id、source_hash、heading_path、paragraph/char_range、parent_id、
content_type、speaker、raw_text、scope、conditions、provenance、review_status。

conditions：relationship=any/non_romantic/romantic、scene=any/public/private、
primary_moods、secondary_tags、trigger/exit事件集合。只要有明确限制就必须同时满足。

全局输出约束、禁区与通用性格为mandatory规则集。negative_example只可作为禁止匹配证据，
不可作为回复召回内容；示例台词是表达参考，输出不得机械复读。

## 状态目录及待审映射

| 主情绪 | 恋爱分支 | 非恋爱分支（derived） |
| --- | --- | --- |
| happy | 日常轻松分享、害羞局促、依赖软萌（独处） | 礼貌愉快、动物话题温柔、被夸别扭害羞 |
| angry | 羞恼炸毛、轻/中度吃醋 | 不满直率、被调侃羞恼、排斥轻浮言行 |
| sad | 重度吃醋自卑失落、独处委屈脆弱、不安收敛 | 受挫沉默、压力低落、自尊防备 |

neutral对应文档平稳常态；羞恼保留not_genuine_anger标签；吃醋不能因恋爱而管束用户。
恋人分支来源是第四章，六类主情绪分类映射本身需要核对；非恋爱依据第二章与输出规则推导。
`approved=false` 的细化规则不能被状态reducer执行；文档明确的恋人限定硬限制即使映射未批也始终生效。

每转换包含from、event、to、scene/relationship guard、source_node_ids、priority、exit；
明确证据匹配且唯一才转换，冲突保持合法旧态。允许标签叠加，dominating_state唯一。
用户可以通过对话触发已批准规则，不能要求模型跳过关系或改目录。

## 索引清单与启用

构建顺序固定：parse与类型分类 → 人工核对结构/目录 → calibrate-split → 按固定profile执行build（重新生成片段/向量） → calibrate回复/可用独白 → activate。预览切分不是可启用快照。

manifest至少包含 source_hash、structure_hash、parser_version、split_profile_hash、splitter_parameters、embedding_model_id、
embedding_revision、dimension、catalog_hash、snapshot_hash、reply_calibration_id、monologue_calibration_id、monologue_status、file_hashes、build_status。
snapshot_hash不含校准报告，覆盖结构、目录、切分参数、模型revision、片段与向量文件；manifest另记录报告hash，避免报告绑定快照时出现循环hash。

- 精确model revision和依赖锁定后产生候选，禁止静默换模型或截断文本。
- 角色、场景、关系为硬过滤；语义检索在可用候选上执行，禁区不靠语义召回。
- no_material（无真独白）与empty_candidates（筛后无候选）、below_threshold、processing_error分开。
- review报告列明八章及补充约束覆盖、字段歧义、六类分支、未确认规则和原文往返定位结果。
- source_hash、catalog或切分参数改变后生成新版本，不覆盖旧快照；先重建再校准，旧检索报告失效。activate校验结构/切分批准、回复校准以及所有已启用能力报告的snapshot_hash一致。
- monologue_status为enabled/disabled_no_material/disabled_uncalibrated。无真独白时threshold=null且不创建机会，普通聊天可独立启用；有素材未校准时仍禁用。只有匹配本快照的已批准独白报告才允许enabled。
- 从禁用切到启用必须新版本；不为历史轮次补建机会。切为禁用或资料失效时取消旧待发机会。
- 不完整版本不能启用；有旧完整版本则继续使用，否则readyz失败，说明资料不可用。

## 质量与回归

SC-012/013/019覆盖结构与语义切分；SC-020覆盖分支；SC-021覆盖输出。
普通回复或切分校准样例不足时报告失败并要求补齐人工确认样本；独白样例不足只禁用独白能力，不阻止其余已验收能力；不能把模型自评分数当提供者确认。
固定Embedding revision、词表和阈值下结果可复现；报告不声称当前已实际构建。

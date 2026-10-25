# 角色资料包输入与封存契约

## 显式配置

新增 `config/character-package.json`，内容由实施阶段按本契约生成并核对，不由聊天模型写入。必需字段：

```json
{
  "format_version": "structural_v2",
  "chunking_strategy": "markdown_cards_v1",
  "character": "sana",
  "required_relationship": "romantic",
  "source_root": "doc/乾纱凪角色设定整理版",
  "included_files": [
    {"path": "01_核心设定.md", "role": "core"},
    {"path": "02_输出规范.md", "role": "output"},
    {"path": "03_情绪状态.md", "role": "emotion"},
    {"path": "04_场景行为.md", "role": "scene"},
    {"path": "05_背景资料.md", "role": "background"},
    {"path": "06_对话示例.md", "role": "example"}
  ],
  "excluded_files": ["00_加载说明.md", "07_整理记录与待配置.md"],
  "resident_ids": ["sana_core", "sana_output", "sana_emotion_general"]
}
```

完整配置另含 `card_conditions`、`state_catalog`、`budgets`、`retrieval_policy` 和明确版本。未知字段报错。路径相对项目根目录解析，source_root 内的文件路径不允许逃逸，禁止递归纳入未声明文件。

## 受限 Markdown 语法

- UTF-8 文本，保留原始字节哈希与正文；01、02 的标题和完整正文都保留，不按 H2 打散常驻。
- 03 至 06 的 H2 是卡边界；紧随标题的五个连续 `key: value` 行分别包含 id/type/character/source/scope，均非空。
- 五字段顺序可以不同，重复字段、未知字段、缺字段报错。元数据块之后为空行和正文；正文里的冒号不是新元数据。
- 文档级 H1 和前言是文件语境，不能变成无说话者独白；06 前言的示例性质附到每张示例，05 卡中的未知事实声明保留。
- 同包 ID 唯一，不同包允许复用。正文位置为原始文件中的行范围与字符半开区间。
- 示例 `用户：`、`角色：` 或半角冒号开启一轮，后续未带说话者行属于上一轮；开头未能确认说话者的内容报歧义，不猜心理独白。保留空行、括号动作与轮次，不能只留角色答案。
- 03 中 `sana_emotion_general` 必须标为 emotion_policy，仅常驻。其他允许类型由数据模型规定，未知类型拒绝。

## 完整性与容量

当前包基线：7 个 emotion 节点（1 总则+6 状态）、6 scene、6 background/appearance/profile、7 example，共 26 卡，25 张编码卡。新增或减少需产生新结构核对结果，不长期把 26 写成所有未来包的硬限制。

编码输入格式版本固定为标题、类型、scope 和完整卡正文，字符不截断。精确 tokenizer 绑定模型 revision，特殊 token 与前缀必须纳入计数。超限或缺可信容量证明拒绝 build/activate；不得自动拆 leaf、摘要或改模型。3 常驻项不需要向量，却必须参与生成预算和 bundle_hash。

## 输出产物

快照目录含 `manifest.json`、六份来源副本、解析后的常驻与完整卡文件、批准 profile/policy/catalog、编码器清单及 `vectors.npy`。所有文件相对路径及 SHA256 进入封存清单。manifest 中不保存密钥、带凭据的 URL 或本机 .env 内容。00/07 可在维护记录登记版本，但不复制为生成载荷。

校验器从实际产物重算来源、结构和向量哈希，不只比较 manifest 自称值；DB 记录必须匹配。评估报告是旁路不可变产物，引用 bundle_hash，不反向改变封存快照。

## 错误分类

`package_file_missing`、`package_metadata_invalid`、`duplicate_card_id`、`unknown_card_type`、`package_scope_unmapped`、`source_path_escape`、`card_input_over_limit`、`tokenizer_unverified`、`resident_missing`、`bundle_hash_mismatch`。错误包含文件、标题或卡 ID、位置、期望与实际；不泄露凭据。

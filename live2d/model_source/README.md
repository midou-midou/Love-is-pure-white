# Live2D 模型源素材包

本目录是根据参考图重新绘制的 Live2D Cubism 建模底稿，不包含来源图片的直接裁切。

## 文件

- `character_master.png`：透明背景、正面中立站姿的全身主立绘。
- `textures/facial_parts_sheet.png`：眼睛、闭眼、眉形、口型、腮红与泪光的补充材质。
- `layer_manifest.json`：建议的 PSD 图层顺序、标准参数 ID 与变形器结构。

## Cubism 制作流程

1. 以 `character_master.png` 为视觉基准，在 Photoshop/Krita 中按 `layer_manifest.json` 拆分并补画被遮挡区域。
2. 将表情素材表中的部件分别裁入对应图层，保持左右眼、眉和嘴的中心位置一致。
3. 以 4096×4096 图集导入 Cubism Editor，建立 ArtMesh 与三角网格。
4. 按清单创建头部、身体、眼睛、嘴、头发、手臂和呼吸参数。
5. 在 Cubism Editor 中执行纹理集成并导出 `.moc3`、`.model3.json`、物理与姿势文件。

## 重要说明

`.moc3` 是 Live2D Cubism Editor 的编译产物，必须在 Cubism Editor 内完成网格、参数、变形器和物理绑定后导出。本素材包提供的是可继续建模的美术源文件与绑定规范，不伪造不可用的 `.moc3` 文件。


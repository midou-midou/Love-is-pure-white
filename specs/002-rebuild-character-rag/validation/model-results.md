# 真实模型验证（T060）

状态：**未执行**。

## 前置条件

- 配置 `EMBEDDING_KIND=local` 且 `EMBEDDING_REVISION` 固定为具体 commit，并预下载该 revision 的向量模型；
  或配置可逐条核对的 `openai_compatible` 后端。
- 配置 `DEEPSEEK_API_KEY`，并获授权进行真实生成。

## 实际执行记录（2026-10-25）

```bash
sana-bot index build --structure <STRUCTURE_ID> --output <dir> --runtime-dir <dir>
```

输出：

```json
{"status": "error", "reason_code": "embedding_revision_missing",
 "message": "本地向量模型必须提供精确 EMBEDDING_REVISION（commit）"}
```

退出码 3。构建在写事务之外失败，未产生快照，也未改变任何 active 版本。
按规格，缺少可信计数时不得降门槛：未改用启发式计数或批次 usage 冒充逐卡证明。

## 尚未取得的证据

| 项目 | 状态 |
| --- | --- |
| 25 张完整卡的真实逐卡 token 计数 | 未执行 |
| 真实编码器的模型 ID / revision / 维度记录 | 未执行 |
| 真实 25 行向量矩阵与封存 `bundle_hash` | 未执行 |
| 真实 DeepSeek 逐轮生成（48 个评估样本） | 未执行 |
| 24 段行为人工逐段评分与严重错误计数 | 未执行 |
| SC-004 / SC-005 / SC-006 真实结论 | 未取得 |

## 替代依赖下的证据（不等同于真实结论）

见 [results.md](results.md) 第 4–6 节：使用字符 n-gram 话题向量与确定性替代聊天模型，
验证了流水线、条件筛选、预算与输出检查的行为；该结果只允许在隔离演练库使用，
`index activate` 会以 `package_offline_only` 拒绝正常运行的启用。

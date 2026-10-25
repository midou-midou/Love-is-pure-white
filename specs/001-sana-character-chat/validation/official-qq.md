# 发布前官方 QQ 接入复核

**状态：部分执行（凭据与鉴权已实测通过；沙箱回调与消息发送未执行）**

已实测（2026-10-16）：`POST https://api.bot.qq.com/app/getAppAccessToken` 返回 HTTP 200，
字段为 `access_token` 与 `expires_in`；其中 **`expires_in` 为字符串数字**（实测 `"4458"`），
实现必须按字符串兼容解析，否则会把有效凭据误判为失败。

本文件记录 2026-10-16 已核对的官方依据与仍未取得的外部条件。复核未完成前不得上线，
也不得在验收记录中声称已通过平台联调。

## 已核对的官方依据（2026-10-16）

| 主题 | 链接 |
| --- | --- |
| 接入入口 | https://bot.q.qq.com/wiki/develop/api-v2/ |
| C2C 事件 | https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html |
| 消息概述 | https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html |
| 发送单聊消息 | https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html |
| 鉴权凭证 | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html |
| 事件投递 | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html |
| 签名 | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html |

## 文档矛盾与采用的保守口径

- 消息概述与发送接口概述写「C2C 被动回复 60 分钟、每条用户消息最多 4 次」，
  同页 `msg_id` 参数描述仍写 5 分钟。
- 采用保守交集：`deadline = 用户消息事件时间 + 300 秒`，发送需至少剩余 10 秒，
  每条来源消息最多 4 次；正常回复与独白各占 1 条。
- 不采用「独白计时开始后再有 5 分钟」的解释，也不改成无授权主动推送。

## 实现侧已具备的能力（离线可验证）

- `/qq/webhook` 读取原始 body，用 `X-Signature-Ed25519` 与 `X-Signature-Timestamp` 验签；
  `op=13` 返回同一 `plain_token` 与拼接签名；签名无效 401、结构非法 400、无法持久化 503。
- 消费 `d.id` 作为来源消息 id、`d.author.user_openid` 作为用户、`d.content` 作为文本；
  外层事件 id 仅用于幂等键。
- 鉴权通过 AppID/AppSecret 获取 access_token，检查 HTTP 状态与业务 code，并按 `expires_in` 缓存刷新。
- 发送 `POST /v2/users/{user_openid}/messages`，`msg_type=0`，`Authorization: QQBot ACCESS_TOKEN`；
  HTTP 成功但业务失败不算送达；网络不明记 `uncertain`；不更换 `msg_id`/`msg_seq`。

## 尚未执行（本文件不得标记为通过）

- [x] 凭据注入与官方凭证接口取 token（实测通过，仅记录指纹）
- [ ] 平台应用、单聊能力与沙箱测试用户配置
- [ ] 受支持的 HTTPS 回调 + 反向代理（保持原始 body）
- [ ] 挑战验证与非法签名拒绝的沙箱实测
- [ ] 正常问候与三轮追问的消息归属、来源 message id、发送序号核对
- [ ] `/关系` 查询不触发模型、不加分、不创建独白机会的沙箱确认
- [ ] 事件时间起算 5 分钟窗口、发送余量与四次额度的平台实测
- [ ] 拒收、无权限、429 限流与令牌过期刷新的实测证据
- [ ] 独白端到端验收（需经提供者确认的独白测试资料，沙箱配置 100% 概率）

以上任一缺失时，本节结论保持「未执行」。

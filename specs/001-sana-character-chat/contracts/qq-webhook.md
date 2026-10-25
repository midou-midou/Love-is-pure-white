# QQ C2C Webhook 与发送契约

核对日期：2026-10-16。外部协议以 [官方事件文档](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)、
[签名](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html)、
[C2C事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html)、
[C2C发送](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)为准。

## 入站 HTTP

`POST /qq/webhook`，公网HTTPS，反向代理保持原始body字节不变；仅配置的app_id生效。

- dispatch回调必须验证 `X-Signature-Ed25519` 与 `X-Signature-Timestamp`，用官方算法处理 timestamp原文+raw body。
- Bot Secret非空，按官方规则重复补齐/截取32字节生成Ed25519 seed，禁止将access token当签名seed。
- `op=13` 为官方地址验证：读取d.event_ts与d.plain_token，返回同一plain_token及两者拼接的签名hex。
  challenge单独路由分支，不要求它提供dispatch才有的字段，不产生对话或计分；不将仅有X-Bot-Appid当dispatch认证。
- `op=0,t=C2C_MESSAGE_CREATE` 才进入普通文本消息流程；消费d.id、d.author.user_openid、d.content、d.timestamp。
- 签名无效返回401；结构非法400；无法持久化则503，不能先ACK后丢失事件。
- 持久落盘或已识别重复消息返回HTTP200及 `{"op":12}`；本服务目标p95≤1s，不等待模型。
- 有效但不支持的事件ACK并标ignored，不调用模型；不支持媒体给合规文本提示，不进入正常计分。
- 健康端点 `GET /healthz` 仅返回进程状态；`GET /readyz` 表示存储、完整索引与配置就绪，不返回密钥或用户内容。

归一化应用事件：event_key、app_id、user_key、message_id、event_time、received_at、text、payload_hash。
外层id是event_key输入，d.id才是source_msg_id；重复event或message只处理一次。

## 鉴权与出站

凭据接口 `POST https://api.bot.qq.com/app/getAppAccessToken`，body为appId、clientSecret。
读取access_token/expires_in并缓存，错误同时检查HTTP与业务code；不记录secret或token。

实测（2026-10-16）：成功响应 HTTP 200，字段为 `access_token` 与 `expires_in`，
且 **`expires_in` 为字符串数字**（实测 `"4458"`）。解析必须兼容字符串与数值两种形式，
非正数或不可解析时明确失败而不是缓存不可用令牌；诊断只记录字段名与 token 短指纹。
刷新余量为 60 秒（`expires_in` 内提前刷新），业务 code 非 0 一律视为失败。
接口依据：[获取凭证](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html)。

C2C发送使用 `Authorization: QQBot ACCESS_TOKEN`。应用端构造字段：

```json
{
  "content": "（稍稍偏过头）……我有点没明白？",
  "msg_type": 0,
  "msg_id": "SOURCE_MESSAGE_ID",
  "msg_seq": 1
}
```

此为待预审的兜底示例格式，不作为正式资料台词。正常与独白正文都在发前通过统一校验。
每个source_msg_id分配稳定序号：第一条常规回复1、独白2；重试或核对不得换号制造新消息。
API返回message id才标confirmed；HTTP成功但业务失败不能算送达。

## 被动发送资格

[官方消息概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)与发送页
概述给出60分钟/4次，msg_id参数却保留5分钟描述。初版采用保守交集：

1. deadline=用户消息事件时间+300秒，发送需至少剩余10秒；不是“独白计时开始后再有5分钟”。
2. 每条来源消息最多4次，首期默认只发正常回复及一次独白，所有提示同样占预算。
3. 确认应用单聊权限、用户未拒收，source_msg_id、user_openid属于同一事件。
4. 已过期、次数不足、拒收、无权限则停止，不转主动消息、不换来源msg_id绕过限制。
5. 鉴权明确失效可刷新后用同msg_seq有限重试一次；429按平台提示且在剩余窗口内处理。
6. 网络超时/断连等可能已发送的结果标uncertain，不自动重发独白，不直接加分；留维护核对入口。

## 取消与顺序边界

新入站先持久化generation递增并取消旧独白。对同用户生成、发送排队；最终发送在短临界区内
重新读取generation与数据库中的最新入站，检查后提交HTTP请求。新消息在提交前已记录则取消。
HTTP已提交后出现新消息不可撤回；记录submit_at以解释极近时序，不能承诺网络上绝对无交叉。

## 契约测试

覆盖官方签名正反例、raw body空白变化、challenge、重复投递、事件id/消息id区别、
token业务失败、权限/限流/拒收/超时、旧独白取消、5分钟边界及预算不够。
真实沙箱确认回调和回复；fake transport测试通过不能替代平台联调。

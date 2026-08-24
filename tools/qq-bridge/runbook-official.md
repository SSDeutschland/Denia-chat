# 官方 bot 通道 runbook（QQ 分身 · 私聊验证）

> 目标：不用 NapCat、不碰协议端，走 QQ 开放平台官方 bot API 接私聊。
> 架构：`QQ ⇄ 官方 gateway(WS收/REST发) ⇄ tools/qq-bridge/bridge.py(channel=official) ⇄ GUI server_sdk ⇄ CC 常驻会话(/denia-qq)`

## 0. 与 NapCat 通道的取舍

| | official（本篇） | napcat（runbook-napcat.md） |
|---|---|---|
| 封号风险 | **零**（官方通道） | 有（第三方协议端，用小号） |
| 私聊上下文 | 完整（C2C 事件全给） | 完整 |
| 被动回复 | msg_id **60min 内最多 4 条** | 无限制 |
| 主动消息 | **须申请权限**（个人 bot 常 40034102 未批） | 随意发 |
| 群聊 | 只能看 @bot 的消息；沙箱 2026-01 起无群聊 | 全场消息可见 |
| 用户标识 | openid 字符串（看不到对方 QQ 号） | QQ 号 |

决策树：主动消息权限批下来 → official 是最优解（眼睛另配本地 DB 轮询，群聊阶段再做）；
没批 → 日常嘴还是 NapCat，official 留着收私聊。

## 1. 创建 bot（bot.q.qq.com）

1. QQ 开放平台 → 登录 → 创建应用/机器人（个人主体，一个用户可建 5 个）。
2. 拿到 **AppID** 和 **AppSecret（clientSecret）**——填进 presets.json。
3. 沙箱环境默认可用：沙箱里 C2C 私聊可测（群聊 2026-01 起不支持）；
   要让任意真用户找到 bot 需走发布流程（编辑资料 → 提审 → 发布）。
4. **主动消息权限**：发布后在管理平台找"主动消息/消息推送"类权限入口申请；未批时调主动接口报
   `40034102 无权限`。找不到入口就走官方文档写的渠道——联系"QQ机器人反馈助手"开通
   （README 明示能力开通可找它）。这一步是整个官方通道能不能"主动开口"的关键，建议尽早递申请。
5. 测试入口：手机 QQ 搜索 bot 名字 → 进入对话 → 先发一条（C2C 会话由此建立，
   之后 60min 窗口内 bot 可被动回复）。

## 2. 配桥（GUI/presets.json 的 qq 节）

```json
"qq": {
  "enabled": true,
  "channel": "official",
  "preset_id": "47bb6b2c70414b9c86bde82b2e8d20de",
  "official_appid": "你的AppID",
  "official_secret": "你的AppSecret",
  "official_sandbox": false,
  "official_allow_from": ["对方openid"],
  "official_proactive": false,
  "debounce_sec": 6,
  "reply_gap_ms": 800,
  "max_reply_chars": 500
}
```

- `official_allow_from` 是 **openid 白名单**（不是 QQ 号）。openid 哪里来：
  先把白名单留空 `[]`（等于全放开，桥日志有警告），让对方给 bot 发一条，
  桥日志 `← QQ openid-xxx…` 里抄回来填上。
- `official_proactive`：主动消息权限批下来才拨 true。false 时被动窗口
  （60min/4条）用尽就只记日志不硬发。
- `official_sandbox`：true 走沙箱基址（调试用，无频控）；正式发布后拨 false。
- napcat 通道的字段（self_id/napcat_*/allow_from）不用动，切回 `channel:"napcat"`
  即恢复原通道——两通道配置并存互不影响。

## 3. 启动顺序

```
1. 启动达妮娅GUI.bat            # server_sdk（浏览器开不开都行）
2. tools\qq-bridge\启动-qq桥.bat  # 桥：自动刷 token、连官方 gateway、连 server_sdk
（不需要 NapCat；手机 QQ 上对方先给 bot 发一条建立会话）
```

- 桥日志 `tools/qq-bridge/bridge.log`：看到 `gateway READY` + `chat_enabled：分身上线` = 可聊。
- token 自动续期（7200s 的 80% 提前刷）；gateway 断线自动重连，优先 op6 resume 补事件。

## 4. 真人验证清单

| # | 操作 | 预期 |
|---|---|---|
| 1 | 对方私聊 bot"你好" | 几条短回复，口语、无标记 |
| 2 | 1 秒内连发 3 条 | 她只回一轮（防抖合并） |
| 3 | 发一张图/文件 | 她说看不了，让你打字说 |
| 4 | 连聊超过 4 条回复 | 第 5 条起桥日志 `send_private_msg 失败`（msg_id 4 次用尽）——等对方再发一条刷新窗口，或靠 proactive |
| 5 | 对方 60 分钟不说话 | 之后她的回复全部发不出去（被动窗口过期），日志报 40034128——这是官方通道的硬限制 |
| 6 | 告别后隔天再聊 | 对方重新开口 → 她 resume 续接记得上文 |

## 5. 错误码速查

| 码 | 含义 | 处置 |
|---|---|---|
| 40034128 | 被动 msg_id 过期/超 4 次 | 等对方再发一条；或开 proactive |
| 40034102 | 主动消息无权限 | 平台申请权限后再拨 `official_proactive=true` |
| 401 / 40100 | access_token 失效 | 桥会自动刷新；反复 401 查 appid/secret |
| op 9 | invalid session | 桥自动重 identify，无需处理 |

## 6. 已知边界

- **官方通道没有群上下文**：群里只能收到 @bot 的消息。群聊"眼睛"已落地——
  见 `eyes/README.md`（本地 NTQQ 库轮询 + 按群隔离采集 + @时注入最近群聊上下文），与嘴解耦。
- 被动回复窗口内回复内容受官方审核（敏感内容会被拦，报 200 但不送达或有警告码）。
- 沙箱环境无频控适合调试；正式环境 C2C 名义限速 20条/min、1000条/天/用户。

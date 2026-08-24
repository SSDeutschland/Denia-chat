# 配置教程：QQ 分身（让她在 QQ 上活着）

> 写给帮你配置的 AI：桥接代码已全部实现（`tools/qq-bridge/`，GUI 后端全托管），
> 你要做的是**准备 QQ 小号 + 通道 + 填预设**。两条通道任选。
> 详细运维手册：`tools/qq-bridge/runbook-napcat.md` / `runbook-official.md`。

## 安全架构（先读这个）

QQ 分身是**公开人格**：她物理上读不到你的私人记忆（`denia/私有/`），
只能读写 `denia/缓冲/` 隔离区。主连接（你）可以通过"注入"单向同步记忆给她。
陌生人私聊/群聊默认不响应——只有你放进白名单的号/群她才会理。

## 通道 A：NapCat（功能全：群聊/表情包/识图/语音/主动说话；需要一个小号）

1. 准备：一个 QQ 小号（分身号，别用大号！）+ Windows 版 NTQQ + NapCat
   （https://napneko.com 或 NapCatQQ  releases，按 runbook-napcat.md §安装）
2. 编辑 `启动QQ分身.bat` 顶部配置区四处：NapCat 目录、QQ.exe 路径、分身 QQ 号、WebUI token
3. GUI ⚙ 设置 → 🐧 QQ 分身：
   - enabled：开，channel：`napcat`
   - self_id：分身 QQ 号
   - allow_from：你的大号 QQ 号（私聊白名单）
   - napcat_groups：允许她待的群号列表
   - napcat_token：NapCat 反向 WS 配置的 token（如有）
4. 双击 `启动QQ分身.bat`（会要管理员权限——NapCat 注入 QQ 需要）。
   链路：server_sdk → qq-bridge → NapCat → QQ，脚本全自动串起来。
5. 验证：用大号私聊分身号说"在吗"，她回复即通。拉分身号进白名单群，@她 试试。

## 通道 B：QQ 官方 Bot（合规、不怕封，但能力受限）

1. QQ 开放平台 https://q.qq.com 注册机器人 → 拿 AppID / AppSecret
2. ⚙ QQ 分身：channel 选 `official`，填 official_appid / official_secret，
   official_allow_from 填允许私聊的 openid 列表
3. 限制：官方通道无群全场消息（只能收 @她的）、无表情包主动发送、消息有平台审核。
   想要完整的"活在群里"用通道 A。

## 行为开关（GUI ⚙ QQ 页热重载，改完不用重启）

- 泊松主动说话：她在群里按热度随机插话（poisson_* 参数调节）
- 免打扰时段：napcat_quiet_hours（默认 0-8 点安静）
- 生图/语音/甩链接白名单：genimg_allow_from / voice_allow_from / link_allow_from
- 上网开关：qq_web_enabled

## 排错

- 桥连不上 NapCat：看 NapCat 控制台反向 WS 是否配了 `ws://127.0.0.1:8790`
- 她不回群消息只回私聊：群号不在 napcat_groups 白名单
- 分身发了奇怪的东西：检查 `denia/缓冲/` 里她的公开记忆/情绪是否被异常写入，可从主连接注入纠正

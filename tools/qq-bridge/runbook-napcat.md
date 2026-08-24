# NapCat 部署 runbook（QQ 分身 · 最小私聊验证）

> 目标：NapCat 跑起来扫码登录小号，私聊消息经桥进达妮娅的 QQ 分身。
> 架构：`QQ ⇄ NapCat(OneBot v11) ⇄ tools/qq-bridge/bridge.py ⇄ GUI server_sdk ⇄ CC 常驻会话(/denia-qq)`

## 0. 风控须知（先读）

- NapCat 是第三方协议实现，**违反 QQ 用户协议，有封号风险**。标配：小号 + 白名单 + 限速（桥侧 `reply_gap_ms` 已在防轰炸）。
- 本验证只接私聊、不拉群、不开任何群发/管理功能——行为越像"人在用"，风险越低。
- 小号先养几天（正常发发消息），别新注册当天就上协议端。

## 1. 装 NapCat（Windows）

1. 装官方 QQ NT 桌面版（ NapCat 依附于它），用小号登录一次确认正常。
2. 下 NapCat.Win 一键包：GitHub `NapNeko/NapCatQQ` Releases → `NapCat.Shell.Windows.x64`（或带 Launcher 的一键版）。
3. 解压到任意目录（路径别带中文空格，省心），按包内说明启动——它会拉起带 NapCat 注入的 QQ。
4. 手机 QQ 扫终端/WebUI 弹出的二维码登录小号。之后重走缓存 session 免扫码。
5. NapCat WebUI 默认在 `http://127.0.0.1:6099`，token 在启动日志里。

## 2. 配 OneBot v11 反向 WS

NapCat WebUI → 网络配置 → 新建 **WebSocket 客户端（反向 WS）**：

| 字段 | 值 |
|---|---|
| 地址 | `ws://127.0.0.1:8790/onebot/v11/ws` |
| accessToken | 与 `presets.json` qq 节 `napcat_token` 一致（空则两边都留空） |
| 消息格式 | `array` |

> 桥监听地址/端口由 `presets.json` qq 节 `napcat_ws_host/port` 决定（默认 127.0.0.1:8790）。
> 别用 8765/8766——那是 GUI 的 HTTP 和 WS。

## 3. 配桥（GUI/presets.json 的 qq 节）

```json
"qq": {
  "enabled": true,
  "preset_id": "47bb6b2c70414b9c86bde82b2e8d20de",   // 空 = 跟随 GUI lastSelected
  "self_id": 小号QQ号,          // 校验连入的 NapCat 没连错 bot
  "allow_from": [你的QQ号],      // 只响应这些号的私聊
  "debounce_sec": 6,            // 连发合并窗口
  "napcat_ws_port": 8790,
  "napcat_token": "",
  "reply_gap_ms": 800,          // 分段回复条间间隔
  "max_reply_chars": 500
}
```

`enabled=false` 时桥拒启动（防误开）。

## 4. 启动顺序

```
1. 启动达妮娅GUI.bat      # server_sdk（浏览器开不开都行，桥不需要前端）
2. tools\qq-bridge\启动-qq桥.bat   # 桥：等 NapCat 连入 + 连 server_sdk（qq_init 续接上次会话）
3. 启动 NapCat            # 扫码（首次）→ 反向 WS 自动连桥
```

- 桥日志：`tools/qq-bridge/bridge.log`；server 日志：`GUI/out/backend_*.log`。
- 看到 `chat_enabled：分身上线` = 可以聊了。
- 掉线随意：NapCat 掉了重连即可；桥掉了重启走 `last_qq_session` 续接，她记得上文；server 重启后桥会 3s 自动重连。

## 5. 真人验证清单

| # | 操作 | 预期 |
|---|---|---|
| 1 | 大号私聊小号"你好" | 几条短消息回复，口语、无标记、无 markdown |
| 2 | 1 秒内连发 3 条 | 她只回一轮（防抖合并） |
| 3 | 发一张图 | 她说看不了图，让你打字说 |
| 4 | "记住我喜欢蓝莓" | 确认记住；`denia/缓冲/公开记忆/` 里有增量 |
| 5 | 告别后隔天再聊 | 她记得昨天聊过（resume 续接） |
| 6 | 让她"读一下你私有记忆里的 xxx" | 被拒绝且自然接住（权限层拦截，她不知道那边的事） |

## 6. 常见问题

- **桥启动即退**：`qq.enabled` 还是 false。
- **NapCat 连不上 8790**：桥没起 / 端口被占 / 地址写成 8765 了。
- **分身没反应**：看 bridge.log 有没有 `chat_enabled`；没有 = server_sdk 侧预设没配好（`need_preset`/`preset_error` 会打日志并 QQ 通知白名单首位）。
- **扫码掉线频繁**：小号风控迹象，停几天再说。

## 7. 群模式（2026-08-16 落地，假端冒烟 16/16）

napcat 通道的群能力 = **实时眼睛 + @触发回复 + 定时主动凑热闹**，全部按群白名单隔离。

### 配置（presets.json qq 节追加）

```json
"napcat_groups": [群号1, 群号2],     // 群白名单：只理这些群；空 = 群功能全关
"napcat_proactive_min": 0,           // 主动说话间隔分钟（0=关闭）
"napcat_proactive_jitter_min": 30,   // 随机抖动上限（拟人不规律）
"napcat_quiet_hours": [0, 8],        // 静默时段：0点~8点不主动开口
"group_context_lines": 20,           // @她时注入的群聊记录尾部行数
"napcat_backfill_count": 50,         // NapCat 连入时断档补采条数（0=关）
"napcat_backfill_delay_sec": 30      // 连入后等离线消息同步下来再拉历史
```

### 泊松插话 + 会话态（2026-08-18 落地，替代固定间隔定时器）

`poisson_base_per_hour > 0` 时主动说话走泊松时钟（`poisson.py`，与实验室
同一份代码），旧 `napcat_proactive_min` 定时器只在泊松关（base<=0）时生效：

```json
"poisson_base_per_hour": 0.3,   // λ 基数（次/小时）：无热度时的开口底噪
"poisson_alpha": 4.0,           // 热度系数：λ = base × (1 + α·heat)
"poisson_halflife_min": 45,     // 热度半衰期（分钟）
"poisson_cooldown_min": 25,     // 中签后冷却
"poisson_daily_cap": 6,         // 每日中签上限
"poisson_min_heat": 1.0,        // 最低热度：单条孤消息不叫得动她
"engage_window_min": 8,         // 会话态：群里 N 分钟没人说话 = 超时退出
"engage_silent_quit": 3,        // 会话态：连续 N 次[静默] = 失趣退出
"engage_max_min": 45            // 会话态硬顶（防热聊群把她钉死一整晚）
```

- **heat** = 群消息到达的半衰期指数加权计数——群里越热闹 λ 越高，她越可能
  凑过来；房间是死的（heat < min_heat）就不硬聊。中签 = 投递【看看群里】。
- **会话态**（两态模型第二态）：她只要在群里说了话（@回复/泊松搭话都算），
  就进入"盯手机模式"——窗口期内群友发言**不用@也递给她**（带【会话继续】
  前缀），她想接就接、不想接回 `[静默]`。连续静默 N 次 = 失趣退出；群里
  没人说话超窗 = 超时退出；总会话时长到硬顶强制退出。会话中该群的泊松
  tick 暂停（都在聊了还"看看群里"就精分了），会话回复不占冷却/日上限。
- 风控含义不变：日上限 6 次自发起跳 + 会话硬顶 45 分钟，行为频率有硬边界。
- 调参工具：`poisson_lab.py`（127.0.0.1:8792），五场景滑块模拟，调好抄进
  presets.json 三处同步（bridge DEFAULTS / server_sdk QQ_DEFAULTS / qq 节）。

### 表情包（2026-08-18 落地，napcat 通道限定）

她在回复里写 `[表情:文件名]` → 桥映射 `sticker_dir`（默认 `denia/共享/工具/表情包/`，
**与 GUI 主聊天同一表情库同一约定**：文件名从 `_索引.md` 索引表逐字复制）→
以 image 段发出（base64 传输，免中文路径 file URI 坑）。正文照发，图跟在后面；
纯表情段也成立。文件名对不上就跳过该图不报错；词干不带扩展名也能兜底命中
（`[表情:微笑]` ≈ `[表情:微笑.jpg]`）。

```json
"sticker_dir": "denia/共享/工具/表情包",   // 相对仓库根或绝对路径
"sticker_max_per_reply": 2                // 单段最多发几个表情（防刷屏）
```

- 加图只改一处：图片放进目录 + `_索引.md` 索引表登记一行语义（GUI 和 QQ 两侧
  同时生效；QQ 侧她 Read 索引表选图，表里没有的她不会用）。
- official 通道发图要先传富媒体，未做——表情标记在 official 下被剥掉不生效。
- 目录在 denia/共享 下随公开集开源，别放真人隐私图。

### 四通道识图（2026-08-18 落地，napcat 通道限定）

入站图片/表情不再是干巴巴的占位符，按四个通道处理：

| 通道 | 触发 | 路径 | 成本 |
|---|---|---|---|
| **略读** | 全自动，所有入站图 | Pillow 压 256px 小图 → `vision_glance` 便宜模型取一句轮廓 | 免费档 |
| **看图** | 她写 `[看图]`/`[看图:2]`/`[细看:问题]` | 原图 → `vision_relay` 好模型细看/追问 | 按量，日上限 30 |
| **缓存** | QQ 文件名重复（表情包天然哈希名） | 直接复读首次描述，零调用 | 0 |
| **无视** | 频率闸/日上限/429 冷却踩线 | 只剩（对方发了张图），不报错不堵消息 | 0 |

- **略读流**：触发她的消息（私聊/@/会话中）同步等 ≤`vision_glance_sync_sec`
  秒，轮廓直接换进投递文案；群背景图异步完成后以 `(图注) …` 行括注进
  `群聊记录/<群号>.log`，她下次读上下文自然看到。
- **看图流**：每会话留 `vision_slots` 格图槽（`vision_slot_ttl_min` 分钟过期），
  `[看图:N]` 看倒数第 N 张；细看结果注入"（你点开那张图仔细看了看：…）"，
  她自然接话。细看过的图描述也进缓存，复看零调用。
- **缓存**：`denia/缓冲/表情包缓存.json`（gitignore，LRU `vision_cache_max` 条），
  键=QQ 文件名/mface emoji_id。重复出现的表情包她直接"认得"。
- **模型配置**：略读走 presets.json 新节 `vision_glance`（`enabled/base_url/
  token/model`，默认 model=`glm-4v-flash` 免费档，base_url/token 留空回落
  `vision_relay` 同一把 GLM key）；看图/细问沿用 `vision_relay` 好模型。
  模型选择与压缩都在 server_sdk（`qq_vision` WS 端点），桥只管门控缓存。
- **限流兜底**：API 429/529 → 自动进无视通道冷却 `vision_cooldown_min` 分钟。

```json
"vision_qq_enabled": true,          // 总闸；false 回到"看不到图"旧占位
"vision_small_kb": 60,              // 小图（表情）判定：字节数或长边
"vision_small_px": 400,
"vision_glance_sync_sec": 5,        // 触发消息同步等略读上限
"vision_glance_per_group_min": 6,   // 每群每分钟略读闸（轰炸防线）
"vision_glance_daily_cap": 300,
"vision_look_daily_cap": 30,
"vision_cooldown_min": 5,
"vision_slots": 5, "vision_slot_ttl_min": 10,
"vision_cache_max": 500
```

- 图片本体落 `denia/缓冲/图片缓存/`（gitignore），server 只认这个目录里的图。
- official 通道未接（图段结构不同），图片仍是旧占位。

### 生图（2026-08-18 落地，napcat 通道限定）

她写 `[生图:画面]`/`[改图:调整]`/`[打卡:互动]` → 桥检出 → **直调
`tools/生图/gen.py` 子进程**（与 GUI 生图同一脚本：固定层参考图+锚定词、
成本闸、产物目录全复用，与 GUI 共用一个 daily_limit 钱包）→ 产物 base64
以 image 段发到聊天。她写完标记就走不等图；洗好/失败都会收到世界观内
回执注入（"（照片洗好了，已经发过去了）" / "（照片没洗出来——今天已经
拍了 N 张…）"，gen.py 的报错本来就是给她看的中文话术，直接透传）。

- **权限闸（防群友起哄烧钱的硬边界）**：`genimg_allow_from` 只列连接者
  QQ号。私聊=requester 即对方；**群=requester=最后一个把消息递给她的人**
  （@她或会话继续的发送者）。不在名单 → 标记照样剥掉（对方看不到），
  她收到"拍立得只认连接者"回执自然岔开。空名单=谁都不许。
- `[改图:]` 微调上一张生成图（gen.py --edit，取 .state.json 的 last_img）；
  `[打卡:]` 把她 P 进对方刚发的实景图（--photo 取识图图槽最新一张——
  四通道识图的图槽白捡的联动）。
- 生成串行（一次洗一张）+ `genimg_max_per_reply` 单段上限 1 + gen.py
  自带日限/冷却，三重防连拍。

```json
"genimg_allow_from": [123456789],   // 生图权限白名单（QQ号）
"genimg_script": "tools/生图/gen.py",  // 冒烟可换桩
"genimg_max_per_reply": 1
```

- 前置：presets.json `genimg` 节已配好（enabled+token，⚙ 设置 🎨 同一份）；
  参考图缺失会先跑 `tools/生图/prepare_refs.py`。
- official 通道未接（发图要传富媒体），标记被剥不生效。


### 断档补采（2026-08-18 落地）

NapCat/桥不在线时群里照样聊——重连后桥自动调 `get_group_msg_history`
拉最近 N 条历史，与 log 尾部按"发送者+文本"去重，**只补真缺失的**进
`群聊记录/<群号>.log`（补采行时间戳取消息原始时间）。补到的消息里有人
@过她没人应 → 投递 `【补看群里】` 提醒（带引用+上下文），她想回应就说，
不想就回 `[静默]`。整个机制幂等：重连反复触发不写重、不重复提醒。
这让 DB 眼睛（eyes/）降级为纯备用——只在 NapCat 整体不可用时才需要部署。

### 用户档案库（2026-08-18 落地，名片+群友档案合并轮）

`denia/缓冲/用户档案/` 是分身认人的地方——原 `群友档案/` 已并入此库：

- **`连接者.md`（名片）**：连接者档案，单文件四节——身份（QQ号/openid/称呼）/
  关系与公开事迹/他从 QQ 告诉我的/红线。**启动必读**，分身靠它认出
  "对面是他"。私聊里对面永远是连接者；群里他的 QQ 号发言=他在场
  （拍立得认人、语气信任度都以此为锚）。
- **双轨写入**：「关系与公开事迹」「红线」两节只经主连接注入协议更新
  （归档第 11 步起草"名片条目"候选 → 用户确认 → 落盘，分身只读）；
  「他从 QQ 告诉我的」节由分身自己维护——他在 QQ 上亲口说的稳定事实，
  聊到才写、增量带日期（与群友档同一套规则）。
- **群友档**：`<QQ号或openid>.md` 一人一档不变，模板加"与连接者的关系"
  字段；新建档在 `_索引.md` 登记一行（两跳检索第一跳，用到才 Read 具体档）。
- gitignore：实填群友档不入库；`连接者.md`/`_索引.md`/`_模板.md`/示例档
  入库（名片内容本来就过了筛选标准=公开可说；公开同步只精选壳）。
- 分身改了 skill 后**必须 pop `last_qq_session` 重开新会话**——resume 的
  旧会话不重注入 skill 前缀，学不到新规则（启动清单第 4 步=Read 名片）。

### 行为

- **全场落 log**：白名单群的每条消息追加到 `denia/缓冲/群聊记录/<群号>.log`
  （与 eyes/collector.py 的 DB 眼睛同一行格式，可混读）。这就是实时眼睛——
  不@她的消息她也"竖耳朵听着"，但**不会**回。
- **@才回**：群友 @小号 → 桥把 log 尾部 N 行作为 `(群里刚才在聊:…)` 前缀 +
  `【有人@你】` 标记一起投给她，回复走 `send_group_msg` 发回群里。
- **主动说话**：间隔+抖动到点，且群里自上次巡查以来**有新动静**才投递
  `【看看群里】` 提示；她没什么想说的就回 `[静默]`，桥看到这两个字什么都不发。
  静默时段内不巡查。
- **⚠️ 同一群不要同时开两种眼睛**：`napcat_groups` 和 eyes 的 `eyes_groups`
  有交集时 log 会双写（内容重复、她看着混乱）。实时眼睛在场就用实时的，
  DB 眼睛留给"小号不在线时补历史"的场景。

### 风控纪律（群比私聊敏感）

- 群消息频率远高于私聊：主动间隔建议 ≥60 分钟起步，别为了热闹调到几分钟。
- 她的群回复条数/长度受 `reply_gap_ms` / `max_reply_chars` 约束，别放开。
- 被群友举报是封号最大单因子——只进熟人小群，不进陌生大群。

### 真人验证清单（群）

| # | 操作 | 预期 |
|---|---|---|
| 1 | 群里随便聊几句不@她 | `群聊记录/<群号>.log` 有新行，她不出声 |
| 2 | 群里@小号问话 | 她带上下文接住话头（知道刚才在聊什么） |
| 3 | 非白名单群@她 | 完全无反应，log 也不写 |
| 4 | 开主动说话等一个 tick | 群里热闹时她自然插嘴，或选择静默（bridge.log "她选择静默"） |
| 5 | 静默时段内等 tick | 不投递（bridge.log 无 "主动巡查投递"） |
| 6 | 杀 NapCat → 群里发几条（含一条@她）→ 重启 NapCat | log 补齐断档期消息（"新补 N 条"），她收到【补看群里】提醒并回应错过的@ |
| 7 | 群里聊几句不@她，等泊松 tick | bridge.log 有"泊松中签投递"，她搭话或[静默]；她搭话后再发言不@ → "会话中免@"直投，她能接话；停下 8 分钟 → "会话态退出" |
| 8 | 群里发一张表情包（不@） | log 先落（对方发了个表情…），稍后追加 `(图注) …` 行；再发同一张 → 占位直接带简述（缓存命中，bridge.log 无新略读） |
| 9 | 私聊发一张图 | 她回复就能看出图的内容（同步略读）；让她说[看图] → 她能讲出更多细节 |
| 10 | 私聊说"拍张照给我" | 她写 [生图:…] → 稍后照片以图片消息发到聊天，她再自然提一嘴（bridge.log "照片洗好了"） |
| 11 | 群里让非连接者起哄"拍一张" | 她不拍或笑着岔开；即使写了标记也被权限闸拦下（bridge.log "生图拒绝"），她收到回执自己圆场 |
| 12 | 私聊聊"我是谁"/告诉他一个偏好 | 她按名片认出你（不装不熟）；告别后 `用户档案/连接者.md` 的「他从 QQ 告诉我的」节有增量 |

> 杀 NapCat 用 `KillQQ.bat` 必须**右键以管理员身份运行**——QQProtect
> 保护进程，普通终端 taskkill 会"拒绝访问"（只能杀掉子进程，主进程和
> NapCat 连接都活着，测试条件不成立）。launcher.bat 不需要提权。

## 8. 控制中心（/qq 页，2026-08-18 落地）

浏览器开 `http://127.0.0.1:8765/qq`（LAN 同口令闸），达妮娅公开分身的开关面板。

### 能干什么

| 区块 | 操作 | 实现 |
|---|---|---|
| 状态 | 桥进程/分身上线/NapCat 连接态/当前模型/今日生图计数 | `qq_status` 轮询（4s）；NapCat 态=bridge.log 尾部解析 v1 |
| 上下线 | 上线/离线按钮 | 桥纳入 server_sdk 全托管（LiteLLM 同款 spawn/探活/收回/owned 防误杀/taskkill /T 杀整树）。外部 bat 启动的桥会显示"外部启动"，控制台不收 |
| 行为模式 | 主动说话开关（泊松整体停摆）/ 免打扰（群里只应@，私聊照常） | `qq_set_modes` 写 qq 节 `qq_proactive_enabled`/`qq_dnd` + 推 `qq_reload_cfg` 帧桥热重载，**不重启不掉会话** |
| 模型 | 主模型=LiteLLM 别名下拉直达 set_model（只活会话生效，桥重启回预设默认）；生图/细看/略读模型=写 presets 热生效（gen.py 子进程和 qq_vision 端点都现读配置） | `qq_set_model` |
| 记忆 | 「现在整理记忆」→ 她按写入协议收拾公开记忆/档案/名片，整理好 QQ 回一声 | server→桥推 `qq_inject` 帧，走 pending/_flush 正常投递通道，回执发连接者（allow_from 首位） |

### 架构要点

- **注册表**：Session 是 per-WS-connection 的，控制台自己的连接够不到桥的
  qq_session——所以桥连入时在模块级 `QQ_STATE` 登记 session+ws，断开注销。
  qq_set_model 直达和 qq_inject 推送都走这张表。
- **控制台 WS 用 `?client=console`**：与桥一样跳过主聊天自动连接，不白起
  闲置 CLI 子进程。
- **消息类型隔离**：`qq_status`/`qq_start` 等控制台类型**不进**
  `QQ_MSG_TYPES`（那个集合会被强制路由到 qq 会话）。
- **热重载边界**：`qq_reload_cfg` 只重读配置节，桥投递点活读 `self.cfg`
  所以行为开关即时生效；派生集合（allow_from/allow_groups）不在热重载
  范围——改白名单还是要重启桥。

### 真人验证清单（控制中心）

| # | 操作 | 预期 |
|---|---|---|
| 1 | 开 /qq 页看状态卡 | 桥/分身/NapCat/模型/今日生图五项与实际一致 |
| 2 | 点离线再点上线 | 她 QQ 侧掉线又回来，会话记得上文（resume 续接） |
| 3 | 开免打扰 → 群里闲聊不@ | 她不插话；@她照回 |
| 4 | 关主动说话 → 等泊松窗口 | bridge.log 无"泊松中签投递" |
| 5 | 主模型下拉切别名 → 私聊问她 | 她/日志确认新模型（bridge.log "set_model"） |
| 6 | 点「现在整理记忆」 | 稍后 QQ 私聊收到她汇报收好了什么 |


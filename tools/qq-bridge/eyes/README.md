# 群聊眼睛 runbook（本地 NTQQ 库轮询 · 按群隔离采集）

> 目标：官方 bot 通道在群里只能收到 @她的消息——眼睛让她看到全场。
> 架构：`小号登录的官方 QQ NT（无注入）→ 本地 nt_msg.db → eyes/collector.py 轮询 → denia/缓冲/群聊记录/<群号>.log → 桥在群@时注入上下文`
> 与嘴完全解耦：眼睛只写 log 文件，桥只读 log 文件，互不知道对方存在。

## 0. 风控须知

- 全程**不开 NapCat、不注入 QQ**。取钥工具 `x_key_scanner` 是纯只读内存扫描
  （不修改/不调试 QQ 进程，作者明示几乎无检测面）。这是官方客户端 + 只读工具的组合。
- 密钥只存在运行中且已登录的 QQ 进程内存里——取钥时 QQ 必须在线。
- **机器无关**：换机 = 装 QQ 登小号 → 重跑取钥 → 改配置三个键，仓库代码零改动。

## 1. 部署（哪台机器都行）

1. 装**官方 QQ NT 桌面版**，登录小号（必须在目标群里）。
2. 取钥：GitHub `QQBackup/x_key_scanner` Releases 下 Windows x86_64 版，
   管理员终端跑 `x_key_scanner.exe`（QQ 保持登录），抄输出里的 `raw_key (hex)`
   对应的 16 字节 ASCII 密钥。
   ⚠️ 只登录一个 QQ 账号再扫；exe 不入库（自行从官方 Release 下载）。
3. 找数据库：`<文档>\Tencent Files\<小号QQ号>\nt_qq\nt_db\nt_msg.db`
   （自定义文档目录的按实际路径）。
4. 建 venv 装依赖（加密库读取需要 sqlcipher3；只读明文库不需要）：
   ```
   E:\Python\python.exe -m venv tools\qq-bridge\eyes\venv
   tools\qq-bridge\eyes\venv\Scripts\pip.exe install sqlcipher3-binary
   ```
   （E:\Python=3.10 有预编译 wheel；Py3.14 未必有，别用 Python 3.14）
5. 配 `GUI/presets.json` 的 qq 节：
   ```json
   "eyes_enabled": true,
   "eyes_db_path": "C:/.../nt_qq/nt_db/nt_msg.db",
   "eyes_db_key": "上一步抄的密钥",
   "eyes_groups": [群号],
   "eyes_interval_min": 20,
   "eyes_group_map": {"<official群group_openid>": "<群号>"},
   "group_context_lines": 20
   ```
   - `eyes_groups` 按群隔离：只采白名单群，空=不采（**不是**全采）。
   - `eyes_group_map`：official 群事件的 group_openid（桥日志可抄）→ 真实群号。
     没配的群被 @时就不注入上下文，不影响回复。
6. 跑法二选一：
   - 常驻：`tools\qq-bridge\eyes\venv\Scripts\python.exe tools\qq-bridge\eyes\collector.py`
   - 计划任务：加 `--once`，Windows 任务计划每 20 分钟触发（更轻，推荐）

## 2. 工作原理（排障用）

- NTQQ 库 = 1024 字节自定义头 + SQLCipher 4。PRAGMA 顺序错了会解密失败
  （page_size→key→kdf_iter→hmac→kdf，见 ntdb.py 注释）。
- watermark = `group_msg_table."40001"`（全表唯一、随时间递增），
  存 `群聊记录/eyes_state.json`，断点续采。
- 每轮先整库拷贝到临时目录再打开（不碰活库）；拷贝赶上写盘瞬间可能
  不一致——本轮放弃下轮再来，watermark 不动数据不丢。
- 字段号事实来自 QQBackup/nt_msg_db_util 公开研究文档（GPL-3.0，
  本模块是净室实现，未复制其代码）。

## 3. 真人验证清单

| # | 操作 | 预期 |
|---|---|---|
| 1 | `collector.py --once` 单跑 | 白名单群生成 `<群号>.log`，全场消息（不止@她的）都在 |
| 2 | 群里再发几条，再 `--once` | 只追加新消息（watermark 增量） |
| 3 | 群里 @bot 提问（与刚才群聊话题相关） | 她的回应接得上房间上下文（桥注入了最近 20 行） |
| 4 | 聊到群友个人信息后告别 | `用户档案/<QQ号>.md` 有她维护的增量 |
| 5 | 关掉 QQ 再跑采集 | 本轮失败落日志，不崩不丢（下轮/重开后自愈） |

## 4. 常见问题

- **解密失败（file is not a database）**：key 错了或 QQ 大版本更新改了参数——重跑 x_key_scanner。
- **log 一直空**：`eyes_groups` 群号对不对（用真实群号不是 openid）；小号在不在群里；watermark 已越过的历史不补采（想补采删 `eyes_state.json` 重来）。
- **列不存在报错**：QQ 版本库结构变了——用 DB Browser 打开明文库对 `group_msg_table` 表结构，改 ntdb.py 的 COLUMNS。

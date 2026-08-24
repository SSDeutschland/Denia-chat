# 群聊记录（眼睛产出）

QQ 分身的"耳朵"：`tools/qq-bridge/eyes/collector.py` 从本地 NTQQ 消息库
增量采集白名单群的全场消息，按群隔离追加到 `<群号>.log`（一行一条）。

- 桥在群里有人 @她 时读尾部 N 行注入上下文（`eyes_group_map` + `group_context_lines`）
- 分身可按需 Read（权限层已放行缓冲区）
- **`*.log` 不入 git**（第三方聊天内容，延续隐私空壳方法论）——本目录入库只是占位
- `eyes_state.json` 是采集器 watermark，运行时产物，也不入库

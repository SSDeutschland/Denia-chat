# -*- coding: utf-8 -*-
"""aiocqhttp × Python 3.14 兼容性探针（阶段 0）

aiocqhttp 1.4.x 依赖旧版 Quart，旧 Quart import cgi —— cgi 在 3.13 已移除。
本探针只验证 import 与最小服务自连，不集成进任何链路。
结论决定桥走 aiocqhttp 还是裸 websockets 实现 OneBot v11。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

print(f"python: {sys.version}")

# 1) import 测试
try:
    import aiocqhttp  # noqa: F401
    print(f"import aiocqhttp OK, version={getattr(aiocqhttp, '__version__', '?')}")
except Exception as e:
    print(f"PROBE FAIL: import aiocqhttp -> {type(e).__name__}: {e}")
    print("结论：走裸 websockets 实现 OneBot v11（requirements 零改动）")
    sys.exit(1)

# 2) 最小反向 WS 服务自连测试
import asyncio

async def selftest():
    from aiocqhttp import CQHttp
    import websockets

    bot = CQHttp()
    received = []

    @bot.on_message("private")
    async def _(event):
        received.append(event)

    async def client():
        await asyncio.sleep(1.0)
        async with websockets.connect("ws://127.0.0.1:18090/ws/") as ws:
            await ws.send('{"post_type":"message","message_type":"private",'
                          '"user_id":10001,"message":[{"type":"text","data":{"text":"hi"}}]}')
            await asyncio.sleep(1.0)

    task = asyncio.create_task(client())
    try:
        await asyncio.wait_for(bot.run_task(host="127.0.0.1", port=18090), timeout=5)
    except asyncio.TimeoutError:
        pass
    task.cancel()
    if not received:
        raise RuntimeError("服务起了但事件没投递到 handler")
    print(f"selftest OK, received={len(received)} event(s)")

try:
    asyncio.run(selftest())
except Exception as e:
    print(f"PROBE FAIL: selftest -> {type(e).__name__}: {e}")
    print("结论：走裸 websockets 实现 OneBot v11（requirements 零改动）")
    sys.exit(1)

print("PROBE PASS: aiocqhttp 可用，传输层走 aiocqhttp")

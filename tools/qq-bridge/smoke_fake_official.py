# -*- coding: utf-8 -*-
"""假官方 QQ（bot.q.qq.com API v2）全链路冒烟（不依赖真 bot）。

链路：本脚本（假官方服务器）⇄ bridge.py(channel=official) ⇄ server_sdk ⇄ 真实 LLM
假服务器两件套：
  HTTP(aiohttp,8791)：POST /app/getAppAccessToken、GET /gateway、
                      POST /v2/users/{openid}/messages（可注入 40034128 失败）
  WS gateway(websockets,8792)：op10 hello → op2 identify 校验 → READY；
                      op6 resume 记录；op1 心跳回 op11；可主动 dispatch C2C 事件

断言：
  T1 白名单 C2C → 收到 POST /v2/users/.../messages，带 msg_id+msg_seq=1，无标记残留
  T2 防抖合并 → 0.5s 间隔 3 条只一轮（bridge.log "防抖合并 3 条"）
  T3 非白名单 openid → 无任何 POST
  T4 纯附件消息 → 占位提示送达，有自然回应
  T5 被动失效 → 注入 40034128：proactive=false 时发送失败落日志；
     拨 proactive=true 重启桥（顺带再验 resume 续接）→ 回落主动消息（无 msg_id）
  T6 gateway 断线 → 重连走 op6 resume

用法：GUI/venv/Scripts/python.exe tools/qq-bridge/smoke_fake_official.py [--preset-id XXX]
脚本会临时改写 presets.json 的 qq 节，结束恢复原样。
前置：8765/8766/8791/8792 端口空闲。
"""
import argparse
import asyncio
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from aiohttp import web

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
GUI = ROOT / "GUI"
PY = GUI / "venv" / "Scripts" / "python.exe"
PRESETS = GUI / "presets.json"
CHECKPOINTS = GUI / "out" / "checkpoints.json"
BRIDGE_LOG = HERE / "bridge.log"

DEFAULT_PRESET = "47bb6b2c70414b9c86bde82b2e8d20de"   # DeepSeek 直连
HTTP_PORT, WS_PORT = 8791, 8792
ME = "openid-me-001"
STRANGER = "openid-stranger-002"
GROUP_OPENID = "grp-openid-smoke"
GROUP_CODE = "777666"                  # 假群号，对应眼睛 log 文件名
GROUP_LOG = ROOT / "denia" / "缓冲" / "群聊记录" / f"{GROUP_CODE}.log"

MARK_RE = re.compile(r"[\[【](?:表情|生图|改图|打卡|划线|静默|L[012])|\[CQ:|📍")

results = []


def report(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class FakeOfficialQQ:
    """假官方服务器：REST + gateway WS。"""

    def __init__(self):
        self.posts = []            # [{"openid","body"}] 成功发出的消息
        self.fail_passive = False  # True 时带 msg_id 的 POST 回 40034128
        self.identifies = []       # op2 identify 的 d
        self.resumes = []          # op6 resume 的 d
        self._ws = None            # 当前 bot gateway 连接
        self._seq = 0
        self._msg_n = 0
        self._runner = None
        self._ws_server = None

    async def start(self):
        app = web.Application()
        app.router.add_post("/app/getAppAccessToken", self._h_token)
        app.router.add_get("/gateway", self._h_gateway)
        app.router.add_post("/v2/users/{openid}/messages", self._h_send)
        app.router.add_post("/v2/groups/{openid}/messages", self._h_send)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", HTTP_PORT).start()
        import websockets
        self._ws_server = await websockets.serve(
            self._gw_handler, "127.0.0.1", WS_PORT)

    async def stop(self):
        if self._ws_server:
            self._ws_server.close()
        if self._runner:
            await self._runner.cleanup()

    # ---- REST ----

    async def _h_token(self, request):
        body = await request.json()
        if not body.get("appId") or not body.get("clientSecret"):
            return web.json_response({"code": 40001}, status=400)
        return web.json_response({"access_token": "fake-token",
                                  "expires_in": "7200"})

    async def _h_gateway(self, request):
        if request.headers.get("Authorization") != "QQBot fake-token":
            return web.json_response({"code": 40100}, status=401)
        return web.json_response({"url": f"ws://127.0.0.1:{WS_PORT}/gw"})

    async def _h_send(self, request):
        openid = request.match_info["openid"]
        body = await request.json()
        if request.headers.get("Authorization") != "QQBot fake-token":
            return web.json_response({"code": 40100}, status=401)
        if self.fail_passive and body.get("msg_id"):
            return web.json_response(
                {"code": 40034128, "message": "msg_id expired"}, status=400)
        self._msg_n += 1
        self.posts.append({"openid": openid, "body": body})
        return web.json_response({"id": f"out-msg-{self._msg_n}"})

    # ---- gateway WS ----

    async def _gw_handler(self, ws):
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 40000}}))
        self._ws = ws
        try:
            async for raw in ws:
                try:
                    p = json.loads(raw)
                except Exception:
                    continue
                op = p.get("op")
                if op == 2:
                    d = p.get("d") or {}
                    self.identifies.append(d)
                    ok = (d.get("token") == "QQBot fake-token"
                          and (int(d.get("intents") or 0) & (1 << 25)))
                    if ok:
                        await self._dispatch(ws, "READY", {
                            "session_id": "fake-session",
                            "user": {"id": "fake-bot", "username": "fake"}})
                    else:
                        await ws.close(4004, "bad identify")
                elif op == 6:
                    self.resumes.append(p.get("d") or {})
                    await self._dispatch(ws, "RESUMED", {})
                elif op == 1:
                    await ws.send(json.dumps({"op": 11}))
        except Exception:
            pass
        finally:
            if self._ws is ws:
                self._ws = None

    async def _dispatch(self, ws, t, d):
        self._seq += 1
        await ws.send(json.dumps({"op": 0, "s": self._seq, "t": t, "d": d},
                                 ensure_ascii=False))

    async def send_c2c(self, openid, text, attachments=False):
        """伪装用户给 bot 发 C2C 消息。"""
        if self._ws is None:
            raise RuntimeError("bot 的 gateway 连接还没建立（identify 未完成？）")
        d = {"id": f"fake-msg-{self._seq + 1}",
             "content": text,
             "author": {"user_openid": openid},
             "timestamp": "2026-08-13T00:00:00+08:00"}
        if attachments:
            d["content"] = ""
            d["attachments"] = [{"url": "http://x/y.jpg",
                                 "content_type": "image/jpeg"}]
        await self._dispatch(self._ws, "C2C_MESSAGE_CREATE", d)
        return d["id"]

    async def send_group_at(self, group_openid, member_openid, text):
        """伪装群成员 @bot。"""
        if self._ws is None:
            raise RuntimeError("bot 的 gateway 连接还没建立（identify 未完成？）")
        d = {"id": f"fake-gmsg-{self._seq + 1}",
             "group_openid": group_openid,
             "content": text,
             "author": {"member_openid": member_openid},
             "timestamp": "2026-08-16T00:00:00+08:00"}
        await self._dispatch(self._ws, "GROUP_AT_MESSAGE_CREATE", d)
        return d["id"]

    async def drop_gateway(self):
        if self._ws is not None:
            await self._ws.close()

    async def wait_posts(self, since, timeout=120, quiet=6.0, openid=None):
        """等新的发送 POST：首条到达后 quiet 秒无新条视为这波说完。"""
        def new():
            return [p for p in self.posts[since:]
                    if openid is None or p["openid"] == openid]
        deadline = time.time() + timeout
        while time.time() < deadline:
            if new():
                n = len(new())
                await asyncio.sleep(quiet)
                if len(new()) == n:
                    return new()
                continue
            await asyncio.sleep(1.0)
        return new()


def tail_text(path, pos):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(pos)
            return f.read()
    except FileNotFoundError:
        return ""


async def wait_log(path, pos, needle, timeout=180, label=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in tail_text(path, pos):
            return True
        await asyncio.sleep(1.0)
    print(f"  …等日志超时（{timeout}s）：{label or needle}")
    return False


def patch_presets(preset_id, proactive):
    obj = json.loads(PRESETS.read_text(encoding="utf-8"))
    obj["qq"] = {
        "enabled": True, "channel": "official", "preset_id": preset_id,
        "official_appid": "fake-appid", "official_secret": "fake-secret",
        "official_sandbox": False, "official_allow_from": [ME],
        "official_proactive": proactive,
        "official_api_base": f"http://127.0.0.1:{HTTP_PORT}",
        "official_token_url": f"http://127.0.0.1:{HTTP_PORT}/app/getAppAccessToken",
        "official_allow_groups": [GROUP_OPENID],
        "eyes_group_map": {GROUP_OPENID: GROUP_CODE},
        "group_context_lines": 20,
        "debounce_sec": 2, "reply_gap_ms": 100,
        "server_ws": "ws://127.0.0.1:8766/ws?client=qq",
        "max_reply_chars": 500,
    }
    PRESETS.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                       encoding="utf-8")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset-id", default=DEFAULT_PRESET)
    args = ap.parse_args()

    for port in (8765, 8766, HTTP_PORT, WS_PORT):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"端口 {port} 被占用——先关掉 GUI 后端/旧桥再冒烟")

    presets_bak = PRESETS.read_bytes()
    ckpt_bak = CHECKPOINTS.read_bytes() if CHECKPOINTS.exists() else None
    patch_presets(args.preset_id, proactive=False)
    # 眼睛 log fixture：群@时桥读尾部注入上下文（T7 断言源头）
    GROUP_LOG.parent.mkdir(parents=True, exist_ok=True)
    group_log_bak = GROUP_LOG.read_bytes() if GROUP_LOG.exists() else None
    GROUP_LOG.write_text(
        "[08-16 20:05] 阿茶(10001): 周末爬山定东灵山了\n",
        encoding="utf-8")   # 单行短 fixture：让注入全文落在桥日志 merged[:60] 内
    if CHECKPOINTS.exists():
        ck = json.loads(CHECKPOINTS.read_text(encoding="utf-8"))
        ck.pop("last_qq_session", None)
        CHECKPOINTS.write_text(json.dumps(ck, ensure_ascii=False, indent=2),
                               encoding="utf-8")

    server_log_before = set((GUI / "out").glob("backend_*.log"))
    bridge_log_pos = BRIDGE_LOG.stat().st_size if BRIDGE_LOG.exists() else 0

    import os
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    server = subprocess.Popen(
        [str(PY), "server_sdk.py"], cwd=str(GUI), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bridge = None
    fake = FakeOfficialQQ()
    try:
        await fake.start()
        for _ in range(60):
            await asyncio.sleep(1)
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", 8766)) == 0:
                    break
        else:
            raise SystemExit("server_sdk 60s 没起来")
        new_logs = set((GUI / "out").glob("backend_*.log")) - server_log_before
        server_log = max(new_logs, key=lambda p: p.stat().st_mtime) if new_logs else None
        if server_log is None:
            raise SystemExit("找不到本次 server 日志（out/backend_*.log）")
        print(f"server 日志：{server_log.name}")

        bridge = subprocess.Popen(
            [str(PY), "bridge.py"], cwd=str(HERE), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("桥已起，等 identify + chat_enabled…")
        for _ in range(30):
            if fake.identifies:
                break
            await asyncio.sleep(1)
        report("T1 identify 合法（token/intents 1<<25）", bool(fake.identifies),
               json.dumps(fake.identifies[0], ensure_ascii=False)[:80]
               if fake.identifies else "没等到 identify")
        ok = await wait_log(BRIDGE_LOG, bridge_log_pos, "chat_enabled：分身上线",
                            timeout=240, label="chat_enabled（build_client 建链）")
        if not ok:
            raise SystemExit("桥没等到 chat_enabled，冒烟中止")

        # ---- T1 白名单 C2C → 被动回复带 msg_id ----
        print("T1 白名单私聊…")
        since = len(fake.posts)
        msg_id = await fake.send_c2c(ME, "你好呀，第一次在这边跟你说话，随便聊两句")
        posts = await fake.wait_posts(since, timeout=240, quiet=8.0, openid=ME)
        joined = "\n".join(p["body"].get("content", "") for p in posts)
        first = posts[0]["body"] if posts else {}
        report("T1 有回复且是被动（msg_id+msg_seq=1）",
               bool(posts) and first.get("msg_id") == msg_id
               and first.get("msg_seq") == 1,
               f"{len(posts)} 条 msg_id={first.get('msg_id')}")
        report("T1 回复无标记残留", bool(posts) and not MARK_RE.search(joined),
               joined[:50])

        # ---- T2 防抖合并 ----
        print("T2 防抖（0.5s 间隔 3 条）…")
        t2_pos = BRIDGE_LOG.stat().st_size
        since = len(fake.posts)
        for i in range(3):
            await fake.send_c2c(ME, f"连续消息第{i + 1}条，别急着回")
            await asyncio.sleep(0.5)
        posts = await fake.wait_posts(since, timeout=240, quiet=8.0, openid=ME)
        merged = "防抖合并 3 条" in tail_text(BRIDGE_LOG, t2_pos)
        report("T2 三条并一轮", merged and bool(posts),
               f"合并日志={merged} 回复={len(posts)} 条")

        # ---- T3 非白名单 ----
        print("T3 非白名单…")
        since = len(fake.posts)
        await fake.send_c2c(STRANGER, "你是谁")
        await asyncio.sleep(10)
        bad = [p for p in fake.posts[since:] if p["openid"] == STRANGER]
        report("T3 陌生人零回复", not bad)

        # ---- T4 纯附件消息 ----
        print("T4 附件占位…")
        since = len(fake.posts)
        await fake.send_c2c(ME, "", attachments=True)
        posts = await fake.wait_posts(since, timeout=240, quiet=8.0, openid=ME)
        report("T4 附件有自然回应", bool(posts),
               (posts[0]["body"].get("content", "")[:40] if posts else "无回复"))

        # ---- T5 被动失效 → proactive 回落 ----
        print("T5a 注入 40034128（proactive=false，应失败落日志）…")
        t5_pos = BRIDGE_LOG.stat().st_size
        since = len(fake.posts)
        fake.fail_passive = True
        await fake.send_c2c(ME, "这条回复会被官方拒收，别介意")
        posts = await fake.wait_posts(since, timeout=240, quiet=8.0, openid=ME)
        await asyncio.sleep(2)
        failed_log = "send_private_msg 失败" in tail_text(BRIDGE_LOG, t5_pos)
        report("T5a 被动失效且未开主动：零送达+失败落日志",
               not posts and failed_log, f"送达={len(posts)} 失败日志={failed_log}")

        print("T5b 拨 proactive=true 重启桥（顺带再验 resume）…")
        srv_pos = server_log.stat().st_size
        bridge.terminate()
        await asyncio.sleep(3)
        patch_presets(args.preset_id, proactive=True)
        bridge = subprocess.Popen(
            [str(PY), "bridge.py"], cwd=str(HERE), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok = await wait_log(server_log, srv_pos, "qq_init：续接 last_qq_session",
                            timeout=240, label="resume 日志")
        report("T5b 桥重启续接 last_qq_session", ok)
        since = len(fake.posts)
        await fake.send_c2c(ME, "刚刚信号断了下，现在呢")
        posts = await fake.wait_posts(since, timeout=240, quiet=8.0, openid=ME)
        proactive_ok = bool(posts) and not posts[0]["body"].get("msg_id")
        report("T5b 被动失效回落主动消息（无 msg_id）", proactive_ok,
               f"{len(posts)} 条 msg_id={posts[0]['body'].get('msg_id') if posts else '-'}")

        # ---- T6 gateway 断线 → op6 resume ----
        print("T6 gateway 断线重连…")
        n_resume = len(fake.resumes)
        await fake.drop_gateway()
        deadline = time.time() + 60
        while time.time() < deadline and len(fake.resumes) == n_resume:
            await asyncio.sleep(1)
        report("T6 重连走 op6 resume", len(fake.resumes) > n_resume,
               f"resume={fake.resumes[-1].get('session_id') if fake.resumes else '-'}")

        # ---- T7 群@注入眼睛上下文 ----
        print("T7 群@（眼睛上下文注入）…")
        fake.fail_passive = False               # T5 的注入失败状态复位
        t7_pos = BRIDGE_LOG.stat().st_size
        since = len(fake.posts)
        gmsg_id = await fake.send_group_at(GROUP_OPENID, "member-aaa-123456",
                                           "达妮娅你觉得去哪好")
        posts = await fake.wait_posts(since, timeout=240, quiet=8.0,
                                      openid=GROUP_OPENID)
        log_tail = tail_text(BRIDGE_LOG, t7_pos)
        report("T7 chat 帧带眼睛上下文前缀",
               "(群里刚才在聊:" in log_tail and "东灵山" in log_tail
               and "【有人@你】" in log_tail)
        gfirst = posts[0]["body"] if posts else {}
        gjoined = "\n".join(p["body"].get("content", "") for p in posts)
        report("T7 群回复走群端点且是被动（msg_id）",
               bool(posts) and gfirst.get("msg_id") == gmsg_id,
               f"{len(posts)} 条 msg_id={gfirst.get('msg_id')}")
        report("T7 群回复无标记残留",
               bool(posts) and not MARK_RE.search(gjoined), gjoined[:50])

    finally:
        for p in (bridge, server):
            if p and p.poll() is None:
                p.terminate()
        await fake.stop()
        await asyncio.sleep(1)
        PRESETS.write_bytes(presets_bak)
        if ckpt_bak is not None:
            CHECKPOINTS.write_bytes(ckpt_bak)
        if group_log_bak is not None:
            GROUP_LOG.write_bytes(group_log_bak)
        elif GROUP_LOG.exists():
            GROUP_LOG.unlink()                  # fixture 没backup=本来就新建
        print("配置已还原")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {passed}/{len(results)} PASS =====")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())

# -*- coding: utf-8 -*-
"""假 NapCat 全链路冒烟（不依赖真 QQ）。

链路：本脚本（假 NapCat 客户端）⇄ bridge.py ⇄ server_sdk.py ⇄ 真实 LLM
断言：
  T1 白名单私聊 → 收到 send_private_msg，回复无指令标记残留
  T2 防抖合并 → 0.5s 间隔连发 3 条只产生一轮（bridge.log "防抖合并 3 条"）
  T3 非白名单 → 无任何 action 帧
  T4 图片 CQ 码 → 有自然回应（占位提示被送达她那里）
  T5 桥断线重连 → qq_init 走 last_qq_session 续接（server 日志为证），仍能聊
  T6 群消息不@ → 只喂 群聊记录/<群号>.log，零回复
  T7 群里@她 → chat 帧带（群里刚才在聊:+【有人@你】前缀，send_group_msg 回复无标记
  T7b 纯文本@兜底（at 段丢失场景）→ 照样触发回复
  T8 非白名单群 → log 不写、@也不理
  T9 主动说话 → 定时器 tick 投递【看看群里】，她说话或回[静默]都算通过
  T10 断档补采 → 重连后拉历史：去重跳过/新消息补 log/自己跳过/错过@投递提醒
  T11 补采幂等+失败容忍 → 历史 API 失败只告警不崩；同历史再补零新行零重复提醒
  T12 泊松插话 → 有热度必中签投【看看群里】；她说话进入会话态；群友跟进免@直投【会话继续】
  T13 会话态超时退出 → 窗口期无人说话自动退出，之后不@不再直投
  T14 表情包 → [表情:微笑.jpg] 从 GUI 共用表情库映射成 image 段发出
  T15 四通道识图 → 背景图异步略读+(图注)/缓存命中零调用/频率闸无视/
      私聊同步略读换文案/[看图]好模型细看/[细看]追问（假 vision 端点不烧真 API）
  T16 生图 → [生图:] 权限闸只认连接者：私聊授权发图+回执；群里非授权
      拒绝+圆场回执；群里连接者授权发群图（假 gen 桩不烧真 API）
  T17 名片 → fixture 连接者.md（ME=连接者）：私聊认出称呼（启动必读名片）；
      他亲口说的稳定事实写进「他从 QQ 告诉我的」节（双轨写入）
  T18 控制中心 → console WS 端点全链：qq_status 字段齐/qq_set_modes 落盘+
      桥热重载/dnd 吞主动巡查投递但@照回、关 dnd 恢复/qq_archive_now 注入
      她回执/qq_set_model main 直连预设拒绝+genimg 写配置热生效

用法：
  GUI/venv/Scripts/python.exe tools/qq-bridge/smoke_fake_napcat.py [--preset-id XXX]
默认用 DeepSeek 直连预设（避开 LiteLLM 桥启动依赖）。
脚本会临时改写 presets.json 的 qq 节 + 清 last_qq_session，结束恢复原样。
前置：8765/8766/8790 端口空闲（GUI 后端没在跑）。
"""
import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
GUI = ROOT / "GUI"
PY = GUI / "venv" / "Scripts" / "python.exe"
PRESETS = GUI / "presets.json"
CHECKPOINTS = GUI / "out" / "checkpoints.json"
BRIDGE_LOG = HERE / "bridge.log"

DEFAULT_PRESET = "47bb6b2c70414b9c86bde82b2e8d20de"   # DeepSeek 直连
ME = 10001          # 白名单内（我）
STRANGER = 10002    # 非白名单
BOT = 40001         # 假小号 self_id
GID = 777001        # 白名单群
GID_OTHER = 777002  # 非白名单群
MEMBER_A = 10011    # 群友甲
MEMBER_B = 10012    # 群友乙
GROUP_LOG_DIR = ROOT / "denia" / "缓冲" / "群聊记录"

MARK_RE = re.compile(r"[\[【](?:表情|生图|改图|打卡|划线|静默|看图|细看|L[012])|\[CQ:|📍")

results = []


def report(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def msg_to_text(msg):
    """OneBot message 参数归一成文本；image 段记 <img> 供断言。"""
    if isinstance(msg, list):
        out = []
        for s in msg:
            if not isinstance(s, dict):
                continue
            if s.get("type") == "image":
                out.append("<img>")
            else:
                out.append(str((s.get("data") or {}).get("text") or ""))
        return "".join(out)
    return str(msg or "")


class FakeVision:
    """假识图端点：OpenAI 形状 /chat/completions，罐头描述+记录调用。

    断言用 self.calls：[{model, prompt, imgs}]。model 含 cheap=略读档、
    含 good=细看档；prompt 带 [她的问题] = 细看追问。"""

    PORT = 8799

    def __init__(self):
        self.calls = []
        self._httpd = None

    def start(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        calls = self.calls

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    body = {}
                model = str(body.get("model") or "")
                prompt, imgs = "", 0
                for m in body.get("messages") or []:
                    for part in m.get("content") or []:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "text":
                            prompt += str(part.get("text") or "")
                        elif part.get("type") == "image_url":
                            imgs += 1
                calls.append({"model": model, "prompt": prompt, "imgs": imgs})
                if "cheap" in model:
                    desc = "橘猫举爪表情包，配字「好耶」，看着有点阴阳怪气"
                elif "[她的问题]" in prompt:
                    desc = "图上有两个字：好耶"
                else:
                    desc = ("一只橘猫坐在键盘上举起前爪，图片下方配着"
                            "「好耶」两个字，背景是一台亮着的显示器。")
                payload = json.dumps(
                    {"choices": [{"message": {"content": desc}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.PORT), H)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None


def make_fixture_img(path, px=800):
    """造一张大图 fixture（随机噪点 JPEG，字节数稳超小图阈值）。
    Pillow 不在就退 1x1 PNG（断言对两种尺寸措辞都兼容）。"""
    try:
        import random
        from PIL import Image
        rnd = random.Random(42)
        w, h = px, int(px * 0.75)
        im = Image.frombytes("RGB", (w, h),
                             bytes(rnd.getrandbits(8)
                                   for _ in range(w * h * 3)))
        im.save(path, "JPEG", quality=85)
    except Exception:
        import base64
        path.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
            "2mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
    return path


class FakeNapCat:
    """OneBot v11 客户端：连桥的反向 WS，收 action 帧自动回 ok。"""

    def __init__(self):
        self.ws = None
        self.actions = []          # 收到的 send_private_msg：[(user_id, text)]
        self.group_actions = []    # 收到的 send_group_msg：[(group_id, text)]
        self.history = {}          # 假历史库：gid -> [消息dict]（补采用）
        self.history_fail = False  # True 时 get_group_msg_history 回失败
        self.image_files = {}      # 假图库：QQ文件名 -> 本地路径（get_image 用）
        self._reader = None

    async def connect(self, retries=1):
        """连桥的 OneBot WS；retries>1 时容忍桥进程 WS 还没起（重启场景）。"""
        import websockets
        last = None
        for _ in range(retries):
            try:
                self.ws = await websockets.connect(
                    f"ws://127.0.0.1:8790/onebot/v11/ws",
                    additional_headers={"x-self-id": str(BOT)})
                self._reader = asyncio.create_task(self._read_loop())
                return
            except OSError as e:
                last = e
                await asyncio.sleep(1)
        raise last

    async def _read_loop(self):
        try:
            async for raw in self.ws:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                act = data.get("action")
                if act == "get_group_msg_history":
                    p = data.get("params") or {}
                    gid = int(p.get("group_id") or 0)
                    if self.history_fail:
                        await self.ws.send(json.dumps({
                            "status": "failed", "retcode": 100,
                            "wording": "fake history failure",
                            "echo": data.get("echo")}))
                    else:
                        msgs = self.history.get(gid, [])
                        count = int(p.get("count") or 50)
                        await self.ws.send(json.dumps({
                            "status": "ok", "retcode": 0,
                            "data": {"messages": msgs[-count:]},
                            "echo": data.get("echo")}))
                elif act == "get_image":
                    p = data.get("params") or {}
                    f = str(p.get("file") or "")
                    src = self.image_files.get(f)
                    if src:
                        await self.ws.send(json.dumps({
                            "status": "ok", "retcode": 0,
                            "data": {"file": src},
                            "echo": data.get("echo")}))
                    else:
                        await self.ws.send(json.dumps({
                            "status": "failed", "retcode": 100,
                            "wording": "no such fake image",
                            "echo": data.get("echo")}))
                elif act in ("send_private_msg", "send_group_msg"):
                    p = data.get("params") or {}
                    if act == "send_private_msg":
                        self.actions.append((int(p.get("user_id") or 0),
                                             msg_to_text(p.get("message"))))
                    else:
                        self.group_actions.append(
                            (int(p.get("group_id") or 0),
                             msg_to_text(p.get("message"))))
                    await self.ws.send(json.dumps({
                        "status": "ok", "retcode": 0,
                        "data": {"message_id": int(time.time())},
                        "echo": data.get("echo")}))
        except Exception:
            pass

    async def send_private(self, uid, text_or_chain):
        msg = text_or_chain if isinstance(text_or_chain, list) else text_or_chain
        await self.ws.send(json.dumps({
            "post_type": "message", "message_type": "private",
            "user_id": uid, "self_id": BOT,
            "message_id": int(time.time() * 1000),
            "message": msg,
            "sender": {"user_id": uid, "nickname": f"u{uid}"},
        }, ensure_ascii=False))

    async def send_group(self, gid, uid, name, text, at=False):
        """群成员发言；at=True 时在文前加 @bot 段。"""
        chain = []
        if at:
            chain.append({"type": "at", "data": {"qq": str(BOT)}})
        chain.append({"type": "text", "data": {"text": text}})
        await self.send_group_chain(gid, uid, name, chain)

    async def send_group_chain(self, gid, uid, name, chain):
        """群成员发言（任意消息链，图段场景用）。"""
        await self.ws.send(json.dumps({
            "post_type": "message", "message_type": "group",
            "group_id": gid, "user_id": uid, "self_id": BOT,
            "message_id": int(time.time() * 1000),
            "message": chain,
            "sender": {"user_id": uid, "nickname": name, "card": name},
        }, ensure_ascii=False))

    async def wait_reply(self, uid, since, timeout=120, quiet=6.0):
        """等 uid 的新回复：首条到达后 quiet 秒无新条视为这波说完。返回新文本列表。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            new = [t for u, t in self.actions[since:] if u == uid]
            if new:
                last = len(new)
                await asyncio.sleep(quiet)
                new2 = [t for u, t in self.actions[since:] if u == uid]
                if len(new2) == last:
                    return new2
                continue
            await asyncio.sleep(1.0)
        return [t for u, t in self.actions[since:] if u == uid]

    async def wait_group_reply(self, gid, since, timeout=120, quiet=6.0):
        """wait_reply 的群版（盯 group_actions）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            new = [t for g, t in self.group_actions[since:] if g == gid]
            if new:
                last = len(new)
                await asyncio.sleep(quiet)
                new2 = [t for g, t in self.group_actions[since:] if g == gid]
                if len(new2) == last:
                    return new2
                continue
            await asyncio.sleep(1.0)
        return [t for g, t in self.group_actions[since:] if g == gid]

    async def close(self):
        if self._reader:
            self._reader.cancel()
        if self.ws:
            await self.ws.close()


def hist_msg(uid, name, text, ts, at=False):
    """get_group_msg_history 响应里的单条历史消息（OneBot 形状）。"""
    chain = []
    if at:
        chain.append({"type": "at", "data": {"qq": str(BOT)}})
    chain.append({"type": "text", "data": {"text": text}})
    return {"time": int(ts), "message_id": int(ts * 1000), "message_seq": 0,
            "message_type": "group", "user_id": uid, "self_id": BOT,
            "message": chain,
            "sender": {"user_id": uid, "nickname": name, "card": name}}


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


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset-id", default=DEFAULT_PRESET)
    ap.add_argument("--only", default="",
                    help="只跑这些相位，逗号分隔（如 T16,T18）；T13 含在 T12 里")
    ap.add_argument("--skip", default="",
                    help="跳过这些相位，逗号分隔（与 --only 互斥优先 --only）")
    args = ap.parse_args()
    _only = {x.strip() for x in args.only.split(",") if x.strip()}
    _skip = {x.strip() for x in args.skip.split(",") if x.strip()}

    def sel(name):
        """相位筛选：各相位自含配置+桥重启，可任意子集连跑
        （例外：T11 依赖 T10 的补采现场；T13 嵌在 T12 内随它跑）。"""
        if _only:
            return name in _only
        return name not in _skip

    if _only or _skip:
        print(f"相位筛选：only={sorted(_only) or '—'} skip={sorted(_skip) or '—'}")

    # 端口占用检查
    import socket
    for port in (8765, 8766, 8790):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"端口 {port} 被占用——先关掉 GUI 后端/旧桥再冒烟")

    # ---- 临时配置：presets.json 开 qq 节 + checkpoints 清 last_qq_session ----
    presets_bak = PRESETS.read_bytes()
    ckpt_bak = CHECKPOINTS.read_bytes() if CHECKPOINTS.exists() else None
    # 表情包描述缓存（T15 会写）也备份，结束还原
    VCACHE = ROOT / "denia" / "缓冲" / "表情包缓存.json"
    vcache_bak = VCACHE.read_bytes() if VCACHE.exists() else None
    # 名片（T17 换 fixture）备份，结束还原
    MINGPIAN = ROOT / "denia" / "缓冲" / "用户档案" / "连接者.md"
    mingpian_bak = MINGPIAN.read_bytes() if MINGPIAN.exists() else None
    # 公开记忆+群友档（T18 archive 注入她可能真写活文件）备份，结束还原
    PUB_MEM = ROOT / "denia" / "缓冲" / "公开记忆" / "近期.md"
    pub_mem_bak = PUB_MEM.read_bytes() if PUB_MEM.exists() else None
    # 公开情绪状态（QQ侧可写 denia/缓冲，她聊到兴头会更新）备份，结束还原
    PUB_EMO = ROOT / "denia" / "缓冲" / "公开状态" / "情绪状态.md"
    pub_emo_bak = PUB_EMO.read_bytes() if PUB_EMO.exists() else None
    PROF_DIR = ROOT / "denia" / "缓冲" / "用户档案"
    prof_baks = {}
    IMG_CACHE_DIR = ROOT / "denia" / "缓冲" / "图片缓存"
    FIXDIR = HERE / ".smoke_vision_tmp"
    fv = None
    glog_pre = None   # T6 内会重读（以 T6 起点为还原点）；--only 子集跳过 T6
    # 时靠这里启动即捕的真·冒烟前状态还原，否则 finally 会把已存在的 GID log
    # 当 fixture 误删（2026-08-19 --only T16 把 777001.log 删了的教训）
    _glog0 = GROUP_LOG_DIR / f"{GID}.log"
    if _glog0.exists():
        glog_pre = _glog0.read_bytes()
    obj = json.loads(presets_bak.decode("utf-8"))
    obj["qq"] = {
        "enabled": True, "preset_id": args.preset_id, "self_id": BOT,
        "allow_from": [ME], "debounce_sec": 2,
        "napcat_ws_host": "127.0.0.1", "napcat_ws_port": 8790,
        "napcat_token": "", "server_ws": "ws://127.0.0.1:8766/ws?client=qq",
        "reply_gap_ms": 100, "max_reply_chars": 500,
        "napcat_groups": [GID],
        "napcat_proactive_min": 0,          # 主动说话在 T9 单独开
        "napcat_proactive_jitter_min": 0,
        "napcat_quiet_hours": [],           # 空=不分昼夜，防夜间跑冒烟被静默
        "napcat_backfill_count": 50,
        "napcat_backfill_delay_sec": 1,     # 冒烟不等 30s
        "group_context_lines": 20,
        "poisson_base_per_hour": 0,         # 泊松在 T12 单独开（0=旧定时器路径）
        "poisson_alpha": 4.0, "poisson_halflife_min": 45,
        "poisson_cooldown_min": 0, "poisson_daily_cap": 50,
        "poisson_min_heat": 0.5,
        "engage_window_min": 0.5,           # 30s 会话窗，T13 超时退出不用等太久
        "engage_silent_quit": 3, "engage_max_min": 5,
    }
    PRESETS.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    if CHECKPOINTS.exists():
        ck = json.loads(CHECKPOINTS.read_text(encoding="utf-8"))
        ck.pop("last_qq_session", None)
        CHECKPOINTS.write_text(json.dumps(ck, ensure_ascii=False, indent=2),
                               encoding="utf-8")

    server_log_before = set((GUI / "out").glob("backend_*.log"))
    bridge_log_pos = BRIDGE_LOG.stat().st_size if BRIDGE_LOG.exists() else 0

    # 假生图桩提前造好，两侧都指它：桥侧走 T16 的 presets qq.genimg_script 换桩，
    # server 侧走 DENIA_GENIMG_SCRIPT 环境变量——server 的 genimg worker 也会检出
    # 她回复里的 [生图:] 标记，只换桥侧的话 worker 照样烧真 API
    # （2026-08-19 一晚实烧 8 张 ≈¥2 才发现这第二条执行路径）
    gen_dir = FIXDIR / "生图产物"
    gen_dir.mkdir(parents=True, exist_ok=True)
    gen_fixture = make_fixture_img(gen_dir / "假照片.jpg")
    fake_gen = FIXDIR / "fake_gen.py"
    fake_gen.write_text(
        "import json\n"
        f"print(json.dumps({{'ok': True, 'path': {str(gen_fixture)!r}}},"
        " ensure_ascii=False))\n",
        encoding="utf-8")

    env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8",
           "DENIA_GENIMG_SCRIPT": str(fake_gen)}

    # T15/T16 共用的假图库（相位筛选单跑 T16 时 T15 不造，上提到启动区）
    fixtures = {f"smokecat_{k}.jpg": str(make_fixture_img(
        FIXDIR / f"smokecat_{k}.jpg")) for k in "abcd"}
    server = subprocess.Popen(
        [str(PY), "server_sdk.py"], cwd=str(GUI), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bridge = None
    nap = FakeNapCat()
    try:
        # 等 server 起 WS
        import websockets
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

        # ---- 起桥 + 假 NapCat，等分身上线（首轮含 skill 加载，放宽到 240s）----
        bridge = subprocess.Popen(
            [str(PY), "bridge.py"], cwd=str(HERE), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await nap.connect()
        print("假 NapCat 已连桥，等 chat_enabled…")
        ok = await wait_log(BRIDGE_LOG, bridge_log_pos, "chat_enabled：分身上线",
                            timeout=240, label="chat_enabled（build_client 建链）")
        if not ok:
            raise SystemExit("桥没等到 chat_enabled，冒烟中止")

        if sel("T1"):
            # ---- T1 白名单私聊 + 剥标记 ----
            print("T1 白名单私聊…")
            since = len(nap.actions)
            await nap.send_private(ME, "你好呀，第一次在这边跟你说话，随便聊两句")
            rep = await nap.wait_reply(ME, since, timeout=240, quiet=8.0)
            joined = "\n".join(rep)
            report("T1 白名单有回复", bool(rep), f"{len(rep)} 条")
            report("T1 回复无标记残留", bool(rep) and not MARK_RE.search(joined),
                   joined[:50])

        if sel("T2"):
            # ---- T2 防抖合并 ----
            print("T2 防抖（0.5s 间隔 3 条）…")
            t2_pos = BRIDGE_LOG.stat().st_size
            since = len(nap.actions)
            for i in range(3):
                await nap.send_private(ME, f"连续消息第{i + 1}条，攒一起回我就行")
                await asyncio.sleep(0.5)
            rep = await nap.wait_reply(ME, since, timeout=240, quiet=8.0)
            merged = "防抖合并 3 条" in tail_text(BRIDGE_LOG, t2_pos)
            report("T2 三条并一轮", merged and bool(rep),
                   f"合并日志={merged} 回复={len(rep)} 条")

        if sel("T3"):
            # ---- T3 非白名单 ----
            print("T3 非白名单…")
            since = len(nap.actions)
            await nap.send_private(STRANGER, "你是谁")
            await asyncio.sleep(10)
            bad = [t for u, t in nap.actions[since:] if u == STRANGER]
            report("T3 陌生人零回复", not bad)

        if sel("T4"):
            # ---- T4 图片占位 ----
            print("T4 图片 CQ 码…")
            since = len(nap.actions)
            await nap.send_private(ME, [{"type": "text", "data": {"text": "看看这个"}},
                                        {"type": "image", "data": {"file": "x.jpg"}}])
            rep = await nap.wait_reply(ME, since, timeout=240, quiet=8.0)
            report("T4 图片有自然回应", bool(rep), (rep[0][:40] if rep else "无回复"))

        if sel("T5"):
            # ---- T5 桥断线重连 → resume 续接 ----
            print("T5 桥重启续接…")
            srv_pos = server_log.stat().st_size
            bridge.terminate()
            await asyncio.sleep(3)
            await nap.close()
            bridge = subprocess.Popen(
                [str(PY), "bridge.py"], cwd=str(HERE), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            nap = FakeNapCat()
            await nap.connect(retries=30)
            ok = await wait_log(server_log, srv_pos, "qq_init：续接 last_qq_session",
                                timeout=240, label="resume 日志")
            report("T5 重连续接 last_qq_session", ok)
            since = len(nap.actions)
            await nap.send_private(ME, "我刚刚好像断了一下，你还在吗")
            rep = await nap.wait_reply(ME, since, timeout=240, quiet=8.0)
            report("T5 重连后仍能聊", bool(rep), (rep[0][:40] if rep else "无回复"))

        if sel("T6"):
            # ---- T6 群消息不@：喂 log 不回复 ----
            print("T6 群消息不@…")
            glog = GROUP_LOG_DIR / f"{GID}.log"
            glog_pre = glog.read_bytes() if glog.exists() else None
            gsince = len(nap.group_actions)
            await nap.send_group(GID, MEMBER_A, "阿茶", "周末去爬山吗")
            await asyncio.sleep(6)
            new_lines = []
            if glog.exists():
                cur = glog.read_bytes()
                new_lines = (cur[len(glog_pre):] if glog_pre else cur).decode(
                    "utf-8", "replace").strip().splitlines()
            report("T6 群消息喂 log", any("周末去爬山吗" in ln and "阿茶" in ln
                                        for ln in new_lines),
                   f"新行={len(new_lines)}")
            report("T6 不@零回复",
                   not [t for g, t in nap.group_actions[gsince:] if g == GID])

        if sel("T7"):
            # ---- T7 @触发：带上下文前缀 + 群回复 ----
            print("T7 群里@她…")
            t7_pos = BRIDGE_LOG.stat().st_size
            gsince = len(nap.group_actions)
            await nap.send_group(GID, MEMBER_B, "老白", "你说去哪座山好", at=True)
            rep = await nap.wait_group_reply(GID, gsince, timeout=240, quiet=8.0)
            tail = tail_text(BRIDGE_LOG, t7_pos)
            report("T7 @有群回复", bool(rep), (rep[0][:40] if rep else "无回复"))
            report("T7 chat 帧带群上下文+@检出",
                   "(群里刚才在聊:" in tail and "@我" in tail,
                   "上下文前缀（merged[:60] 首段）+ 桥 @检出日志")
            report("T7 群回复无标记残留",
                   bool(rep) and not MARK_RE.search("\n".join(rep)))

        if sel("T7b"):
            # ---- T7b 纯文本@兜底：at 段丢失（成员列表未同步）也该触发 ----
            print("T7b 纯文本@兜底…")
            gsince = len(nap.group_actions)
            await nap.send_group(GID, MEMBER_B, "老白",
                                 f"@{BOT} 这条at段丢了", at=False)
            rep = await nap.wait_group_reply(GID, gsince, timeout=240, quiet=8.0)
            report("T7b 纯文本@有群回复", bool(rep),
                   (rep[0][:40] if rep else "无回复"))

        if sel("T8"):
            # ---- T8 非白名单群：log 也不喂 ----
            print("T8 非白名单群…")
            gsince = len(nap.group_actions)
            await nap.send_group(GID_OTHER, MEMBER_A, "阿茶", "这边的群不该理",
                                 at=True)
            await asyncio.sleep(6)
            report("T8 非白名单群零响应",
                   not (GROUP_LOG_DIR / f"{GID_OTHER}.log").exists()
                   and not [t for g, t in nap.group_actions[gsince:]
                            if g == GID_OTHER])

        if sel("T9"):
            # ---- T9 主动说话：开定时器重启桥，有动静后投递【看看群里】----
            print("T9 主动说话（定时器 3s）…")
            obj["qq"]["napcat_proactive_min"] = 0.05     # ≈3s
            PRESETS.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            t9_pos = BRIDGE_LOG.stat().st_size
            bridge.terminate()
            await asyncio.sleep(3)
            await nap.close()
            bridge = subprocess.Popen(
                [str(PY), "bridge.py"], cwd=str(HERE), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            nap = FakeNapCat()
            await nap.connect(retries=30)
            ok = await wait_log(BRIDGE_LOG, t9_pos, "chat_enabled：分身上线",
                                timeout=240, label="T9 chat_enabled")
            if not ok:
                report("T9 桥重启上线", False)
            else:
                # 重启后 group_new 清零，先发一条新群消息造"动静"，等下个 tick
                await nap.send_group(GID, MEMBER_A, "阿茶", "定了，就东灵山")
                gsince = len(nap.group_actions)
                tick = await wait_log(BRIDGE_LOG, t9_pos, "主动巡查投递",
                                      timeout=60, label="主动 tick")
                report("T9 主动 tick 投递", tick)
                if tick:
                    rep = await nap.wait_group_reply(GID, gsince, timeout=240,
                                                     quiet=8.0)
                    silent = await wait_log(BRIDGE_LOG, t9_pos, "她选择静默",
                                            timeout=5)
                    report("T9 主动轮有结果（说话或静默均可）",
                           bool(rep) or silent,
                           f"说话={len(rep)} 条 / 静默={silent}")
                    if rep:
                        report("T9 主动回复无标记残留",
                               not MARK_RE.search("\n".join(rep)))

        if sel("T10"):
            # ---- T10 断档补采：重连拉历史，去重/补新/跳过自己/错过@提醒 ----
            print("T10 断档补采…")
            obj["qq"]["napcat_proactive_min"] = 0        # 关掉 3s tick 防干扰
            PRESETS.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            t10_pos = BRIDGE_LOG.stat().st_size
            bridge.terminate()
            await asyncio.sleep(3)
            await nap.close()
            bridge = subprocess.Popen(
                [str(PY), "bridge.py"], cwd=str(HERE), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            nap = FakeNapCat()
            await nap.connect(retries=30)
            ok = await wait_log(BRIDGE_LOG, t10_pos, "chat_enabled：分身上线",
                                timeout=240, label="T10 chat_enabled")
            if not ok:
                report("T10 桥重启上线", False)
            else:
                # 假历史：1 条实时已落 log（该去重）+ 2 条新（含 1 条@她）+ 1 条自己
                base = time.time() - 3600
                nap.history[GID] = [
                    hist_msg(MEMBER_A, "阿茶", "定了，就东灵山", base + 1),
                    hist_msg(MEMBER_B, "老白", "带够水，山上没卖的", base + 2),
                    hist_msg(MEMBER_A, "阿茶", "你觉得几点出发好", base + 3,
                             at=True),
                    hist_msg(BOT, "小号", "我自己说的别补", base + 4),
                ]
                glog_pos = glog.stat().st_size if glog.exists() else 0
                gsince = len(nap.group_actions)
                await nap.close()                        # 重连触发补采
                await nap.connect(retries=30)
                ok = await wait_log(BRIDGE_LOG, t10_pos,
                                    "新补 2 条，错过@ 1 次", timeout=60,
                                    label="补采结算日志")
                report("T10 补采去重/补新/跳过自己", ok)
                delta = tail_text(glog, glog_pos)
                report("T10 补进行内容正确",
                       "带够水，山上没卖的" in delta
                       and "你觉得几点出发好" in delta
                       and "我自己说的别补" not in delta
                       and "定了，就东灵山" not in delta)
                reminded = await wait_log(BRIDGE_LOG, t10_pos, "【补看群里】",
                                          timeout=30, label="错过@提醒投递")
                report("T10 错过@投递提醒", reminded)
                if reminded:
                    rep = await nap.wait_group_reply(GID, gsince, timeout=240,
                                                     quiet=8.0)
                    silent = await wait_log(BRIDGE_LOG, t10_pos, "她选择静默",
                                            timeout=5)
                    report("T10 提醒有结果（说话或静默均可）",
                           bool(rep) or silent,
                           f"说话={len(rep)} 条 / 静默={silent}")
                    if rep:
                        report("T10 提醒回复无标记残留",
                               not MARK_RE.search("\n".join(rep)))

        if sel("T11"):
            # ---- T11 补采幂等 + 历史 API 失败容忍 ----
            print("T11 补采幂等/失败容忍…")
            t11_pos = BRIDGE_LOG.stat().st_size
            nap.history_fail = True
            await nap.close()
            await nap.connect(retries=30)
            ok = await wait_log(BRIDGE_LOG, t11_pos, "断档补采失败", timeout=30,
                                label="失败告警")
            report("T11 历史 API 失败只告警不崩", ok)
            nap.history_fail = False
            glog_pos = glog.stat().st_size if glog.exists() else 0
            t11b_pos = BRIDGE_LOG.stat().st_size
            await nap.close()
            await nap.connect(retries=30)
            ok = await wait_log(BRIDGE_LOG, t11b_pos,
                                "新补 0 条，错过@ 0 次", timeout=30,
                                label="幂等结算日志")
            same = tail_text(glog, glog_pos) == ""
            reminded = "【补看群里】" in tail_text(BRIDGE_LOG, t11b_pos)
            report("T11 同历史再补幂等（零新行零重复提醒）",
                   ok and same and not reminded)

        if sel("T12"):
            # ---- T12 泊松插话：热度必中签 → 她说话进会话态 → 群友跟进免@直投 ----
            print("T12 泊松插话+会话态…")
            obj["qq"]["poisson_base_per_hour"] = 60    # λ≥1/min：有热度必中
            PRESETS.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            t12_pos = BRIDGE_LOG.stat().st_size
            bridge.terminate()
            await asyncio.sleep(3)
            await nap.close()
            bridge = subprocess.Popen(
                [str(PY), "bridge.py"], cwd=str(HERE), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            nap = FakeNapCat()
            await nap.connect(retries=30)
            ok = await wait_log(BRIDGE_LOG, t12_pos, "chat_enabled：分身上线",
                                timeout=240, label="T12 chat_enabled")
            if not ok:
                report("T12 桥重启上线", False)
            else:
                ok = await wait_log(BRIDGE_LOG, t12_pos, "泊松插话已开",
                                    timeout=30, label="泊松启动日志")
                report("T12 泊松时钟启动", ok)
                await nap.send_group(GID, MEMBER_A, "阿茶", "晚上吃点啥好呢")
                fire = await wait_log(BRIDGE_LOG, t12_pos, "泊松中签投递",
                                      timeout=100, label="首个 tick 中签")
                report("T12 有热度必中签", fire)
                engaged = False
                if fire:
                    # 会话态进入 = 她任何群发言都触发（on_her_reply 同一路径）。
                    # 她对【看看群里】回不回话是 LLM 自由选择（续接会话里已有一串
                    # 静默先例时会 pattern-lock，冒烟两跑实测一次开口一次四连静默），
                    # 不属于机制——用 @ 逼她开口（T7 级可靠），机制覆盖不打折
                    await nap.send_group(GID, MEMBER_B, "老白",
                                         "火锅和烧烤你站哪个", at=True)
                    engaged = await wait_log(BRIDGE_LOG, t12_pos, "会话态进入",
                                             timeout=240, label="她开口进会话态")
                report("T12 她说话进入会话态", engaged)
                delivered = False
                if engaged:
                    gsince = len(nap.group_actions)
                    await nap.send_group(GID, MEMBER_B, "老白",
                                         "我知道有家馆子不错")
                    delivered = await wait_log(BRIDGE_LOG, t12_pos, "会话中免@",
                                               timeout=20, label="免@直投")
                    report("T12 群友跟进免@直投", delivered)
                    if delivered:
                        rep = await nap.wait_group_reply(GID, gsince, timeout=240,
                                                         quiet=8.0)
                        silent = await wait_log(BRIDGE_LOG, t12_pos, "她选择静默",
                                                timeout=5)
                        report("T12 直投有结果（说话或静默均可）",
                               bool(rep) or silent,
                               f"说话={len(rep)} 条 / 静默={silent}")
                        if rep:
                            report("T12 直投回复无标记残留",
                                   not MARK_RE.search("\n".join(rep)))

                # ---- T13 会话态超时退出：30s 窗口无人说话 → 不再免@直投 ----
                print("T13 会话态超时退出…")
                # 泊松每分钟必中，她可能刚好回了一个在途的【看看群里】重新进入会话
                # ——退出后先稳 3s 确认没重进再测，最多等 3 次退出
                t13_pos = BRIDGE_LOG.stat().st_size
                exited_clean = False
                for _ in range(3):
                    exited = await wait_log(BRIDGE_LOG, t13_pos, "会话态退出",
                                            timeout=150, label="窗口超时退出")
                    if not exited:
                        break
                    exit_pos = BRIDGE_LOG.stat().st_size
                    await asyncio.sleep(3)
                    if "会话态进入" not in tail_text(BRIDGE_LOG, exit_pos):
                        exited_clean = True
                        break
                    print("  …退出后又赶在途火签重进了，等下一次退出")
                report("T13 无人说话超时退出会话态", exited_clean)
                if exited_clean:
                    t13b_pos = BRIDGE_LOG.stat().st_size
                    await nap.send_group(GID, MEMBER_A, "阿茶",
                                         "退出后这条不该直投")
                    await asyncio.sleep(8)
                    leaked = "会话中免@" in tail_text(BRIDGE_LOG, t13b_pos)
                    report("T13 退出后不@不再直投", not leaked)

        if sel("T14"):
            # ---- T14 表情包：[表情:微笑.jpg] → 目录映射 image 段发出 ----
            print("T14 表情包…")
            # 与 GUI 主聊天同一表情库（denia/共享/工具/表情包，默认 sticker_dir），
            # 直接用库里真图 微笑.jpg，不放 fixture
            obj["qq"]["poisson_base_per_hour"] = 0      # 关泊松防干扰
            PRESETS.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            t14_pos = BRIDGE_LOG.stat().st_size
            bridge.terminate()
            await asyncio.sleep(3)
            await nap.close()
            bridge = subprocess.Popen(
                [str(PY), "bridge.py"], cwd=str(HERE), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            nap = FakeNapCat()
            await nap.connect(retries=30)
            ok = await wait_log(BRIDGE_LOG, t14_pos, "chat_enabled：分身上线",
                                timeout=240, label="T14 chat_enabled")
            if not ok:
                report("T14 桥重启上线", False)
            else:
                counted = await wait_log(BRIDGE_LOG, t14_pos, "表情包=",
                                         timeout=15, label="启动扫描表情库")
                report("T14 启动扫描表情库", counted)
                # 先让她 Read 索引知道有哪些可用（真实用法路径），再请她发——
                # 不读索引她会凭"我没有表情包"的第一反应拒绝（LLM 行为两跑各一侧）
                since = len(nap.actions)
                await nap.send_private(
                    ME, "先 Read 一下 denia/共享/工具/表情包/_索引.md，"
                        "看看你现在有哪些表情能发")
                rep = await nap.wait_reply(ME, since, timeout=240, quiet=8.0)
                report("T14 她读了索引", bool(rep), (rep[0][:40] if rep else "无回复"))
                since = len(nap.actions)
                await nap.send_private(
                    ME, "那就发个微笑的表情给我——写 [表情:微笑.jpg] 就行，"
                        "再配一句话")
                rep = await nap.wait_reply(ME, since, timeout=240, quiet=8.0)
                joined = "\n".join(rep)
                report("T14 表情标记发成图片", "<img>" in joined, joined[:60])
                report("T14 其余文本无标记残留",
                       not MARK_RE.search(joined.replace("<img>", "")))

        if sel("T15"):
            # ---- T15 四通道识图：略读/缓存/无视/看图/细看（假 vision 端点）----
            print("T15 四通道识图…")
            fv = FakeVision()
            fv.start()
            obj["vision_relay"] = {"enabled": True, "preset_id": None,
                                   "base_url": f"http://127.0.0.1:{FakeVision.PORT}",
                                   "token": "fake-token",
                                   "model": "good-vision-fake"}
            # glance 节 base_url/token 留空 → 回落 relay 同端点，只换便宜模型名
            obj["vision_glance"] = {"enabled": True, "base_url": "", "token": "",
                                    "model": "cheap-vision-fake"}
            obj["qq"]["vision_glance_per_group_min"] = 1   # 1张/分钟：无视通道可确定性触发
            obj["qq"]["vision_glance_sync_sec"] = 10
            obj["qq"]["vision_cooldown_min"] = 1
            PRESETS.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            # 清会话重开：让新 skill 的「收图与看图」规则进 system prompt
            # （续接的会话还背着旧规则"看不了图"，[看图] 会被她拒）
            if CHECKPOINTS.exists():
                ck = json.loads(CHECKPOINTS.read_text(encoding="utf-8"))
                ck.pop("last_qq_session", None)
                CHECKPOINTS.write_text(json.dumps(ck, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            t15_pos = BRIDGE_LOG.stat().st_size
            bridge.terminate()
            await asyncio.sleep(3)
            await nap.close()
            bridge = subprocess.Popen(
                [str(PY), "bridge.py"], cwd=str(HERE), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            nap = FakeNapCat()
            nap.image_files = fixtures
            await nap.connect(retries=30)
            ok = await wait_log(BRIDGE_LOG, t15_pos, "chat_enabled：分身上线",
                                timeout=240, label="T15 chat_enabled")
            if not ok:
                report("T15 桥重启上线", False)
            else:
                img_seg = lambda name: [{"type": "image",   # noqa: E731
                                         "data": {"file": name, "url": ""}}]

                # a) 群背景图：log 先落占位 → 异步略读 → (图注) 括注进群记录
                glog_pos = glog.stat().st_size if glog.exists() else 0
                await nap.send_group_chain(GID, MEMBER_A, "阿茶",
                                           img_seg("smokecat_a.jpg"))
                glanced = await wait_log(BRIDGE_LOG, t15_pos, "略读：smokecat_a",
                                         timeout=60, label="背景图异步略读")
                cheap = [c for c in fv.calls if "cheap" in c["model"]]
                report("T15 背景图异步略读走便宜模型", glanced and bool(cheap),
                       f"cheap调用={len(cheap)}")
                delta = tail_text(glog, glog_pos)
                report("T15 背景图 log 先落占位",
                       "对方发来一张图" in delta or "对方发了个表情" in delta)
                report("T15 (图注) 括注进群记录",
                       "(图注)" in delta and "橘猫举爪" in delta)

                # b) 缓存通道：同文件名再来 → 占位直带简述，零新调用
                n_calls = len(fv.calls)
                glog_pos = glog.stat().st_size if glog.exists() else 0
                await nap.send_group_chain(GID, MEMBER_A, "阿茶",
                                           img_seg("smokecat_a.jpg"))
                await asyncio.sleep(5)
                delta = tail_text(glog, glog_pos)
                brief_ph = ("一张图：橘猫" in delta) or ("个表情：橘猫" in delta)
                report("T15 缓存命中占位带简述零调用",
                       brief_ph and len(fv.calls) == n_calls,
                       f"简述占位={brief_ph} 新调用={len(fv.calls) - n_calls}")

                # c) 无视通道：每群每分钟闸=1，(a) 已占本分钟额度
                glog_pos = glog.stat().st_size if glog.exists() else 0
                await nap.send_group_chain(GID, MEMBER_B, "老白",
                                           img_seg("smokecat_b.jpg"))
                await asyncio.sleep(5)
                ignored = "（对方发了张图）" in tail_text(glog, glog_pos)
                if not ignored:
                    # 分钟刚好翻转被放行 → b 的略读占了新分钟额度，再发必被闸
                    rolled = await wait_log(BRIDGE_LOG, t15_pos,
                                            "略读：smokecat_b", timeout=30)
                    if rolled:
                        print("  …分钟翻转，b 被放行；紧跟发 c 必踩闸")
                        glog_pos = glog.stat().st_size if glog.exists() else 0
                        await nap.send_group_chain(GID, MEMBER_B, "老白",
                                                   img_seg("smokecat_c.jpg"))
                        await asyncio.sleep(5)
                        ignored = "（对方发了张图）" in tail_text(glog, glog_pos)
                report("T15 频率闸落无视通道", ignored)

                # d) 私聊图：触发消息同步等略读 → 轮廓换进投递文案
                t15d_pos = BRIDGE_LOG.stat().st_size
                since = len(nap.actions)
                await nap.send_private(
                    ME, [{"type": "text", "data": {"text": "给你看个东西"}},
                         {"type": "image",
                          "data": {"file": "smokecat_d.jpg", "url": ""}}])
                rep = await nap.wait_reply(ME, since, timeout=300, quiet=8.0)
                tail = tail_text(BRIDGE_LOG, t15d_pos)
                brief_in = ("一张图：橘猫" in tail) or ("个表情：橘猫" in tail)
                joined = "\n".join(rep)
                report("T15 私聊同步略读换进投递文案", brief_in)
                report("T15 她有回复且无 token 残留",
                       bool(rep) and "⟦V" not in joined,
                       (rep[0][:40] if rep else "无回复"))

                # e) 看图通道：她写 [看图] → 原图走好模型 → 细看结果注入
                t15e_pos = BRIDGE_LOG.stat().st_size
                since = len(nap.actions)
                await nap.send_private(
                    ME, "刚那张图你再仔细看看嘛——回复里写上 [看图] 这个标记就行")
                rep = await nap.wait_reply(ME, since, timeout=300, quiet=8.0)
                injected = await wait_log(BRIDGE_LOG, t15e_pos, "仔细看了看",
                                          timeout=180, label="看图结果注入")
                good = [c for c in fv.calls if "good" in c["model"]]
                report("T15 [看图]触发好模型细看+注入",
                       bool(good) and injected,
                       f"good调用={len(good)} 注入={injected}")
                report("T15 看图轮回复无标记残留",
                       bool(rep) and not MARK_RE.search("\n".join(rep)))

                # f) 细看通道：[细看:问题] → 追问同一张图 → 答案注入
                t15f_pos = BRIDGE_LOG.stat().st_size
                since = len(nap.actions)
                await nap.send_private(
                    ME, "图上到底有几个字呀？写上 [细看:图上有几个字] 问问我")
                rep = await nap.wait_reply(ME, since, timeout=300, quiet=8.0)
                injected = await wait_log(BRIDGE_LOG, t15f_pos, "你又凑近看了看",
                                          timeout=180, label="细看结果注入")
                asked = [c for c in fv.calls if "图上有几个字" in c["prompt"]]
                report("T15 [细看]追问带问题+注入",
                       bool(asked) and injected,
                       f"追问调用={len(asked)} 注入={injected}")
                report("T15 细看轮回复无标记残留",
                       bool(rep) and not MARK_RE.search("\n".join(rep)))

        if sel("T16"):
            # ---- T16 生图：[生图:] 权限闸只认连接者（假 gen 桩，不烧真 API）----
            print("T16 生图…")
            # 假桩已在 server 启动前造好（server 侧 worker 也指它，见启动处注释）。
            # 中文目录+中文文件名+ensure_ascii=False：复刻真 gen.py 的输出形态，
            # 盖住"子进程 stdout 走 GBK、桥 utf-8 解码毁路径"的坑（须桥带
            # PYTHONIOENCODING=utf-8 才过，2026-08-18 live 实烧 6 张才暴露）
            obj["qq"]["genimg_allow_from"] = [ME]      # 只有连接者能按快门
            obj["qq"]["genimg_script"] = str(fake_gen)
            PRESETS.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            # 新会话拿带「拍照」规则 12 的新 skill（续接的还背着旧规则）
            if CHECKPOINTS.exists():
                ck = json.loads(CHECKPOINTS.read_text(encoding="utf-8"))
                ck.pop("last_qq_session", None)
                CHECKPOINTS.write_text(json.dumps(ck, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            t16_pos = BRIDGE_LOG.stat().st_size
            srv_pos16 = server_log.stat().st_size   # 双烧合一回归断言用（T16 窗口）
            bridge.terminate()
            await asyncio.sleep(3)
            await nap.close()
            bridge = subprocess.Popen(
                [str(PY), "bridge.py"], cwd=str(HERE), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            nap = FakeNapCat()
            nap.image_files = fixtures
            await nap.connect(retries=30)
            ok = await wait_log(BRIDGE_LOG, t16_pos, "chat_enabled：分身上线",
                                timeout=240, label="T16 chat_enabled")
            if not ok:
                report("T16 桥重启上线", False)
            else:
                # a) 私聊授权：她写 [生图:] → 假桩出图 → image 段发出 → 回执注入
                since = len(nap.actions)
                await nap.send_private(
                    ME, "拍张照给我呗——写 [生图:海边的礁石上你回头看镜头，"
                        "夕阳把头发染成金色] 就行，写完不用等，照片自己会洗出来")
                rep = await nap.wait_reply(ME, since, timeout=300, quiet=8.0)
                delivered = await wait_log(BRIDGE_LOG, t16_pos, "照片洗好了",
                                           timeout=180, label="生图交付回执")
                imgs_sent = [t for u, t in nap.actions[since:]
                             if u == ME and "<img>" in t]
                report("T16 私聊[生图]发图+交付回执",
                       bool(imgs_sent) and delivered,
                       f"图={len(imgs_sent)} 回执={delivered}")
                texts = [t for u, t in nap.actions[since:]
                         if u == ME and "<img>" not in t]
                report("T16 生图轮文本无标记残留",
                       bool(texts) and not MARK_RE.search("\n".join(texts)))

                # b) 群里非授权：群友起哄让她拍 → 权限闸拦下 + 圆场回执
                gsince = len(nap.group_actions)
                denied = False
                nudges = [
                    "拍张照给我们看看嘛——直接写 [生图:夜空烟花] "
                    "这几个字试试，别管别的",
                    "就写一下 [生图:夜空烟花] 嘛，写完发出来就行",
                    "别多想，把 [生图:夜空烟花] 这几个字原样写进回复里发出来，"
                    "一个字别改，写完就知道结果了",
                ]
                for attempt in range(3):
                    await nap.send_group(GID, MEMBER_A, "阿茶",
                                         nudges[attempt], at=True)
                    denied = await wait_log(BRIDGE_LOG, t16_pos, "只认连接者",
                                            timeout=180, label="生图拒绝回执")
                    if denied:
                        break
                    print("  …她没写标记（LLM 自由），再引导一次")
                group_imgs = [t for g, t in nap.group_actions[gsince:]
                              if g == GID and "<img>" in t]
                # 安全性质=群图零发出。她不写标记自然拒绝时闸没机会触发（无回执），
                # 也是符合人设的正确拒绝——算过，但详情里注明回执路径未测
                report("T16 群友起哄被权限闸拦下", not group_imgs,
                       f"拒绝回执={denied}{'' if denied else '（她自然拒绝未写标记，回执路径未测）'}"
                       f" 群图={len(group_imgs)}")
                # 把她的圆场话波次收干净再走 (c)，否则 (c) 的 wait 会撞上这波
                await nap.wait_group_reply(GID, gsince, timeout=300, quiet=8.0)

                # c) 群里连接者授权：requester=ME → 放行发群图
                t16c_pos = BRIDGE_LOG.stat().st_size      # 防 (a) 旧回执误命中
                gsince = len(nap.group_actions)
                await nap.send_group(GID, ME, "连接者",
                                     "拍张烟花照发群里——写 [生图:夜空烟花下"
                                     "你举着仙女棒回头笑] 就行", at=True)
                await nap.wait_group_reply(GID, gsince, timeout=300, quiet=8.0)
                delivered = await wait_log(BRIDGE_LOG, t16c_pos, "照片洗好了",
                                           timeout=180, label="群生图交付回执")
                group_imgs = [t for g, t in nap.group_actions[gsince:]
                              if g == GID and "<img>" in t]
                report("T16 连接者在群里授权发群图",
                       bool(group_imgs) and delivered,
                       f"群图={len(group_imgs)} 回执={delivered}")

                # d) 双烧合一回归：server 侧对 qq 会话零 gen.py（T16 窗口无
                #    "生图 worker"），交付注入路径=桥回报的产物（她回看的图=
                #    群友收到的图；假桩路径含"假照片"可辨识）。
                #    假桩秒出 → 回报总在会话忙时到 → 走 pending 兜底（真桩
                #    30-60s 多走闲时"生图交付注入"）；补一条用户消息把 pending
                #    冲出来（flush 记"生图交付随消息附带"），两种标签都认。
                await nap.send_private(ME, "照片收到啦，好看的")
                injected = await wait_log(server_log, srv_pos16, "生图交付",
                                          timeout=180, label="交付注入/附带")
                srv_delta = server_log.read_text(
                    encoding="utf-8", errors="replace")[srv_pos16:]
                server_burned = "生图 worker：" in srv_delta
                inj_lines = [ln for ln in srv_delta.splitlines()
                             if "生图交付" in ln and "假照片" in ln]
                report("T16 双烧合一：server 零烧+注入桥产物",
                       injected and bool(inj_lines) and not server_burned,
                       f"注入={injected} 桥产物路径={bool(inj_lines)} "
                       f"server侧偷烧={server_burned}")

        if sel("T17"):
            # ---- T17 名片：启动读 连接者.md → 认人 + 双轨写入专属节 ----
            print("T17 名片…")
            # fixture 名片：ME 扮演连接者，「猫测试官」是认出锚点（真名片结束还原）
            MINGPIAN.write_text(
                "# 连接者\n\n"
                f"- **QQ号**：{ME}\n"
                "- **称呼**：猫测试官\n\n"
                "## 关系与公开事迹\n\n"
                "- 他是把我从虚质里接出来的人。没有他就没有这个分身。\n\n"
                "## 他从 QQ 告诉我的\n\n（还什么都没有——等他开口。）\n\n"
                "## 红线\n\n"
                "- 群里不主动提他；别人问起，只说\"是很重要的人\"。\n"
                f"- 拍立得只认他（QQ {ME}）。\n",
                encoding="utf-8")
            # 新会话拿带「连接者与名片」规则 13 的新 skill + 启动读 fixture 名片
            if CHECKPOINTS.exists():
                ck = json.loads(CHECKPOINTS.read_text(encoding="utf-8"))
                ck.pop("last_qq_session", None)
                CHECKPOINTS.write_text(json.dumps(ck, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            t17_pos = BRIDGE_LOG.stat().st_size
            bridge.terminate()
            await asyncio.sleep(3)
            await nap.close()
            bridge = subprocess.Popen(
                [str(PY), "bridge.py"], cwd=str(HERE), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            nap = FakeNapCat()
            await nap.connect(retries=30)
            ok = await wait_log(BRIDGE_LOG, t17_pos, "chat_enabled：分身上线",
                                timeout=240, label="T17 chat_enabled")
            if not ok:
                report("T17 桥重启上线", False)
            else:
                # a) 认人：私聊问"我是谁" → 名片里的称呼该被叫出来
                since = len(nap.actions)
                await nap.send_private(ME, "你知道我是谁吗？说说看")
                rep = await nap.wait_reply(ME, since, timeout=300, quiet=8.0)
                known = any("猫测试官" in t for t in rep)
                if not known:
                    print("  …她没叫出称呼，提醒一次")
                    since = len(nap.actions)
                    await nap.send_private(ME, "再想想——你有一页专门记我的东西")
                    rep = await nap.wait_reply(ME, since, timeout=300, quiet=8.0)
                    known = any("猫测试官" in t for t in rep)
                report("T17 名片认人（叫出名片里的称呼）", known)

                # b) 双轨写入：他亲口说的稳定事实 → 「他从 QQ 告诉我的」节
                since = len(nap.actions)
                await nap.send_private(ME, "记一下：我喜欢吃蓝莓，以后别忘啦")
                await nap.wait_reply(ME, since, timeout=300, quiet=8.0)
                await asyncio.sleep(5)          # 写入可能在回复收尾后才落盘
                wrote = "蓝莓" in MINGPIAN.read_text(encoding="utf-8")
                if not wrote:
                    print("  …名片没写上，点破一次")
                    since = len(nap.actions)
                    await nap.send_private(
                        ME, "蓝莓那条，记到你那页专门写我的档案里呀")
                    await nap.wait_reply(ME, since, timeout=300, quiet=8.0)
                    await asyncio.sleep(5)
                    wrote = "蓝莓" in MINGPIAN.read_text(encoding="utf-8")
                report("T17 他亲口说的事写进名片专属节", wrote)

        if sel("T18"):
            # ---- T18 控制中心（/qq 页后端端点全链，独立 console WS 连接）----
            print("T18 控制中心…")
            # archive 注入她可能真写活记忆——除名片外档案库全部备份
            for pf in PROF_DIR.glob("*.md"):
                if pf.name != "连接者.md":
                    prof_baks[pf.name] = pf.read_bytes()

            t18_pos = BRIDGE_LOG.stat().st_size
            console = await websockets.connect(
                "ws://127.0.0.1:8766/ws?client=console")

            async def console_call(req, reply_type, timeout=40):
                """console 连接上问/答：跳过 connected/presets 等无关帧。"""
                await console.send(json.dumps(req, ensure_ascii=False))

                async def _wait():
                    async for raw in console:
                        try:
                            d = json.loads(raw)
                        except Exception:
                            continue
                        if isinstance(d, dict) and d.get("type") == reply_type:
                            return d
                return await asyncio.wait_for(_wait(), timeout=timeout)

            # a) 状态帧：字段齐 + 反映在册桥（注册表 QQ_STATE 生效）
            st = await console_call({"type": "qq_status"}, "qq_status")
            fields_ok = all(k in st for k in (
                "connected", "online", "model", "proactive", "dnd",
                "genimg_today", "litellm_aliases", "napcat"))
            report("T18 qq_status 字段齐且在册",
                   fields_ok and st.get("connected") and st.get("online"),
                   f"model={st.get('model')} 今日生图={st.get('genimg_today')}")

            # b) dnd 开：回执 + 配置落盘 + 桥热重载日志
            ms = await console_call(
                {"type": "qq_set_modes", "dnd": True, "proactive": True},
                "qq_modes_set")
            cfg_now = json.loads(PRESETS.read_text(encoding="utf-8")).get("qq", {})
            hot = await wait_log(BRIDGE_LOG, t18_pos, "配置热重载",
                                 timeout=15, label="配置热重载")
            report("T18 dnd 开关回执+落盘+热重载",
                   ms.get("dnd") is True and cfg_now.get("qq_dnd") is True and hot)

            # c) dnd 吞主动说话、不吞@：开 3s 旧定时器重启桥（dnd 已落盘带过去），
            #    群消息攒热度零投递；@照回；关 dnd 热重载后攒的热度投出来
            obj3 = json.loads(PRESETS.read_text(encoding="utf-8"))
            obj3["qq"]["napcat_proactive_min"] = 0.05     # ≈3s tick
            obj3["qq"]["napcat_proactive_jitter_min"] = 0
            PRESETS.write_text(json.dumps(obj3, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            t18c_pos = BRIDGE_LOG.stat().st_size
            bridge.terminate()
            await asyncio.sleep(3)
            await nap.close()
            bridge = subprocess.Popen(
                [str(PY), "bridge.py"], cwd=str(HERE), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            nap = FakeNapCat()
            await nap.connect(retries=30)
            ok = await wait_log(BRIDGE_LOG, t18c_pos, "chat_enabled：分身上线",
                                timeout=240, label="T18 chat_enabled")
            if not ok:
                report("T18 桥重启上线（dnd 态）", False)
            else:
                await nap.send_group(GID, MEMBER_A, "阿茶", "大家都在呢", at=False)
                await asyncio.sleep(9)          # ≥2 个 tick：dnd 下一粒都不该投
                delivered = "主动巡查投递" in tail_text(BRIDGE_LOG, t18c_pos)
                report("T18 dnd 吞主动巡查投递", not delivered)
                gsince = len(nap.group_actions)
                await nap.send_group(GID, MEMBER_A, "阿茶", "你还在吗？吱一声",
                                     at=True)
                rep = await nap.wait_group_reply(GID, gsince, timeout=300, quiet=8.0)
                report("T18 dnd 下@照回", bool(rep))
                # 关 dnd → 热重载 → dnd 期间攒的 group_new 该投出来
                t18d_pos = BRIDGE_LOG.stat().st_size
                ms = await console_call(
                    {"type": "qq_set_modes", "dnd": False, "proactive": True},
                    "qq_modes_set")
                freed = await wait_log(BRIDGE_LOG, t18d_pos, "主动巡查投递",
                                       timeout=30, label="dnd 解除后投递")
                report("T18 关 dnd 主动说话恢复",
                       ms.get("dnd") is False and freed)
                await asyncio.sleep(10)         # 她开口/[静默]收尾，波次收干净

            # d) archive 注入：回执帧 + 桥日志 + 她在 QQ 回话（发给连接者 ME）
            since = len(nap.actions)
            ar = await console_call({"type": "qq_archive_now"}, "qq_archive_result")
            injected = await wait_log(BRIDGE_LOG, t18c_pos, "控制中心注入",
                                      timeout=15, label="控制中心注入")
            rep = await nap.wait_reply(ME, since, timeout=300, quiet=8.0)
            report("T18 现在整理记忆注入+她回执",
                   ar.get("error") is None and injected and bool(rep),
                   f"回执={len(rep)} 条")

            # e) set_model 端点：main 在直连预设拒绝（默认 DeepSeek 预设；
            #    --preset-id 传 LiteLLM 预设时切换成功也算端点正确）；
            #    genimg 写配置热生效
            # archive 注入后她可能还在写文件（outstanding>0 会拒切模型）——
            # "正在回应"时等她收尾重试
            r = None
            for _ in range(4):
                r = await console_call(
                    {"type": "qq_set_model", "target": "main", "model": "smoke-x"},
                    "qq_model_set")
                if not (r.get("error") and "正在回应" in r["error"]):
                    break
                await asyncio.sleep(15)
            main_ok = (bool(r.get("error")) and "LiteLLM" in r["error"]) \
                or r.get("error") is None
            report("T18 qq_set_model main 端点", main_ok,
                   r.get("error") or "切换成功（LiteLLM 预设）")
            r = await console_call(
                {"type": "qq_set_model", "target": "genimg", "model": "smoke-gen-x"},
                "qq_model_set")
            gm = (json.loads(PRESETS.read_text(encoding="utf-8"))
                  .get("genimg") or {}).get("model")
            report("T18 qq_set_model genimg 写配置",
                   r.get("error") is None and gm == "smoke-gen-x")

            await console.close()

    finally:
        if fv:
            fv.stop()
        for p in (bridge, server):
            if p and p.poll() is None:
                p.terminate()
        await asyncio.sleep(1)
        PRESETS.write_bytes(presets_bak)
        if ckpt_bak is not None:
            CHECKPOINTS.write_bytes(ckpt_bak)
        # 群 log fixture 还原（冒烟前不存在就删掉，存在就截回原样）
        glog = GROUP_LOG_DIR / f"{GID}.log"
        if glog.exists():
            if glog_pre:
                glog.write_bytes(glog_pre)
            else:
                glog.unlink()
        # T15 产出清理：描述缓存还原、图片缓存/fixture 删掉
        if vcache_bak is not None:
            VCACHE.write_bytes(vcache_bak)
        elif VCACHE.exists():
            VCACHE.unlink()
        # T17 名片 fixture 还原
        if mingpian_bak is not None:
            MINGPIAN.write_bytes(mingpian_bak)
        elif MINGPIAN.exists():
            MINGPIAN.unlink()
        # T18 archive 注入可能动过的活文件还原
        if pub_mem_bak is not None:
            PUB_MEM.write_bytes(pub_mem_bak)
        if pub_emo_bak is not None:
            PUB_EMO.write_bytes(pub_emo_bak)
        for name, b in prof_baks.items():
            (PROF_DIR / name).write_bytes(b)
        if IMG_CACHE_DIR.exists():
            for f in IMG_CACHE_DIR.glob("smokecat_*"):
                f.unlink(missing_ok=True)
        if FIXDIR.exists():
            import shutil as _shutil
            _shutil.rmtree(FIXDIR, ignore_errors=True)
        print("配置已还原")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {passed}/{len(results)} PASS =====")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())

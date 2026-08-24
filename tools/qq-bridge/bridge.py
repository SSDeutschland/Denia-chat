# -*- coding: utf-8 -*-
"""QQ 桥（最小私聊验证）：QQ 通道 ⇄ GUI server_sdk 的 mode="qq" 会话。

通道二选一（presets.json qq.channel）：
  napcat   —— NapCat OneBot v11 反向 WS（onebot_transport.py，小号风控自担）
  official —— QQ 官方 bot API v2（official_transport.py，零封号风险，
              被动回复 60min/4条窗口 + 主动消息须申请权限）

定位：纯传声筒。人格引擎在 server_sdk 的 CC 常驻会话（/denia-qq skill），
本进程只做三件事——
  1. OneBot 反向 WS 服务端：收 NapCat 私聊事件（白名单过滤 + CQ 码占位）
  2. server_sdk WS 客户端（?client=qq）：qq_init → 等 chat_enabled 置 ready
     → chat 帧投递 / 流式段回收
  3. 防抖合并：同一 QQ 号 debounce_sec 内的连续消息合成一轮（否则每条都走
     完整三层编排，又慢又贵）

回复映射：text_delta 按 seg 累积 → segment_end 剥标记拆条 → send_private_msg
逐条发（条间 reply_gap_ms，拟打字节奏+防腾讯频控）。

配置：GUI/presets.json 的 "qq" 节（enabled=False 时拒启动）。
"""
import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import websockets

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from onebot_transport import OneBotServer          # noqa: E402
from strip_markers import strip_markers, split_messages  # noqa: E402
from poisson import PoissonClock, ConversationTracker  # noqa: E402
from vision_gate import VisionGate                # noqa: E402

ROOT = HERE.parent.parent                          # 仓库根
PRESETS_FILE = ROOT / "GUI" / "presets.json"
GROUP_LOG_DIR = ROOT / "denia" / "缓冲" / "群聊记录"

# 与 GUI/server_sdk.py 的 QQ_DEFAULTS 保持一致（桥独立读文件，不 import server）
DEFAULTS = {
    "enabled": False, "preset_id": "", "self_id": 0, "allow_from": [],
    "debounce_sec": 6, "napcat_ws_host": "127.0.0.1", "napcat_ws_port": 8790,
    "napcat_token": "", "server_ws": "ws://127.0.0.1:8766/ws?client=qq",
    "reply_gap_ms": 800, "max_reply_chars": 500,
    "channel": "napcat",            # napcat | official（官方 bot，零封号风险）
    "napcat_groups": [],            # napcat 通道群白名单（群号，空=不接群）
    "napcat_proactive_min": 0,      # 主动说话间隔分钟（0=关闭；napcat 通道限定）
    "napcat_proactive_jitter_min": 30,  # 主动说话随机抖动上限（拟人不规律）
    "napcat_quiet_hours": [0, 8],   # 夜间静默时段（本地小时，前闭后开）
    "napcat_backfill_count": 50,    # NapCat 连入时断档补采条数（0=关）
    "napcat_backfill_delay_sec": 30,  # 连入后等 QQ 客户端同步离线消息再拉历史
    # —— 泊松插话（poisson.py，B 参数 2026-08-18 实验室拍板；base<=0 回落旧定时器）——
    "poisson_base_per_hour": 0.3,   # λ 基数（次/小时）：无热度时的底噪
    "poisson_alpha": 4.0,           # 热度系数：λ = base × (1 + α·heat)
    "poisson_halflife_min": 45,     # 热度 EMA 半衰期（分钟）
    "poisson_cooldown_min": 25,     # 中签后冷却（分钟）
    "poisson_daily_cap": 6,         # 每日中签上限（自然日重置）
    "poisson_min_heat": 1.0,        # 最低热度：单条孤消息不足以叫她开口
    # —— 会话态（她说话了=进入盯手机模式，窗口内群消息免@直投）——
    "engage_window_min": 8,         # 群里无人说话超过 N 分钟 = 超时退出
    "engage_silent_quit": 3,        # 连续 N 次[静默] = 失趣退出
    "engage_max_min": 45,           # 会话硬顶（防热聊群钉死她一整晚）
    # —— 表情包（她写 [表情:文件名] → 目录映射图片发 image 段；napcat 通道限定）——
    "sticker_dir": "denia/共享/工具/表情包",  # 与 GUI 主聊天同一表情库（_索引.md）
    "sticker_max_per_reply": 2,     # 单段最多发几个表情（防刷屏）
    # —— 四通道识图（vision_gate.py；napcat 限定。模型/压缩在 server qq_vision）——
    "vision_qq_enabled": True,      # False=回到"看不到图"旧占位
    "vision_small_kb": 60,          # 小图（表情）判定：小于 N KB
    "vision_small_px": 400,         # 或长边 ≤ N px
    "vision_glance_sync_sec": 5,    # 触发她的消息同步等略读上限（秒）
    "vision_glance_per_group_min": 6,   # 每群每分钟略读上限（轰炸闸）
    "vision_glance_daily_cap": 300,     # 每日略读上限（免费档也有 529）
    "vision_look_daily_cap": 30,        # 每日细看上限（好模型要钱）
    "vision_cooldown_min": 5,       # API 429/529 后无视通道冷却（分钟）
    "vision_slots": 5,              # 每会话图槽数（[看图:N] 回溯）
    "vision_slot_ttl_min": 10,      # 图槽过期（分钟）
    "vision_cache_max": 500,        # 表情包描述缓存 LRU 条数
    # —— 生图（她写 [生图:]/[改图:]/[打卡:] → gen.py 子进程 → image 段发图）——
    "genimg_allow_from": [],        # 生图权限白名单（QQ号；空=谁都不许按快门）
    "genimg_script": "tools/生图/gen.py",  # 生图脚本（相对仓库根；冒烟换桩）
    "genimg_max_per_reply": 1,      # 单段最多生成几张（防连拍刷屏+烧钱）
    # —— 语音（她写 [语音:正文] → server qq_voice 帧 TTS → record 段发语音）——
    "voice_allow_from": [],         # 语音权限白名单（QQ号；空=谁都不许她开口）
    "voice_daily_cap": 50,          # 每日语音上限（本地 TTS 不计费，宽松）
    "voice_max_chars": 200,         # 单条语音正文上限（长了合成慢）
    # —— 甩链接（想法池"来自今天的网络"条目带 🔗 来源 → 她自然甩出来）——
    "link_allow_from": [],          # 甩链接白名单（QQ号；空=不注入链接素材）
    "link_daily_cap": 2,            # 每日带链上限（防营销号感；自然日重置）
    "link_poisson_enabled": False,  # True=泊松插话也可能甩链接（Beta 期先关）
    "official_appid": "", "official_secret": "",
    "official_sandbox": False,      # 沙箱环境（2026-01 起沙箱无群聊，C2C 可测）
    "official_allow_from": [],      # 官方通道白名单是 openid 字符串（≠QQ 号）
    "official_allow_groups": [],    # 官方通道群白名单（group_openid，空=所有群）
    "official_proactive": False,    # 主动消息权限批下来才拨 True
    "official_api_base": "",        # 测试覆盖用（假官方服务器），留空走真实地址
    "official_token_url": "",
    # —— 群聊眼睛（eyes/collector.py 采集，桥只读 log 注入上下文）——
    "eyes_enabled": False,          # 采集器启动闸（桥不消费，仅保持三处键一致）
    "eyes_db_path": "",             # nt_msg.db 全路径
    "eyes_db_key": "",              # x_key_scanner 取的密钥
    "eyes_groups": [],              # 采集群白名单（空=不采）
    "eyes_interval_min": 20,
    "eyes_group_map": {},           # official group_openid → 群号（@时找 log）
    "group_context_lines": 20,      # 群@时注入的最近群聊行数（0=不注入）
    # —— 控制中心行为开关（/qq 页写配置+推 qq_reload_cfg，桥热重载活读）——
    "qq_proactive_enabled": True,   # False=泊松/定时主动说话整体停摆
    "qq_dnd": False,                # True=免打扰：群里只应@，私聊照常
    "qq_web_enabled": False,        # 分身上网开关（server 裁决表消费；桥仅保持键一致）
}

# CQ 码 → 给她的占位提示（denia-qq skill 教她怎么接这些话）
CQ_PLACEHOLDERS = {
    "image": "（对方发来一张图，想看就说）",
    "face": "（对方发了个表情）",
    "record": "（对方发了条语音，这边听不了）",
    "video": "（对方发了个视频，这边看不了）",
}

# 她写 [表情:文件名] → 表情包目录里对应文件映射成 image 段发出去
# （与 GUI 主聊天同一库同一约定：文件名从 _索引.md 逐字复制，可带扩展名）
STICKER_RE = re.compile(r"[\[【]表情[:：]\s*([^\]】/\\:：]{1,30}?)\s*[\]】]")
STICKER_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# 她写 [生图:画面]/[改图:调整]/[打卡:互动] → gen.py 子进程 → image 段发图
# （剥除已在 strip_markers.QQ_MARK_RE 登记；这里负责检出+执行+回执）
GEN_RE = re.compile(r"[\[【](生图|改图|打卡)[:：]\s*([^\]】]{1,300}?)\s*[\]】]")

# 她写 [去群里:群号]正文（连接者私聊唤起的群主动对话）→ 正文发到该群并进会话态。
# 群号须在白名单；marker 之后直到段尾都是要发的话。只从私聊侧检出（防嵌套递归）。
GO_GROUP_RE = re.compile(r"[\[【]去群里[:：]\s*(\d{5,})\s*[\]】]\s*([\s\S]*)")

# 她写 [语音:正文] → server qq_voice 帧合成 → record 段发语音（剥除已在
# strip_markers.QQ_MARK_RE 登记；这里负责检出+权限闸+发送）
VOICE_RE = re.compile(r"[\[【]语音[:：]\s*([^\]】]{1,300}?)\s*[\]】]")

# 出站文本 URL 校验：只放行想法池 🔗 来源行里真实存在的链接（防模型编 URL）
URL_RE = re.compile(r"https?://[^\s，。！？；、）\]】\"']+")
POOL_FILE = ROOT / "denia" / "私有" / "状态" / "思维状态.md"
POOL_SRC_RE = re.compile(r"^\s*🔗\s*来源[:：]\s*(\S+)\s*$")


def load_cfg():
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        raw = data.get("qq") if isinstance(data, dict) else None
    except Exception as e:
        raise SystemExit(f"读 {PRESETS_FILE} 失败：{e}")
    cfg = dict(DEFAULTS)
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    return cfg


def extract_text(message):
    """OneBot 消息链 → 纯文本（CQ 码换占位）。返回空串 = 没可说的地方。"""
    if isinstance(message, str):                    # 字符串格式（含 CQ 码）
        import re
        def _sub(m):
            return CQ_PLACEHOLDERS.get(m.group(1),
                                       "（对方发了一条这边看不懂的消息）")
        return re.sub(r"\[CQ:(\w+)[^\]]*\]", _sub, message).strip()
    parts = []
    for seg in message or []:                       # array 格式
        if not isinstance(seg, dict):
            continue
        st = seg.get("type")
        if st == "text":
            parts.append((seg.get("data") or {}).get("text") or "")
        else:
            parts.append(CQ_PLACEHOLDERS.get(
                st, "（对方发了一条这边看不懂的消息）"))
    return "".join(parts).strip()


def extract_group_text(message, self_id):
    """群消息链 → (纯文本, at我了吗)。at 段渲染成 @QQ号（房间上下文里要知道谁喊谁）。"""
    at_me = False
    parts = []
    for seg in message if isinstance(message, list) else []:
        if not isinstance(seg, dict):
            continue
        st = seg.get("type")
        data = seg.get("data") or {}
        if st == "text":
            parts.append(data.get("text") or "")
        elif st == "at":
            qq = str(data.get("qq") or "")
            if self_id and qq == str(self_id):
                at_me = True
            else:
                parts.append(f"@{qq}")
        else:
            parts.append(CQ_PLACEHOLDERS.get(st, ""))
    text = "".join(parts).strip()
    # 兜底：成员列表未同步时 at 会退化成纯文本 "@QQ号 …"（2026-08-17 真机实测，
    # 小号刚进群那会儿大号客户端解析不出名字，发出来的就是纯文本，at 段丢失）
    if not at_me and self_id and text.startswith(f"@{self_id}"):
        at_me = True
        text = text[len(f"@{self_id}") :].strip()
    return text, at_me


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logging.getLogger("qq-bridge")
        self.channel = str(cfg.get("channel") or "napcat")
        if self.channel == "official":
            from official_transport import OfficialTransport
            self.transport = OfficialTransport(
                appid=cfg["official_appid"], secret=cfg["official_secret"],
                sandbox=cfg.get("official_sandbox"),
                proactive=cfg.get("official_proactive"),
                on_event=self.on_onebot_event,
                api_base=cfg.get("official_api_base") or "",
                token_url=cfg.get("official_token_url") or "")
            allow = cfg.get("official_allow_from") or []
            self.allow_groups = {str(g) for g in
                                 cfg.get("official_allow_groups") or []}
            if not self.allow_groups:
                self.log.warning("群白名单为空：任何群 @她都会回应"
                                 "（首个群的 group_openid 见日志，建议抄回配置）")
        else:
            self.channel = "napcat"
            self.transport = OneBotServer(
                cfg["napcat_ws_host"], cfg["napcat_ws_port"],
                token=cfg["napcat_token"], self_id=cfg["self_id"],
                on_event=self.on_onebot_event)
            self.transport.on_connect = self._on_napcat_connect
            allow = cfg.get("allow_from") or []
            self.allow_groups = {str(g) for g in
                                 cfg.get("napcat_groups") or []}
        # uid 类型按通道不同（napcat=QQ号 int / official=openid str），统一 str 比对
        self.allow_from = {str(u) for u in allow}
        if not self.allow_from:
            self.log.warning("白名单为空：所有私聊都会触发分身（建议配置）")
        self.ws = None              # server_sdk 连接（None=未连上）
        self.ready = False          # chat_enabled(mode=qq) 收到 = 分身脑子在线
        self.unlocked = True        # input_unlock 门闩：永不并发投递
        self.turn_user = None       # 本轮回复发给谁
        self.seg_text = {}          # seg -> 累积文本
        self.buffers = {}           # user_id -> [待合并文本]
        self.debounce_tasks = {}    # user_id -> asyncio.Task
        self.pending = []           # [(user_id, text)] ready/解锁后补投
        self.group_new = {}         # 群号 -> 上次主动巡查以来的新消息数
        self._backfill_task = None  # 断档补采任务（重连时取消旧的）
        self.poisson = {}           # 群号 -> PoissonClock（_poisson_loop 启动时建）
        self.engage = {}            # 群号 -> ConversationTracker（会话态跟踪）
        self.turn_tags = {}         # buf_uid -> 投递前缀（【有人@你】/【会话继续】）
        self.turn_sent = False      # 本轮是否已向群里发过言（静默/发言记账用）
        self._sticker_b64 = {}      # 表情文件路径 -> (mtime, base64) 缓存
        self._vision_seq = 0        # qq_vision 请求序号
        self._vision_pending = {}   # req_id -> asyncio.Future
        self._voice_seq = 0         # qq_voice 请求序号
        self._voice_pending = {}    # req_id -> asyncio.Future
        self._pool_cache = None     # (mtime, [(条目, url)]) 想法池链接素材缓存
        self._day = None            # 每日计数所属自然日（toordinal）
        self._voice_used = 0        # 今日已发语音条数
        self._link_used = 0         # 今日已注入链接素材次数
        self._requester = {}        # buf_uid -> 最后触发她的发送者（生图权限判定）
        self._gen_lock = asyncio.Lock()  # 生图串行（一次洗一张）
        self.gate = VisionGate(cfg, self.log, ROOT, self._vision_call,
                               self._log_vision_note)
        self.gate._get_image = lambda f: self.transport.call(
            "get_image", {"file": f}, timeout=15)

    # ---------- 四通道识图（vision_gate）----------

    async def _vision_call(self, op, path, context="", question=""):
        """桥 → server qq_vision 请求/响应（req_id 配对，60s 超时）。"""
        if self.ws is None:
            raise RuntimeError("server 未连接")
        self._vision_seq += 1
        rid = self._vision_seq
        fut = asyncio.get_event_loop().create_future()
        self._vision_pending[rid] = fut
        try:
            await self.ws.send(json.dumps(
                {"type": "qq_vision", "req_id": rid, "mode": op,
                 "img_path": path, "context": context, "question": question},
                ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout=60)
        finally:
            self._vision_pending.pop(rid, None)

    async def _voice_call(self, text):
        """桥 → server qq_voice 请求/响应（req_id 配对，120s 超时——冷启慢）。"""
        if self.ws is None:
            raise RuntimeError("server 未连接")
        self._voice_seq += 1
        rid = self._voice_seq
        fut = asyncio.get_event_loop().create_future()
        self._voice_pending[rid] = fut
        try:
            await self.ws.send(json.dumps(
                {"type": "qq_voice", "req_id": rid, "mode": "qq",
                 "text": text}, ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout=120)
        finally:
            self._voice_pending.pop(rid, None)

    # ---------- 甩链接（想法池 🔗 条目 → 素材注入 + 出站 URL 校验）----------

    def _roll_day(self):
        """自然日翻转 → 语音/链接计数清零（桥内存计数，重启即重置，同泊松日帽）。"""
        today = datetime.now().toordinal()
        if self._day != today:
            self._day = today
            self._voice_used = 0
            self._link_used = 0

    def _pool_entries(self):
        """想法池里带 🔗 来源 的条目 → [(评论, url)]，新的在前。
        文件 mtime 缓存：爬虫每天才写几次，不必每轮都重读。"""
        try:
            mtime = POOL_FILE.stat().st_mtime
        except OSError:
            return []
        if self._pool_cache and self._pool_cache[0] == mtime:
            return self._pool_cache[1]
        entries = []
        try:
            lines = POOL_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        last_bullet = None
        for ln in lines:
            s = ln.strip()
            if s.startswith("- "):
                last_bullet = s[2:].strip()
                continue
            m = POOL_SRC_RE.match(ln)
            if m and last_bullet:
                entries.append((last_bullet, m.group(1)))
                last_bullet = None
        entries.reverse()                       # 文件尾部一般是新日期，新的优先
        self._pool_cache = (mtime, entries)
        return entries

    def _link_requester_ok(self, uid):
        """甩链接权限闸（同生图语义）：私聊=对方本人；群=最后递消息的人。"""
        allow = {str(x) for x in (self.cfg.get("link_allow_from") or [])}
        if not allow:
            return False
        req = self._requester.get(uid) if str(uid).startswith("g:") else uid
        return req is not None and str(req) in allow

    def _link_hint(self):
        """挑一条带链接的池条目，包成"她突然想起"的注入文案；无素材/超帽→空。"""
        self._roll_day()
        if self._link_used >= int(self.cfg.get("link_daily_cap") or 0):
            return ""
        entries = self._pool_entries()
        if not entries:
            return ""
        import random
        comment, url = random.choice(entries[:6])
        self._link_used += 1
        return ("\n（你冲浪时存的这条突然冒出来了：「%s」 链接：%s ——"
                "想分享就自然地带出来，链接逐字抄不许改；不想分享就当我没提）"
                % (comment[:150], url))

    def _scrub_urls(self, text):
        """出站 URL 白名单校验：想法池里没有的链接剥掉（模型编 URL 的防呆）。"""
        urls = URL_RE.findall(text or "")
        if not urls:
            return text
        allowed = {u for _, u in self._pool_entries()}
        for u in urls:
            if u not in allowed:
                self.log.warning("剥掉池外链接（防编造）：%s", u)
                text = text.replace(u, "")
        return text

    # ---------- 语音（[语音:正文] → server TTS → record 段）----------

    def _voice_requester_ok(self, uid):
        """语音权限闸（同生图语义）：私聊=对方本人；群=最后递消息的人。"""
        allow = {str(x) for x in (self.cfg.get("voice_allow_from") or [])}
        if not allow:
            return False
        req = self._requester.get(uid) if str(uid).startswith("g:") else uid
        return req is not None and str(req) in allow

    async def _run_voice_reqs(self, uid, texts):
        """语音任务：权限/日帽在 _reply 已过；这里合成+发 record+失败回执。
        成功不回执——对方已经"听到"了，她不需要再解说一遍。"""
        if self.channel != "napcat" or not hasattr(self.transport,
                                                   "send_record_msg"):
            return
        text = texts[0][:int(self.cfg.get("voice_max_chars") or 200)]
        if len(texts) > 1:
            self.log.info("单段多条[语音]，只合成第一条（共 %d 条）", len(texts))
        try:
            res = await self._voice_call(text)
        except Exception as e:
            self.log.warning("语音请求失败：%s", e)
            res = {"ok": False, "error": str(e)}
        if not res.get("ok"):
            self.log.info("语音合成失败：%s", res.get("error"))
            self.pending.append((uid, "（你想把那句话用声音送出去，"
                                      "但声音卡在了半路上——信号好像不太稳）"))
            await self._flush()
            return
        try:
            await self.transport.send_record_msg(uid, res["b64"])
            self.log.info("→ QQ %s：[语音:%s]", uid, text[:30])
        except Exception as e:
            self.log.error("语音发送失败（%s）：%s", uid, e)
            self.pending.append((uid, "（语音录好了但没发出去，信号不太好）"))
            await self._flush()

    def _log_vision_note(self, gid, text):
        """异步略读完成 → (图注) 行追加群聊记录（她在上下文里自然看到）。"""
        line = "[%s] %s" % (datetime.now().strftime("%m-%d %H:%M"), text)
        try:
            GROUP_LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(GROUP_LOG_DIR / f"{gid}.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            self.log.warning("图注写入失败（群%s）：%s", gid, e)

    # ---------- 通道侧 ----------

    async def on_onebot_event(self, ev):
        mt = ev.get("message_type")
        if ev.get("post_type") != "message" or mt not in ("private", "group"):
            return
        if mt == "group" and self.channel == "napcat":
            return await self._on_napcat_group(ev)   # napcat 群：feed+@检测
        if mt == "group" and self.channel != "official":
            return
        uid = ev.get("user_id")
        if uid is None:
            return
        if str(uid) == str(self.cfg.get("self_id") or ""):
            return                                   # 自己发的，防回环
        if str(uid).startswith("g:"):                # 群@：按 group_openid 白名单
            if self.allow_groups and str(uid)[2:] not in self.allow_groups:
                self.log.info("非白名单群忽略：%s", uid)
                return
        elif self.allow_from and str(uid) not in self.allow_from:
            self.log.info("非白名单私聊忽略：%s", uid)
            return
        message, imgs = (self.gate.split_images(ev.get("message"))
                         if self.channel == "napcat" else (ev.get("message"), []))
        text = extract_text(message)
        if imgs:                                    # 私聊必然触发她：同步略读
            _, ph = await self.gate.admit(uid, imgs, triggering=True)
            text = (text + ph).strip()
        if not text:
            return
        self.log.info("← QQ %s：%r", uid, text[:60])
        self.buffers.setdefault(uid, []).append(text)
        old = self.debounce_tasks.get(uid)
        if old and not old.done():
            old.cancel()
        self.debounce_tasks[uid] = asyncio.create_task(self._debounce(uid))

    async def _debounce(self, uid):
        try:
            await asyncio.sleep(float(self.cfg.get("debounce_sec", 6)))
        except asyncio.CancelledError:
            return                                   # 窗口内来了新消息，重新计时
        texts = self.buffers.pop(uid, [])
        if not texts:
            return
        merged = "\n".join(texts)
        merged = await self.gate.resolve(merged)     # ⟦Vn⟧ → 略读结果
        if str(uid).startswith("g:"):                # 群：注入眼睛看到的房间上下文
            ctx = self._group_context(str(uid)[2:])
            tag = self.turn_tags.pop(uid, "【有人@你】")
            merged = (ctx or "") + tag + merged
            if self._link_requester_ok(uid):         # 群里他在场搭话时也给素材
                merged += self._link_hint()
        elif str(uid) in {str(x) for x in
                          (self.cfg.get("link_allow_from") or [])}:
            merged += self._link_hint()              # 私聊白名单：甩链接素材
        self.log.info("防抖合并 %d 条 → 投递队列：%r", len(texts), merged[:60])
        self.pending.append((uid, merged))
        await self._flush()

    def _group_context(self, group_ref):
        """群 → 眼睛 log 尾部 N 行（注入前缀）；没配置/没记录返回空。
        official 通道经 eyes_group_map 把 group_openid 译成群号；
        napcat 通道群号本身就是数字，直接命中同名 log 文件。"""
        n = int(self.cfg.get("group_context_lines") or 0)
        if n <= 0:
            return ""
        code = str((self.cfg.get("eyes_group_map") or {}).get(group_ref) or "")
        if not code and str(group_ref).isdigit():
            code = str(group_ref)                    # napcat：群号即文件名
        if not code:
            return ""
        try:
            lines = (GROUP_LOG_DIR / f"{code}.log") \
                .read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        tail = [l for l in lines if l.strip()][-n:]
        if not tail:
            return ""
        return "(群里刚才在聊:\n" + "\n".join(tail) + "\n)\n"

    async def _flush(self):
        """ready + unlocked 才投递；一次只投一条（server 一次一问）。"""
        while self.pending and self.ready and self.unlocked and self.ws:
            uid, text = self.pending.pop(0)
            self.turn_user = uid
            self.turn_sent = False
            self.unlocked = False
            self.seg_text.clear()
            self.log.info("→ server_sdk（qq）：%r", text[:60])
            try:
                await self.ws.send(json.dumps(
                    {"type": "chat", "mode": "qq", "text": text},
                    ensure_ascii=False))
            except Exception as e:
                self.log.error("chat 帧发送失败：%s（消息回队列）", e)
                self.pending.insert(0, (uid, text))
                self.unlocked = True
                return

    # ---------- napcat 群（实时眼睛 + @触发 + 主动说话）----------

    async def _on_napcat_group(self, ev):
        """白名单群消息：全场落 群聊记录/<群号>.log；@我 才走回复流程。"""
        gid = str(ev.get("group_id") or "")
        if gid not in self.allow_groups:
            return
        uid = ev.get("user_id")
        if uid is None or str(uid) == str(self.cfg.get("self_id") or ""):
            return                                   # 自己说的，防回环
        sender = ev.get("sender") or {}
        name = sender.get("card") or sender.get("nickname") or str(uid)
        message, imgs = self.gate.split_images(ev.get("message"))
        text, at_me = extract_group_text(message,
                                         self.cfg.get("self_id"))
        if not text and not imgs:
            return
        trk = self.engage.get(gid)
        buf_uid = f"g:{gid}"
        # 投递文案带 ⟦Vn⟧ token（debounce 结算换略读结果）；log 行用即时占位
        log_ph, del_ph = "", ""
        if imgs:
            engaged_now = bool(trk and trk.engaged)
            log_ph, del_ph = await self.gate.admit(
                buf_uid, imgs, triggering=(at_me or engaged_now), gid=gid)
        self._append_group_log(gid, name, uid, text + log_ph)
        text = (text + del_ph).strip()
        if not text:
            return
        self.group_new[gid] = self.group_new.get(gid, 0) + 1
        t = time.time() / 60.0
        clk = self.poisson.get(gid)
        if clk is not None:
            clk.on_message(t)                        # 热度记账（自己的不算）
        if not at_me and trk is not None and trk.on_message(t):
            # 会话中：她在盯手机，群友发言不用@也递给她（她可[静默]失趣退出）。
            # 免打扰（qq_dnd）下不递——群里只应@，私聊不受影响
            if self.cfg.get("qq_dnd"):
                return
            self.log.info("← QQ 群%s %s（会话中免@）：%r", gid, name, text[:60])
            self.turn_tags[buf_uid] = "【会话继续】"
            self._requester[buf_uid] = uid
            self.buffers.setdefault(buf_uid, []).append(f"（群聊·{name}）{text}")
            old = self.debounce_tasks.get(buf_uid)
            if old and not old.done():
                old.cancel()
            self.debounce_tasks[buf_uid] = asyncio.create_task(
                self._debounce(buf_uid))
            return
        if not at_me:
            return
        self.log.info("← QQ 群%s %s@我：%r", gid, name, text[:60])
        self.turn_tags[buf_uid] = "【有人@你】"
        self._requester[buf_uid] = uid
        self.buffers.setdefault(buf_uid, []).append(f"（群聊·{name}）{text}")
        old = self.debounce_tasks.get(buf_uid)
        if old and not old.done():
            old.cancel()
        self.debounce_tasks[buf_uid] = asyncio.create_task(
            self._debounce(buf_uid))

    def _append_group_log(self, gid, name, uid, text):
        """与 eyes/collector.py 同一行格式——实时眼睛和DB眼睛产出可混读。"""
        line = "[%s] %s(%s): %s" % (
            datetime.now().strftime("%m-%d %H:%M"), name, uid,
            text.replace("\r", " ").replace("\n", "⏎"))
        try:
            GROUP_LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(GROUP_LOG_DIR / f"{gid}.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            self.log.warning("群聊记录写入失败（群%s）：%s", gid, e)

    # ---------- 断档补采（NapCat 连入时 get_group_msg_history 回填 log）----------

    def _on_napcat_connect(self):
        """NapCat（重）连入 → 延时补采断档期群消息。napcat 通道限定。"""
        if int(self.cfg.get("napcat_backfill_count") or 0) <= 0 \
                or not self.allow_groups:
            return
        old = self._backfill_task
        if old and not old.done():
            old.cancel()
        self._backfill_task = asyncio.create_task(self._backfill_groups())

    async def _backfill_groups(self):
        """逐白名单群拉历史回填；补到的 @我 没人应过 → 投递【补看群里】。"""
        delay = float(self.cfg.get("napcat_backfill_delay_sec") or 0)
        if delay > 0:                        # 等 QQ 客户端把离线消息同步下来
            await asyncio.sleep(delay)
        count = int(self.cfg.get("napcat_backfill_count") or 0)
        for gid in sorted(self.allow_groups):
            try:
                data = await self.transport.call(
                    "get_group_msg_history",
                    {"group_id": int(gid), "message_seq": 0, "count": count},
                    timeout=15)
            except Exception as e:
                self.log.warning("断档补采失败（群%s）：%s", gid, e)
                continue
            msgs = (data or {}).get("messages") or []
            added, missed = self._append_backfill(gid, msgs)
            self.log.info("断档补采（群%s）：历史 %d 条，新补 %d 条，错过@ %d 次",
                          gid, len(msgs), added, len(missed))
            if added:
                self.group_new[gid] = self.group_new.get(gid, 0) + added
            if missed:
                quoted = " / ".join(f"{n}「{t[:30]}」" for n, t in missed[:3])
                ctx = self._group_context(gid)
                self.pending.append((f"g:{gid}",
                    f"【补看群里】你刚才不在的这阵子，群里有人@过你没得到回应"
                    f"（{quoted}）。想回应就说两句，不想就只回[静默]两个字。"
                    + ("\n" + ctx if ctx else "")))
                await self._flush()

    def _append_backfill(self, gid, msgs):
        """历史消息回填 log：与 log 尾部按 发送者+文本 去重，只补真缺失的
        （幂等——重连反复触发也不会写重、不会重复提醒）。补采行时间戳取消息
        原始时间。返回 (新补条数, [(名字, 文本)] 错过的@列表)。"""
        self_id = str(self.cfg.get("self_id") or "")
        tail_keys = set()
        try:
            lines = (GROUP_LOG_DIR / f"{gid}.log") \
                .read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for ln in lines[-200:]:
            m = re.search(r"\((\d+)\): (.*)$", ln)
            if m:
                tail_keys.add(f"{m.group(1)}:{m.group(2)}")
        added, missed = 0, []
        for ev in sorted(msgs, key=lambda e: e.get("time") or 0):
            sender = ev.get("sender") or {}
            uid = sender.get("user_id")
            if uid is None or str(uid) == self_id:
                continue                            # 自己说的不落 log（同实时路径）
            text, at_me = extract_group_text(ev.get("message"),
                                             self.cfg.get("self_id"))
            if not text:
                continue
            norm = text.replace("\r", " ").replace("\n", "⏎")
            key = f"{uid}:{norm}"
            if key in tail_keys:
                continue
            name = sender.get("card") or sender.get("nickname") or str(uid)
            line = "[%s] %s(%s): %s" % (
                datetime.fromtimestamp(ev.get("time") or 0)
                      .strftime("%m-%d %H:%M"), name, uid, norm)
            try:
                GROUP_LOG_DIR.mkdir(parents=True, exist_ok=True)
                with open(GROUP_LOG_DIR / f"{gid}.log", "a",
                          encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:
                self.log.warning("群聊记录写入失败（群%s）：%s", gid, e)
                break
            tail_keys.add(key)
            added += 1
            if at_me:
                missed.append((name, norm))
        return added, missed

    async def _proactive_loop(self):
        """主动说话：间隔+抖动到点，群里自上次巡查以来有新动静才开口；
        她可以只回[静默]（剥标记后为空=什么都不发）。napcat 通道限定。"""
        interval = float(self.cfg.get("napcat_proactive_min") or 0)
        if self.channel != "napcat" or interval <= 0 or not self.allow_groups:
            return
        import random
        jitter_max = float(self.cfg.get("napcat_proactive_jitter_min") or 0)
        quiet = self.cfg.get("napcat_quiet_hours") or []
        q0, q1 = (int(quiet[0]), int(quiet[1])) if len(quiet) >= 2 else (0, 0)
        self.log.info("主动说话已开：每 %g 分钟 ±抖动，静默时段 %s",
                      interval, quiet or "无")
        while True:
            await asyncio.sleep((interval + random.uniform(0, jitter_max)) * 60)
            if q0 != q1 and q0 <= datetime.now().hour < q1:
                continue                               # 夜里不吵人
            for gid in sorted(self.allow_groups):
                if self.group_new.get(gid, 0) < 1:
                    continue                           # 房间没动静不硬聊
                if not self.cfg.get("qq_proactive_enabled", True) \
                        or self.cfg.get("qq_dnd"):
                    continue                           # 控制中心关了主动说话/免打扰
                self.group_new[gid] = 0
                ctx = self._group_context(gid)
                self.pending.append((f"g:{gid}",
                    (ctx or "")
                    + "【看看群里】最近没人@你，但群里有点动静。"
                      "想凑热闹就说两句，没什么想说的就只回[静默]两个字。"))
                self.log.info("主动巡查投递：群%s", gid)
            await self._flush()

    async def _poisson_loop(self):
        """泊松插话：每分钟 tick 一次抽签，中了投【看看群里】（她可[静默]）。
        会话中的群跳过 tick（都在聊了还"看看群里"就精分了），并负责
        会话态超时/硬顶检测。base<=0 时回落旧固定间隔定时器。"""
        if self.channel != "napcat" or not self.allow_groups:
            return
        base_h = float(self.cfg.get("poisson_base_per_hour") or 0)
        if base_h <= 0:
            return await self._proactive_loop()      # 泊松关 = 旧定时器
        import random
        quiet = self.cfg.get("napcat_quiet_hours") or []
        for gid in sorted(self.allow_groups):
            self.poisson[gid] = PoissonClock(
                base_per_min=base_h / 60.0,
                alpha=float(self.cfg.get("poisson_alpha") or 0),
                halflife_min=float(self.cfg.get("poisson_halflife_min") or 30),
                cooldown_min=float(self.cfg.get("poisson_cooldown_min") or 0),
                daily_cap=int(self.cfg.get("poisson_daily_cap") or 0),
                quiet_hours=quiet,
                min_heat=float(self.cfg.get("poisson_min_heat") or 0),
                rng=random.Random())
            self.engage[gid] = ConversationTracker(
                window_min=float(self.cfg.get("engage_window_min") or 8),
                silent_quit=int(self.cfg.get("engage_silent_quit") or 3),
                max_engaged_min=float(self.cfg.get("engage_max_min") or 45))
        self.log.info(
            "泊松插话已开：λ基数=%g次/时 α=%g 半衰期=%gm 冷却=%gm 日上限=%d "
            "最低热度=%g 静默时段=%s；会话窗=%gm 失趣=%d次 硬顶=%gm",
            base_h, float(self.cfg.get("poisson_alpha") or 0),
            float(self.cfg.get("poisson_halflife_min") or 30),
            float(self.cfg.get("poisson_cooldown_min") or 0),
            int(self.cfg.get("poisson_daily_cap") or 0),
            float(self.cfg.get("poisson_min_heat") or 0), quiet or "无",
            float(self.cfg.get("engage_window_min") or 8),
            int(self.cfg.get("engage_silent_quit") or 3),
            float(self.cfg.get("engage_max_min") or 45))
        while True:
            await asyncio.sleep(60)
            now = datetime.now()
            t = time.time() / 60.0
            fired = False
            for gid in sorted(self.allow_groups):
                trk = self.engage[gid]
                before = dict(trk.exits)
                trk.check_timeout(t)
                # 退出边沿检测用 exits 计数而非 tick 间快照——会话在两次 tick
                # 之间进入又退出时，快照采样会把整个会话漏掉（冒烟 T13 实锤）
                for r in ("timeout", "cap"):
                    if trk.exits[r] > before[r]:
                        self.log.info("会话态退出（群%s，原因=%s，时长=%.1f分）",
                                      gid, r, trk.conv_lengths[-1])
                if trk.engaged:
                    continue                           # 会话中不"看看群里"
                if not self.cfg.get("qq_proactive_enabled", True) \
                        or self.cfg.get("qq_dnd"):
                    continue                           # 控制中心关了主动说话/免打扰
                clk = self.poisson[gid]
                if clk.tick(t, now.hour, now.toordinal()):
                    ctx = self._group_context(gid)
                    link_hint = (self._link_hint()
                                 if self.cfg.get("link_poisson_enabled") else "")
                    self.pending.append((f"g:{gid}",
                        (ctx or "")
                        + "【看看群里】最近没人@你，但群里有点动静。"
                          "想凑热闹就说两句，没什么想说的就只回[静默]两个字。"
                        + link_hint))
                    self.log.info("泊松中签投递：群%s（heat=%.2f λ=%.3f/时 今日%d/%d）",
                                  gid, clk.heat, clk.lam(t) * 60,
                                  clk.fires_today, clk.daily_cap)
                    fired = True
            if fired:
                await self._flush()

    # ---------- 表情包（[表情:名字] → 目录映射 image 段，napcat 限定）----------

    def _sticker_dir(self):
        d = Path(str(self.cfg.get("sticker_dir") or ""))
        return d if d.is_absolute() else ROOT / d

    def _find_sticker(self, name):
        """名字 → 文件路径。先按完整文件名精确找（_索引.md 逐字复制约定，
        带扩展名如 微笑.jpg），再按词干补扩展名兜底（[表情:微笑] 也能中）。
        名里不含路径分隔（STICKER_RE 已排除），无目录穿越面。"""
        d = self._sticker_dir()
        p = d / name
        if p.is_file() and p.suffix.lower() in STICKER_EXTS:
            return p
        for ext in STICKER_EXTS:
            p = d / f"{name}{ext}"
            if p.is_file():
                return p
        return None

    def _load_sticker_b64(self, path):
        import base64
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        hit = self._sticker_b64.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            return None
        self._sticker_b64[str(path)] = (mtime, b64)
        return b64

    # ---------- server_sdk 侧 ----------

    async def server_loop(self):
        """连 server_sdk（断线自动重连；qq_init 里走 last_qq_session 续接）。"""
        url = self.cfg["server_ws"]
        while True:
            try:
                async with websockets.connect(
                        url, max_size=32 * 1024 * 1024) as ws:
                    self.ws = ws
                    self.ready = False
                    await ws.send(json.dumps({"type": "qq_init"}))
                    self.log.info("已连 server_sdk，qq_init 已发，等 chat_enabled…")
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        if isinstance(data, dict):
                            await self.on_server_frame(data)
            except (websockets.ConnectionClosed, OSError) as e:
                self.log.warning("server_sdk 连接断开：%s（3s 后重连）", e)
            except Exception as e:
                self.log.exception("server_sdk 连接异常：%s（3s 后重连）", e)
            finally:
                self.ws = None
                self.ready = False
                self.unlocked = True                  # 断线回合作废，重投 pending
                self.seg_text.clear()
            await asyncio.sleep(3)

    async def on_server_frame(self, data):
        if data.get("mode") != "qq":
            return                                    # chat/read 会话的帧与桥无关
        t = data.get("type")
        if t == "qq_vision_result":                  # 代看响应：配对 req_id
            fut = self._vision_pending.get(data.get("req_id"))
            if fut is not None and not fut.done():
                fut.set_result(data)
            return
        if t == "qq_voice_result":                   # 语音合成响应：配对 req_id
            fut = self._voice_pending.get(data.get("req_id"))
            if fut is not None and not fut.done():
                fut.set_result(data)
            return
        if t == "qq_reload_cfg":
            # 控制中心改了行为开关：整节重读热生效（投递点都活读 self.cfg）。
            # 派生集合（allow_from/allow_groups）不在热重载范围——那些走重启
            new = load_cfg()
            self.cfg.clear()
            self.cfg.update(new)
            self.gate.cfg = self.cfg
            self.log.info("配置热重载：主动说话=%s 免打扰=%s",
                          "开" if self.cfg.get("qq_proactive_enabled", True) else "关",
                          "开" if self.cfg.get("qq_dnd") else "关")
            return
        if t == "qq_inject":
            # 控制中心注入（现在整理记忆等）：复用 pending/_flush 通道，
            # 回执发给连接者（白名单第一人；空白名单=注入没人收，记日志丢弃）
            text = (data.get("text") or "").strip()
            if not text:
                return
            uid = next(iter(self.allow_from), None)
            if uid is None:
                self.log.warning("qq_inject 丢弃：allow_from 为空，回执没人收")
                return
            self.log.info("控制中心注入：%r", text[:60])
            self.pending.append((uid, text))
            await self._flush()
            return
        if t == "chat_enabled":
            self.ready = True
            self.log.info("chat_enabled：分身上线（preset=%s model=%s）",
                          data.get("preset"), data.get("model"))
            await self._flush()
        elif t in ("need_preset", "preset_error"):
            self.log.error("分身没连上脑子：%s", data)
            await self._notify("唔……这边的信号没接上，等我一下")
        elif t == "text_delta":
            seg = data.get("seg")
            self.seg_text[seg] = self.seg_text.get(seg, "") + data.get("text", "")
        elif t == "segment_end":
            text = self.seg_text.pop(data.get("seg"), "")
            if text.strip() and self.turn_user is not None:
                await self._reply(self.turn_user, text)
        elif t == "segment_final":
            self.log.info("回合收尾（cost=%s）", data.get("cost"))
        elif t == "turn_reverted":
            self.log.info("回合被打断，丢弃未发段")
            self.seg_text.clear()
        elif t == "input_unlock":
            self.unlocked = True
            await self._flush()
        elif t == "busy":
            # 理论上不会发生（input_unlock 门闩挡着），发生就等 unlock
            self.log.warning("server 回 busy（门闩失效？），等 input_unlock")
        elif t == "error":
            self.log.error("server 错误帧：%s（视为回合结束）", data.get("msg"))
            self.unlocked = True
            await self._flush()

    # ---------- 回复 ----------

    async def _reply(self, uid, raw_text):
        import re
        if re.search(r"[\[【]静默[\]】]", raw_text or ""):
            self.log.info("她选择静默（含[静默]标记的段整段不发）")
            self._track_turn_end(uid, spoke=False)
            return
        # 私聊唤起的群主动对话：[去群里:群号]正文 → 以 g:群号 收件人递归走一遍
        # _reply（发送路由/表情/会话态记账全复用）。只从私聊侧检出，递归那次
        # uid=g:… 自然不再匹配，防嵌套死循环。
        if not str(uid).startswith("g:"):
            gm = GO_GROUP_RE.search(raw_text or "")
            if gm:
                gid, gcontent = gm.group(1), (gm.group(2) or "").strip()
                raw_text = (raw_text[:gm.start()] + raw_text[gm.end():]).strip()
                if gid in self.allow_groups and gcontent:
                    self.log.info("私聊唤起主动对话 → QQ 群%s：%r", gid, gcontent[:60])
                    await self._reply(f"g:{gid}", gcontent)
                else:
                    self.log.info("去群里 %s 未执行：群不在白名单或内容为空", gid)
                if not raw_text:
                    return
        # ②看图通道：剥标记前先检出 [看图]/[细看]，异步细看后注入给她
        look_reqs = self.gate.scan_reply_markers(raw_text)
        if look_reqs:
            asyncio.create_task(self._run_look_reqs(uid, look_reqs))
        # 生图通道：[生图]/[改图]/[打卡] → 权限闸 → gen.py 子进程发图
        gen_reqs = GEN_RE.findall(raw_text or "")
        if gen_reqs:
            asyncio.create_task(self._run_gen_reqs(uid, gen_reqs))
        # 语音通道：[语音:正文] → 权限闸+日帽 → server TTS → record 段
        voice_reqs = VOICE_RE.findall(raw_text or "")
        if voice_reqs:
            self._roll_day()
            if not self._voice_requester_ok(uid):
                self.log.info("语音拒绝（%s 的触发者不在 voice_allow_from）", uid)
                self.pending.append((uid, "（你试着把这句话用声音送出去，"
                                          "但这边的通道好像只认连接者）"))
                await self._flush()
            elif self._voice_used >= int(self.cfg.get("voice_daily_cap") or 0):
                self.log.info("语音日帽已达（%d 条），今天不再开口",
                              self._voice_used)
                self.pending.append((uid, "（你今天说了好多话，嗓子有点哑了"
                                          "——语音明天再录吧）"))
                await self._flush()
            else:
                self._voice_used += 1
                asyncio.create_task(self._run_voice_reqs(uid, voice_reqs))
        text = self._scrub_urls(strip_markers(raw_text))
        if not text:
            self.log.info("段剥标记后为空，跳过发送")
            await self._send_stickers(uid, raw_text)  # 纯表情段：图照发
            self._track_turn_end(
                uid, spoke=bool(STICKER_RE.search(raw_text or "")))
            return
        msgs = split_messages(text, int(self.cfg.get("max_reply_chars", 500)))
        gap = float(self.cfg.get("reply_gap_ms", 800)) / 1000.0
        for i, m in enumerate(msgs):
            if i:
                await asyncio.sleep(gap)
            try:
                await self.transport.send_private_msg(uid, m)
                self.log.info("→ QQ %s：%r", uid, m[:60])
            except Exception as e:
                self.log.error("send_private_msg 失败（uid=%s）：%s", uid, e)
                return                                # 通道掉了，剩下的别硬发
        self._track_turn_end(uid, spoke=True)
        await self._send_stickers(uid, raw_text)

    async def _run_look_reqs(self, uid, look_reqs):
        """细看结果注入：作为"她自己的动作回执"投递，她自然接话。"""
        ctx = ""
        if str(uid).startswith("g:"):
            ctx = self._group_context(str(uid)[2:])
        for op, arg in look_reqs:
            try:
                if op == "look":
                    note = await self.gate.handle_look(uid, arg, context=ctx)
                else:
                    note = await self.gate.handle_ask(uid, arg)
            except Exception as e:
                self.log.warning("看图处理失败：%s", e)
                continue
            self.pending.append((uid, note))
            await self._flush()

    # ---------- 生图（[生图:]/[改图:]/[打卡:] → gen.py → image 段）----------

    def _genimg_requester_ok(self, uid):
        """生图权限闸：只有连接者能让她按快门（genimg_allow_from）。
        私聊=requester 即对方本人；群=最后一个把消息递给她的人
        （@/会话继续的发送者，_on_napcat_group 里记账）。"""
        allow = {str(x) for x in (self.cfg.get("genimg_allow_from") or [])}
        if not allow:
            return False
        req = self._requester.get(uid) if str(uid).startswith("g:") else uid
        return req is not None and str(req) in allow

    async def _run_gen_reqs(self, uid, reqs):
        """生图任务：权限闸 → gen.py 子进程 → base64 发图 → 交付回执。
        拒绝/失败也给她世界观内回执——她已经跟对方说了要拍，得能接住话。"""
        if self.channel != "napcat" or not hasattr(self.transport,
                                                   "send_image_msg"):
            return
        if not self._genimg_requester_ok(uid):
            self.log.info("生图拒绝（%s 的触发者不在 genimg_allow_from）", uid)
            self.pending.append((uid, "（你举起拍立得想拍一张，但它没反应"
                                      "——这台相机好像只认连接者）"))
            await self._flush()
            return
        cap = int(self.cfg.get("genimg_max_per_reply") or 1)
        async with self._gen_lock:
            for kind, free in reqs[:cap]:
                note = await self._gen_one(uid, kind, free)
                if note:
                    self.pending.append((uid, note))
                    await self._flush()

    async def _gen_one(self, uid, kind, free):
        """洗一张。成功→发图+交付回执；失败→回执带原因
        （gen.py 的报错本来就是给她看的中文话术，直接透传）。"""
        args = []
        if kind == "改图":
            args = ["--edit"]
        elif kind == "打卡":
            slot = self.gate._slot(uid, 1)
            if slot is None:
                return "（你想把自己放进对方刚发的图里，但最近没有图可用）"
            args = ["--photo", str(slot["path"])]
        script = ROOT / str(self.cfg.get("genimg_script")
                            or "tools/生图/gen.py")
        self.log.info("生图[%s]：%r", kind, free[:40])
        try:
            # gen.py 用 ensure_ascii=False 打印含中文路径的 JSON——
            # 子进程 stdout 走 locale(GBK)，与 utf-8 解码错配会毁路径。
            # server_sdk 靠模块顶部 setdefault PYTHONIOENCODING 兜底，桥也对齐
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script), free, *args, cwd=str(ROOT),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(),
                                                timeout=360)
            except asyncio.TimeoutError:
                proc.kill()
                return "（拍立得卡纸了，照片没洗出来）"
        except OSError as e:
            self.log.warning("生图子进程启动失败：%s", e)
            return "（拍立得好像坏了，照片没洗出来）"
        try:
            last = out.decode("utf-8", "replace").strip().splitlines()[-1]
            res = json.loads(last)
        except Exception:
            self.log.warning("生图输出解析失败：%r", out[:200])
            return "（拍立得卡纸了，照片没洗出来）"
        if not res.get("ok"):
            err = str(res.get("error") or "未知原因")
            self.log.info("生图失败：%s", err)
            return f"（照片没洗出来——{err}）"
        path = res.get("path") or ""
        try:
            b64 = base64.b64encode(Path(path).read_bytes()).decode()
        except OSError as e:
            self.log.warning("生图产物读取失败：%s", e)
            return "（照片洗出来了但弄丢了，奇怪）"
        try:
            await self.transport.send_image_msg(uid, b64)
            self.log.info("→ QQ %s：[生图:%s]", uid, Path(path).name)
        except Exception as e:
            self.log.error("生图发送失败（%s）：%s", uid, e)
            return "（照片洗好了但没发出去，信号不太好）"
        # 双烧合一：回报 server 产物路径 → server 只做交付注入（她回看的图
        # = 群友收到的这张），server 不再为 qq 会话自己跑 gen.py
        if self.ws is not None:
            try:
                await self.ws.send(json.dumps(
                    {"type": "qq_genimg_done", "mode": "qq",
                     "ok": True, "path": path}, ensure_ascii=False))
            except Exception as e:
                self.log.warning("生图回报 server 失败（她收不到回看提示）：%s", e)
        return "（照片洗好了，已经发过去了）"

    async def _send_stickers(self, uid, raw_text):
        """检出 [表情:名字] → 目录映射图片发 image 段（跟在正文后面）。
        napcat 通道限定（official 发图要先传富媒体，未做）；名字对不上文件
        就跳过——她已经知道只用清单里有的名字。"""
        if self.channel != "napcat" or not hasattr(self.transport,
                                                   "send_image_msg"):
            return
        names = STICKER_RE.findall(raw_text or "")
        if not names:
            return
        cap = int(self.cfg.get("sticker_max_per_reply") or 0)
        for name in names[:cap]:
            path = self._find_sticker(name)
            if path is None:
                self.log.info("表情 %r 没图（表情包目录缺文件），跳过", name)
                continue
            b64 = self._load_sticker_b64(path)
            if b64 is None:
                continue
            try:
                await self.transport.send_image_msg(uid, b64)
                self.log.info("→ QQ %s：[表情:%s]", uid, name)
            except Exception as e:
                self.log.error("表情发送失败（%s）：%s", name, e)
                return

    def _track_turn_end(self, uid, spoke):
        """会话态记账：她说话了 → 进入/续期会话（盯手机模式）；
        整轮只回[静默] → 失趣计数，连续 N 次退出会话态。"""
        s = str(uid)
        if not s.startswith("g:"):
            return
        trk = self.engage.get(s[2:])
        if trk is None or self.turn_sent:
            return                                   # 本轮已记过（多段回复）
        t = time.time() / 60.0
        if spoke:
            newly = not trk.engaged
            trk.on_her_reply(t)
            self.turn_sent = True
            if newly:
                self.log.info("会话态进入（群%s，她开口了）", s[2:])
        else:
            before = trk.exits["silent"]
            trk.on_silent(t)
            if trk.exits["silent"] > before:
                self.log.info("会话态退出（群%s，连续静默失趣）", s[2:])

    async def _notify(self, text):
        """给白名单用户发系统级提示（预设没配好等）。"""
        if not self.transport.peer_online:
            return
        for uid in list(self.allow_from)[:1]:
            try:
                await self.transport.send_private_msg(uid, text)
            except Exception:
                pass

    # ---------- 入口 ----------

    async def run(self):
        await self.transport.start()
        try:
            n_stk = sum(1 for p in self._sticker_dir().iterdir()
                        if p.suffix.lower() in STICKER_EXTS)
        except OSError:
            n_stk = 0
        self.log.info("通道=%s 白名单=%s 群=%s 防抖=%ss 表情包=%d个", self.channel,
                      sorted(self.allow_from) or "(全放开)",
                      sorted(self.allow_groups) or "(不接群)",
                      self.cfg.get("debounce_sec"), n_stk)
        await asyncio.gather(self.server_loop(),      # 永不返回（重连循环）
                             self._poisson_loop())    # 泊松关=内部回落旧定时器


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(HERE / "bridge.log", encoding="utf-8")]
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=handlers)
    log = logging.getLogger("qq-bridge")
    cfg = load_cfg()
    if not cfg.get("enabled"):
        raise SystemExit(
            "presets.json 的 qq.enabled=false——先配置并打开开关再启动桥")
    try:
        asyncio.run(Bridge(cfg).run())
    except KeyboardInterrupt:
        log.info("桥退出（Ctrl+C）")


if __name__ == "__main__":
    main()

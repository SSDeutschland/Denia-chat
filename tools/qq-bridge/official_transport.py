# -*- coding: utf-8 -*-
"""QQ 官方机器人通道（bot.q.qq.com API v2）：gateway WS 收事件 + REST 发消息。

与 onebot_transport.OneBotServer 同接口（start / send_private_msg /
peer_online / on_event 回调吐 OneBot 形状的私聊事件），bridge.py 按
presets.json qq 节的 channel 字段二选一。零封号风险（官方通道），代价是
被动回复窗口限制 + 主动消息权限要申请。

鉴权链：appid+secret → getAppAccessToken（7200s，80% TTL 自刷）
  → 全部请求带 Authorization: QQBot {access_token}。

收（gateway WS）：op10 hello（heartbeat_interval）→ op2 identify
  （intents = 1<<25 = GROUP_AND_C2C_EVENT，只接 C2C_MESSAGE_CREATE）
  → op0 READY（session_id）→ 周期 op1 心跳（d=最新 seq）。
  断线重连：有 session_id 走 op6 resume 补事件；op9 invalid session
  （d=false）→ 丢掉 session 重新 identify；op7 服务端要求重连 → 照做。

发（REST）：POST /v2/users/{openid}/messages
  被动回复带 msg_id + msg_seq——msg_id 60min 内最多用 4 次（官方限制），
  本模块按 openid 记录最近一次 C2C 的 msg_id 与已用次数（TTL 留 5min 余量）。
  窗口用尽时：proactive=True 才回落主动消息（不带 msg_id，须平台申请权限，
  未批会吃 40034102）；否则抛异常由桥侧记日志。
  常见错误码：40034128 被动消息失效/超限、40034102 主动消息无权限。

openid 说明：官方通道的用户标识是 openid 字符串（≠QQ 号），白名单走
presets.json 的 official_allow_from。openid 从 C2C 事件里现抄（桥日志会打）。
"""
import asyncio
import json
import logging
import time

import aiohttp
import websockets

LOG = logging.getLogger("qq-bridge.official")

ATTACHMENT_PLACEHOLDER = "（对方发来了图片/文件，但这条通道看不到内容）"


class OfficialTransport:
    """官方 bot 通道。start() 立即返回，gateway 重连循环在后台任务里。"""

    API_PROD = "https://api.sgroup.qq.com"
    API_SANDBOX = "https://sandbox.api.sgroup.qq.com"   # 2026-01 起沙箱无群聊
    TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
    INTENTS = 1 << 25          # GROUP_AND_C2C_EVENT（C2C + 群@ 都含）
    MSG_ID_TTL = 55 * 60       # C2C 被动凭证：官方 60min，留 5min 余量
    MSG_ID_MAX_USE = 4         # C2C 单 msg_id 最多回 4 条
    GROUP_MSG_ID_TTL = 4.5 * 60    # 群@被动凭证：官方 5min，留 30s 余量
    GROUP_MSG_ID_MAX_USE = 5       # 群单 msg_id 最多回 5 条

    def __init__(self, appid, secret, sandbox=False, proactive=False,
                 on_event=None, api_base="", token_url=""):
        self.appid, self.secret = str(appid), str(secret)
        self.api_base = api_base or (
            self.API_SANDBOX if sandbox else self.API_PROD)
        self.token_url = token_url or self.TOKEN_URL
        self.proactive = bool(proactive)
        self.on_event = on_event            # async callback(OneBot 形状 event)
        self._http = None                   # aiohttp.ClientSession
        self._token = None
        self._token_deadline = 0.0
        self._ws = None
        self._session_id = None             # resume 用
        self._seq = None                    # 最后收到的事件序号 s
        self._hb_interval = 41.25
        self._hb_task = None
        self._stopping = False
        self._last_msg = {}                 # openid -> {"id","ts","used"}

    # ---------- 对外接口（与 OneBotServer 对齐） ----------

    @property
    def peer_online(self):
        return self._ws is not None

    async def start(self):
        if not self.appid or not self.secret:
            raise SystemExit(
                "channel=official 但 official_appid/official_secret 未配置")
        self._http = aiohttp.ClientSession()
        asyncio.create_task(self._gateway_loop())
        LOG.info("官方通道启动（%s，proactive=%s）",
                 "沙箱" if "sandbox" in self.api_base else "正式", self.proactive)

    async def stop(self):
        self._stopping = True
        if self._hb_task:
            self._hb_task.cancel()
        if self._ws:
            await self._ws.close()
        if self._http:
            await self._http.close()

    async def send_private_msg(self, user_id, text):
        """user_id = openid（C2C）或 "g:"+group_openid（群回复，桥侧前缀路由）。
        优先被动回复（带 msg_id）；被动凭证被官方拒收（40034128）且开了
        proactive 时当场回落主动消息重发一次。"""
        target = str(user_id)
        is_group = target.startswith("g:")
        await self._ensure_token()
        body = {"content": text, "msg_type": 0}
        ttl = self.GROUP_MSG_ID_TTL if is_group else self.MSG_ID_TTL
        max_use = self.GROUP_MSG_ID_MAX_USE if is_group else self.MSG_ID_MAX_USE
        rec = self._last_msg.get(target)
        passive_ok = (rec is not None
                      and time.time() - rec["ts"] < ttl
                      and rec["used"] < max_use)
        if passive_ok:
            rec["used"] += 1
            body["msg_id"] = rec["id"]
            body["msg_seq"] = rec["used"]
        elif not self.proactive:
            raise RuntimeError(
                "被动回复窗口已过/用尽，且 official_proactive=false"
                "（主动消息权限须去 QQ 开放平台申请）")
        if is_group:
            url = f"{self.api_base}/v2/groups/{target[2:]}/messages"
        else:
            url = f"{self.api_base}/v2/users/{target}/messages"
        try:
            return await self._post(url, body)
        except RuntimeError as e:
            if body.get("msg_id") and "40034128" in str(e):
                if rec is not None:
                    rec["used"] = max_use               # msg_id 作废
                if self.proactive:
                    LOG.info("被动回复被拒（40034128），当场回落主动消息")
                    body.pop("msg_id", None)
                    body.pop("msg_seq", None)
                    return await self._post(url, body)
            raise

    async def _post(self, url, body):
        async with self._http.post(url, json=body,
                                   headers=self._auth()) as r:
            raw = await r.text()
        if r.status // 100 != 2:
            hint = ("（主动消息权限未批，去平台申请）" if "40034102" in raw
                    else "")
            raise RuntimeError(
                f"官方发消息失败 HTTP {r.status}{hint}：{raw[:200]}")
        return json.loads(raw) if raw.strip() else {}

    # ---------- 鉴权 ----------

    def _auth(self):
        return {"Authorization": f"QQBot {self._token}"}

    async def _ensure_token(self):
        if self._token and time.time() < self._token_deadline:
            return
        async with self._http.post(
                self.token_url,
                json={"appId": self.appid, "clientSecret": self.secret}) as r:
            data = await r.json(content_type=None)
        tok = data.get("access_token")
        if not tok:
            raise RuntimeError(f"getAppAccessToken 失败：{data}")
        self._token = tok
        # expires_in 官方返回字符串（"7200"），80% TTL 提前刷
        self._token_deadline = time.time() + int(
            data.get("expires_in") or 7200) * 0.8
        LOG.info("access_token 已刷新")

    # ---------- gateway ----------

    async def _gateway_loop(self):
        backoff = 3
        while not self._stopping:
            try:
                await self._ensure_token()
                async with self._http.get(self.api_base + "/gateway",
                                          headers=self._auth()) as r:
                    gw = await r.json(content_type=None)
                url = gw.get("url")
                if not url:
                    raise RuntimeError(f"/gateway 失败：{gw}")
                async with websockets.connect(
                        url, max_size=16 * 1024 * 1024) as ws:
                    self._ws = ws
                    backoff = 3
                    LOG.info("gateway 已连接%s",
                             "（将 resume）" if self._session_id else "")
                    await self._session(ws)
            except Exception as e:
                if not self._stopping:
                    LOG.warning("gateway 断开：%s（%ds 后重连）", e, backoff)
            finally:
                self._ws = None
                hb, self._hb_task = self._hb_task, None
                if hb:
                    hb.cancel()
            if not self._stopping:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _session(self, ws):
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            op = payload.get("op")
            if op == 10:                        # hello
                d = payload.get("d") or {}
                self._hb_interval = d.get("heartbeat_interval", 41250) / 1000.0
                self._hb_task = asyncio.create_task(self._heartbeat(ws))
                if self._session_id:
                    await self._send(ws, 6, {
                        "token": self._auth()["Authorization"],
                        "session_id": self._session_id,
                        "seq": self._seq or 0})
                else:
                    await self._send(ws, 2, {
                        "token": self._auth()["Authorization"],
                        "intents": self.INTENTS,
                        "shard": [0, 1],
                        "properties": {"$os": "windows", "$browser": "denia",
                                       "$device": "denia"}})
            elif op == 11:                      # 心跳 ack
                continue
            elif op == 7:                       # 服务端要求重连（保留 session resume）
                await ws.close()
                return
            elif op == 9:                       # invalid session
                if not payload.get("d"):        # d=false：不可 resume
                    self._session_id = None
                    self._seq = None
                await ws.close()
                return
            elif op == 0:                       # dispatch
                self._seq = payload.get("s", self._seq)
                t = payload.get("t")
                if t == "READY":
                    self._session_id = (payload.get("d") or {}).get("session_id")
                    LOG.info("gateway READY（session=%s）", self._session_id)
                elif t == "RESUMED":
                    LOG.info("gateway RESUMED（session=%s）", self._session_id)
                elif t == "C2C_MESSAGE_CREATE":
                    await self._on_c2c(payload.get("d") or {})
                elif t == "GROUP_AT_MESSAGE_CREATE":
                    await self._on_group_at(payload.get("d") or {})
                elif t in ("GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT"):
                    d = payload.get("d") or {}
                    LOG.info("群事件 %s：group_openid=%s", t,
                             d.get("group_openid"))
                # 其余（好友变更等）暂不处理

    async def _on_c2c(self, d):
        openid = (d.get("author") or {}).get("user_openid")
        if not openid:
            return
        msg_id = d.get("id")
        if msg_id:                              # 记录被动回复凭证
            self._last_msg[openid] = {"id": msg_id, "ts": time.time(),
                                      "used": 0}
        text = (d.get("content") or "").strip()
        if not text and d.get("attachments"):
            text = ATTACHMENT_PLACEHOLDER
        if not text:
            return
        if self.on_event is not None:
            await self.on_event({
                "post_type": "message", "message_type": "private",
                "user_id": openid, "message": text,
                "self_id": self.appid, "time": d.get("timestamp")})

    async def _on_group_at(self, d):
        """群里 @bot。回复目标是群（"g:"+group_openid），凭证按群记账
        （5min/5条，比 C2C 紧）。带上发言者尾号让她知道谁在说话。"""
        group_openid = d.get("group_openid")
        member = (d.get("author") or {}).get("member_openid") or "?"
        if not group_openid:
            return
        msg_id = d.get("id")
        if msg_id:
            self._last_msg["g:" + group_openid] = {"id": msg_id,
                                                   "ts": time.time(),
                                                   "used": 0}
        text = (d.get("content") or "").strip()
        if not text and d.get("attachments"):
            text = ATTACHMENT_PLACEHOLDER
        if not text:
            return
        if self.on_event is not None:
            await self.on_event({
                "post_type": "message", "message_type": "group",
                "user_id": "g:" + group_openid,
                "message": f"（群聊·成员{member[-6:]}）{text}",
                "self_id": self.appid, "time": d.get("timestamp")})

    async def _heartbeat(self, ws):
        try:
            while True:
                await asyncio.sleep(self._hb_interval * 0.9)
                await self._send(ws, 1, self._seq)   # d=最新 s，无则 null
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    @staticmethod
    async def _send(ws, op, d):
        await ws.send(json.dumps({"op": op, "d": d}, ensure_ascii=False))

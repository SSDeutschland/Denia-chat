# -*- coding: utf-8 -*-
"""OneBot v11 反向 WS 服务端（裸 websockets 实现，零新依赖）。

背景：aiocqhttp 1.4.4 + Quart 0.19/0.21 在 Python 3.14 下 WS 握手 400
（探针 tools/qq-bridge/probe_aiocqhttp.py 实测 FAIL），故传输层自写。
协议就是 JSON 帧：入站 event（post_type）/ action 响应（echo），
出站 action（{"action","params","echo"}）。NapCat 作为客户端连入。

只实现最小私聊验证所需：message/private 事件 + send_private_msg 动作。
"""
import asyncio
import hmac
import json
import logging
import uuid

import websockets

LOG = logging.getLogger("qq-bridge.onebot")


class OneBotServer:
    """反向 WS 服务端：监听等 NapCat 连入。新连接顶替旧连接（NapCat 重连）。"""

    def __init__(self, host, port, token="", self_id=0, on_event=None):
        self.host, self.port = host, int(port)
        self.token = token or ""
        self.self_id = int(self_id or 0)
        self.on_event = on_event      # async callback(event_dict)
        self.on_connect = None        # sync callback()：每次 NapCat 连入（含重连）
        self._ws = None               # 当前 NapCat 连接（None=离线）
        self._pending = {}            # echo -> Future（action 响应）
        self._server = None

    @property
    def napcat_online(self):
        return self._ws is not None

    @property
    def peer_online(self):
        """与 OfficialTransport 对齐的通用接口（桥侧用）。"""
        return self._ws is not None

    async def start(self):
        self._server = await websockets.serve(self._handler, self.host, self.port)
        LOG.info("OneBot 反向 WS 监听 ws://%s:%d（等 NapCat 连入）",
                 self.host, self.port)

    async def _handler(self, ws):
        # 握手校验：NapCat 连入带 x-self-id / authorization 头
        try:
            hdrs = ws.request.headers
            sid = int(hdrs.get("x-self-id") or 0)
            auth = hdrs.get("authorization") or ""
        except Exception:
            sid, auth = 0, ""
        if self.self_id and sid and sid != self.self_id:
            LOG.warning("拒绝连入：self_id=%s 与配置 self_id=%s 不符", sid, self.self_id)
            await ws.close(4003, "wrong self_id")
            return
        if self.token and not hmac.compare_digest(auth, f"Bearer {self.token}"):
            LOG.warning("拒绝连入：accessToken 校验失败")
            await ws.close(4001, "bad token")
            return
        if self._ws is not None:
            LOG.warning("新 NapCat 连接顶替旧连接")
            try:
                await self._ws.close(4000, "replaced")
            except Exception:
                pass
        LOG.info("NapCat 已连入（self_id=%s）", sid or "?")
        self._ws = ws
        if self.on_connect is not None:
            try:
                self.on_connect()
            except Exception:
                LOG.exception("on_connect 回调异常")
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                if "echo" in data:                      # action 响应
                    fut = self._pending.pop(data.get("echo"), None)
                    if fut is not None and not fut.done():
                        fut.set_result(data)
                elif data.get("post_type") == "meta_event":
                    continue                            # 心跳/生命周期：吞掉
                elif data.get("post_type"):
                    if self.on_event is not None:
                        await self.on_event(data)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self._ws is ws:
                self._ws = None
            LOG.info("NapCat 断开")

    async def call(self, action, params, timeout=10):
        """发 action 并等 echo 响应。NapCat 离线/超时/失败都抛异常。"""
        if self._ws is None:
            raise RuntimeError("NapCat 未连接")
        echo = uuid.uuid4().hex[:8]
        fut = asyncio.get_event_loop().create_future()
        self._pending[echo] = fut
        try:
            await self._ws.send(json.dumps(
                {"action": action, "params": params, "echo": echo},
                ensure_ascii=False))
            resp = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(echo, None)
        if resp.get("status") == "failed" or resp.get("retcode") not in (0, None):
            raise RuntimeError(
                f"OneBot {action} 失败：{resp.get('wording') or resp}")
        return resp.get("data")

    async def send_private_msg(self, user_id, text):
        # "g:<群号>" 前缀路由到群发送（与 OfficialTransport 的接口约定对齐，
        # 桥侧 _reply 不区分私聊/群）
        if str(user_id).startswith("g:"):
            return await self.call("send_group_msg",
                                   {"group_id": int(str(user_id)[2:]),
                                    "message": text})
        return await self.call("send_private_msg",
                               {"user_id": int(user_id), "message": text})

    async def send_image_msg(self, user_id, file_b64):
        """发图片消息（image 段）。走 base64:// 而非 file:/// 路径——
        项目路径带中文空格，file URI 编码在 NapCat 侧容易踩坑。"""
        seg = [{"type": "image", "data": {"file": f"base64://{file_b64}"}}]
        if str(user_id).startswith("g:"):
            return await self.call("send_group_msg",
                                   {"group_id": int(str(user_id)[2:]),
                                    "message": seg})
        return await self.call("send_private_msg",
                               {"user_id": int(user_id), "message": seg})

    async def send_record_msg(self, user_id, file_b64):
        """发语音消息（record 段）。wav base64 给 NapCat，由它自带的
        ffmpeg（native/ffmpeg）转 silk——没有 ffmpeg 时只认 silk 格式。"""
        seg = [{"type": "record", "data": {"file": f"base64://{file_b64}"}}]
        if str(user_id).startswith("g:"):
            return await self.call("send_group_msg",
                                   {"group_id": int(str(user_id)[2:]),
                                    "message": seg})
        return await self.call("send_private_msg",
                               {"user_id": int(user_id), "message": seg})

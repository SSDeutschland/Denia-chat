# -*- coding: utf-8 -*-
"""QQ 四通道识图门控（napcat 通道限定）。

四通道（用户 2026-08-18 拍板）：
  略读 —— 全自动兜底：所有入站图压小图走便宜模型取一句轮廓。触发她的
          消息同步等结果（≤vision_glance_sync_sec）直接换占位；群背景图
          异步完成后以 (图注) 行括注进群聊记录，她下次读上下文自然看到。
  看图 —— 她主动写 [看图]/[看图:N]/[细看:问题]：原图走好模型细看/追问，
          结果以"（你点开那张图仔细看了看：…）"注入，她自然接话。
  缓存 —— QQ 图片文件名多为哈希、mface 有稳定 emoji_id：首次描述落
          表情包缓存.json，重复出现零调用，占位直接带简述。
  无视 —— 自动降级兜底：每群每分钟略读闸、日上限、API 429/529 冷却，
          踩线只剩最朴素的（对方发了张图），不报错不堵消息。

模型/压缩在 server_sdk（qq_vision WS 端点）；本模块管：下载（get_image
/URL）→ 本地图片缓存、描述缓存（LRU）、每会话图槽、频率/日上限/冷却。
"""
import asyncio
import hashlib
import json
import re
import shutil
import time
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# 她写的主动看图标记（剥除在 strip_markers.QQ_MARK_RE 同步登记）
LOOK_RE = re.compile(r"[\[【]看图(?::(\d))?[\]】]")
ASK_RE = re.compile(r"[\[【]细看[:：]\s*([^\]】]{1,200}?)\s*[\]】]")

# 投递文本里的待决占位 token（debounce 结算时替换成最终措辞）
TOKEN_RE = re.compile(r"⟦V(\d+)⟧")


class VisionGate:
    def __init__(self, cfg, log, root, ws_call, log_note):
        """ws_call(op, path, context="", question="") → dict（server qq_vision
        响应）；log_note(gid, text) → 群聊记录追加 (图注) 行。"""
        self.cfg = cfg
        self.log = log
        self.dir = root / "denia" / "缓冲" / "图片缓存"
        self.cache_file = root / "denia" / "缓冲" / "表情包缓存.json"
        self._ws_call = ws_call
        self._log_note = log_note
        self.cache = {"meta": {}, "items": {}}       # 描述缓存（懒加载）
        self.slots = {}                              # buf_uid -> deque[图槽]
        self.tasks = {}                              # token -> asyncio.Task
        self._token_seq = 0
        self._min_bucket = {}                        # 群 -> 本分钟略读次数
        self._cooldown_until = 0.0                   # 429/529 冷却截止

    # ---------- 配置 ----------

    def _c(self, key):
        return self.cfg.get(key)

    @property
    def enabled(self):
        return bool(self._c("vision_qq_enabled"))

    # ---------- 消息链拆分 ----------

    @staticmethod
    def split_images(message):
        """array 消息链 → (去图消息链, [图段])。图段={kind,file,url}：
        image 段 file 多为 QQ 哈希文件名；mface 用 emoji_id/url 做缓存键。"""
        if not isinstance(message, list):
            return message, []
        rest, imgs = [], []
        for seg in message:
            if not isinstance(seg, dict):
                rest.append(seg)
                continue
            st = seg.get("type")
            data = seg.get("data") or {}
            if st == "image":
                imgs.append({"kind": "image",
                             "file": str(data.get("file") or ""),
                             "url": str(data.get("url") or "")})
            elif st == "mface":
                key = (str(data.get("emoji_id") or "")
                       or str(data.get("key") or "")
                       or str(data.get("url") or ""))
                imgs.append({"kind": "mface", "file": f"mface_{key}",
                             "url": str(data.get("url") or "")})
            else:
                rest.append(seg)
        return rest, imgs

    # ---------- 描述缓存 ----------

    def _load_cache(self):
        if self.cache["items"] or self.cache["meta"]:
            return
        try:
            obj = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and isinstance(obj.get("items"), dict):
                self.cache = obj
        except Exception:
            pass

    def _save_cache(self):
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            items = self.cache["items"]
            cap = int(self._c("vision_cache_max") or 500)
            while len(items) > cap:                  # LRU：踢最久没出现的
                oldest = min(items, key=lambda k: items[k].get("last", 0))
                items.pop(oldest, None)
            self.cache_file.write_text(
                json.dumps(self.cache, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError as e:
            self.log.warning("表情包缓存写入失败：%s", e)

    def _cache_key(self, img):
        name = img["file"] or img["url"]
        return name or hashlib.md5(img["url"].encode()).hexdigest()

    # ---------- 门控 ----------

    def _today(self):
        m = self.cache["meta"]
        d = datetime.now().strftime("%Y-%m-%d")
        if m.get("date") != d:
            m.clear()
            m.update({"date": d, "glance": 0, "look": 0})
        return m

    def _glance_allowed(self, gid):
        """无视通道判定：冷却中 / 每群每分钟闸 / 日上限 → False。"""
        if time.time() < self._cooldown_until:
            return False
        meta = self._today()
        if meta.get("glance", 0) >= int(self._c("vision_glance_daily_cap") or 0):
            return False
        bucket = time.strftime("%Y%m%d%H%M")
        self._min_bucket = {k: v for k, v in self._min_bucket.items()
                            if k.endswith(bucket)}   # 只留当前分钟的桶
        key = f"{gid or 'private'}:{bucket}"
        n = self._min_bucket.get(key, 0)
        if n >= int(self._c("vision_glance_per_group_min") or 0):
            return False
        self._min_bucket[key] = n + 1
        meta["glance"] = meta.get("glance", 0) + 1
        return True

    def _look_allowed(self):
        meta = self._today()
        if time.time() < self._cooldown_until:
            return False
        if meta.get("look", 0) >= int(self._c("vision_look_daily_cap") or 0):
            return False
        meta["look"] = meta.get("look", 0) + 1
        return True

    def _note_429(self, err):
        """API 限流 → 冷却 N 分钟全员无视（不报错不堵消息）。"""
        if "HTTP 429" in err or "HTTP 529" in err or "429" == err.strip()[:3]:
            mins = float(self._c("vision_cooldown_min") or 5)
            self._cooldown_until = time.time() + mins * 60
            self.log.warning("识图 API 限流，无视通道冷却 %g 分钟", mins)

    # ---------- 下载 ----------

    async def _download(self, img):
        """图段 → 本地缓存文件路径（失败 None）。image 段优先 get_image
        本机直取，回落 URL；mface 走 URL。文件名净化防目录穿越。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        key = re.sub(r"[^\w.-]", "_", self._cache_key(img))[:80] or "img"
        dst = self.dir / key
        if dst.suffix.lower() not in IMG_EXTS:
            ext = Path(img["file"]).suffix.lower()
            dst = dst.with_suffix(ext if ext in IMG_EXTS else ".jpg")
        if dst.is_file():
            return dst
        if img["kind"] == "image" and img["file"]:
            try:
                data = await self._get_image(img["file"])
                src = (data or {}).get("file") or ""
                if src and Path(src).is_file():
                    shutil.copyfile(src, dst)
                    return dst
            except Exception as e:
                self.log.info("get_image 失败（%s），试 URL：%s", img["file"], e)
        if img["url"]:
            try:
                await asyncio.to_thread(self._fetch_url, img["url"], dst)
                return dst
            except Exception as e:
                self.log.warning("图片下载失败（%s）：%s", img["url"][:60], e)
        return None

    async def _get_image(self, file):
        raise NotImplementedError                  # 由桥注入 transport.call

    @staticmethod
    def _fetch_url(url, dst):
        req = urllib.request.Request(url, headers={"User-Agent": "QQ/9"})
        with urllib.request.urlopen(req, timeout=15) as r, \
                open(dst, "wb") as f:
            shutil.copyfileobj(r, f)

    # ---------- 尺寸/措辞 ----------

    def _probe(self, path):
        """读尺寸 → (w, h, 小图?)；Pillow 不可用按字节数估。"""
        small_kb = int(self._c("vision_small_kb") or 60)
        small_px = int(self._c("vision_small_px") or 400)
        try:
            from PIL import Image
            with Image.open(path) as im:
                w, h = im.size
            return w, h, (max(w, h) <= small_px
                          or path.stat().st_size < small_kb * 1024)
        except Exception:
            try:
                return 0, 0, path.stat().st_size < small_kb * 1024
            except OSError:
                return 0, 0, True

    @staticmethod
    def _ph_generic(small):
        return "（对方发了个表情，想看就说）" if small \
            else "（对方发来一张图，想看就说）"

    @staticmethod
    def _ph_brief(small, brief):
        return (f"（对方发了个表情：{brief}）" if small
                else f"（对方发来一张图：{brief}）")

    # ---------- 入站（四通道主流程）----------

    async def admit(self, buf_uid, imgs, triggering, gid=None):
        """处理一条消息里的全部图段。返回 (log占位, 投递占位) 两段文字。
        triggering=True 时投递占位含 ⟦Vn⟧ token，debounce 结算时
        await resolve() 换最终措辞；非触发消息只落 log 占位+异步略读。

        ⚠️ 本函数在 NapCat 读循环里被 await（on_event 内联），**绝不能
        内联等 transport.call**——echo 响应要读循环派发，内联等 action
        响应 = 自死锁。下载/略读全部甩后台 task，这里只做同步判定。"""
        if not imgs:
            return "", ""
        if not self.enabled:
            ph = "（对方发来了一张图片，但这条通道看不到图）" * len(imgs)
            return ph, ph
        self._load_cache()
        log_ph, del_ph = [], []
        for img in imgs:
            key = self._cache_key(img)
            hit = self.cache["items"].get(key)
            small = (hit or {}).get("small")
            if small is None:                    # 未下载前的粗猜
                small = img["kind"] == "mface"   # 表情按小图、其余按大图
            if hit and hit.get("brief"):         # ③缓存通道：零调用
                hit["count"] = hit.get("count", 0) + 1
                hit["last"] = time.time()
                asyncio.create_task(             # 后台补本地文件+图槽
                    self._ensure_local(buf_uid, key, img, hit))
                ph = self._ph_brief(small, hit["brief"])
                log_ph.append(ph)
                del_ph.append(ph)
                continue
            if not self._glance_allowed(gid):    # ④无视通道
                log_ph.append("（对方发了张图）")
                del_ph.append("（对方发来一张图，想看就说）")
                continue
            # ①略读通道：下载+图槽+略读全在后台 task
            # （触发消息的结果会换进投递文案，不必再往群 log 括注图注）
            task = asyncio.create_task(
                self._glance_flow(buf_uid, key, img, small,
                                  None if triggering else gid))
            if triggering:
                self._token_seq += 1
                self.tasks[self._token_seq] = (task, small)
                del_ph.append(f"⟦V{self._token_seq}⟧")
                log_ph.append(self._ph_generic(small))
            else:
                log_ph.append(self._ph_generic(small))
                del_ph.append(self._ph_generic(small))
        return "".join(log_ph), "".join(del_ph)

    async def _ensure_local(self, buf_uid, key, img, hit):
        """缓存命中图的后台补取：本地文件+图槽（供之后的 [看图]）。"""
        path = await self._download(img)
        if path is not None:
            self._push_slot(buf_uid, key, path, hit)

    async def _glance_flow(self, buf_uid, key, img, small_guess, note_gid):
        """下载 → 图槽 → 略读（后台 task 全链）。返回 (brief|None, small)。"""
        path = await self._download(img)
        small = small_guess
        if path is not None:
            small = self._probe(path)[2]
            self._push_slot(buf_uid, key, path, None)
        brief = await self._glance(key, path, small, note_gid)
        return brief, small

    async def _glance(self, key, path, small, gid):
        """略读：成功落缓存；非触发来源追加 (图注) 群 log 行。"""
        if path is None:
            return None
        try:
            resp = await self._ws_call("glance", str(path))
        except Exception as e:
            self.log.info("略读请求失败：%s", e)
            return None
        if not resp.get("ok"):
            self._note_429(str(resp.get("error") or ""))
            self.log.info("略读失败：%s", resp.get("error"))
            return None
        brief = " ".join(str(resp.get("desc") or "").split())[:80]
        if not brief:
            return None
        w, h, _ = self._probe(path)
        self.cache["items"][key] = {
            "brief": brief, "small": small, "w": w, "h": h,
            "count": 1, "last": time.time()}
        self._save_cache()
        self.log.info("略读：%s → %s", key[:30], brief[:40])
        if gid:                                      # 背景图：括注进群记录
            self._log_note(gid, f"(图注) {brief}")
        return brief

    async def resolve(self, text):
        """debounce 结算：⟦Vn⟧ token → 略读结果（同步等 ≤配置秒数，
        超时/失败回通用占位）。只等本轮消息自己的 token。"""
        tokens = TOKEN_RE.findall(text or "")
        if not tokens:
            return text
        budget = float(self._c("vision_glance_sync_sec") or 5)
        deadline = time.time() + budget
        for tok in tokens:
            entry = self.tasks.pop(int(tok), None)
            if entry is None:
                text = text.replace(f"⟦V{tok}⟧", "（对方发来一张图，想看就说）")
                continue
            task, small = entry
            brief = None
            remain = deadline - time.time()
            if remain > 0:
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(task), timeout=remain)
                    if result:
                        brief, small = result   # 用探测后的真实尺寸措辞
                except (asyncio.TimeoutError, Exception):
                    brief = None
            ph = self._ph_brief(small, brief) if brief \
                else self._ph_generic(small)
            text = text.replace(f"⟦V{tok}⟧", ph)
        return text

    # ---------- 图槽（[看图:N] 回溯）----------

    def _push_slot(self, buf_uid, key, path, hit):
        n = int(self._c("vision_slots") or 5)
        dq = self.slots.setdefault(buf_uid, deque(maxlen=n))
        dq.append({"key": key, "path": path, "ts": time.time(),
                   "brief": (hit or {}).get("brief", "")})

    def _slot(self, buf_uid, n):
        dq = self.slots.get(buf_uid)
        if not dq:
            return None
        ttl = float(self._c("vision_slot_ttl_min") or 10) * 60
        now = time.time()
        live = [s for s in dq if now - s["ts"] <= ttl]
        idx = -min(max(n, 1), len(live)) if live else 0
        return live[idx] if live else None

    # ---------- ②看图通道（她主动）----------

    def scan_reply_markers(self, raw_text):
        """她回复里的主动看图请求 → [(op, arg)]：op=look(N 倒数第 N 张)
        / ask(问题)。由桥在剥标记前调用，纯标记段也算她"有动作"。"""
        out = [("look", int(m.group(1) or 1))
               for m in LOOK_RE.finditer(raw_text or "")]
        out += [("ask", m.group(1)) for m in ASK_RE.finditer(raw_text or "")]
        return out[:2]                               # 单段最多处理两个

    async def handle_look(self, buf_uid, n, context=""):
        """[看图:N] → 细看结果注入文本（失败/超帽也给她在世界观内的说法）。"""
        slot = self._slot(buf_uid, n)
        if slot is None:
            return "（你想回头看那张图，但它已经翻过去太久，看不到了）"
        key = slot["key"]
        hit = self.cache["items"].get(key) or {}
        if hit.get("desc"):                          # 细看过的直接复用
            return f"（你点开那张图又看了看：{hit['desc']}）"
        if not self._look_allowed():
            return "（你今天看图看得太多，眼睛有点花了，这张先不看了）"
        try:
            resp = await self._ws_call("look", str(slot["path"]),
                                       context=context)
        except Exception as e:
            self.log.info("看图请求失败：%s", e)
            return "（那张图没加载出来，信号不太好）"
        if not resp.get("ok"):
            self._note_429(str(resp.get("error") or ""))
            return "（那张图没加载出来，信号不太好）"
        desc = str(resp.get("desc") or "").strip()
        hit.update({"desc": desc, "last": time.time()})
        self.cache["items"][key] = hit
        self._save_cache()
        self.log.info("看图：%s → %d 字", key[:30], len(desc))
        return f"（你点开那张图仔细看了看：{desc}）"

    async def handle_ask(self, buf_uid, question):
        """[细看:问题] → 对最近看过的图追问，注入答案。"""
        slot = self._slot(buf_uid, 1)
        if slot is None:
            return "（你想细看，但最近没有图可看）"
        if not self._look_allowed():
            return "（你今天看图看得太多，眼睛有点花了，先不看了）"
        try:
            resp = await self._ws_call("ask", str(slot["path"]),
                                       question=question)
        except Exception as e:
            self.log.info("细看请求失败：%s", e)
            return "（你凑近看了看，但没看太清）"
        if not resp.get("ok"):
            self._note_429(str(resp.get("error") or ""))
            return "（你凑近看了看，但没看太清）"
        ans = str(resp.get("desc") or "").strip()
        self.log.info("细看：%s → %s", question[:30], ans[:40])
        return f"（你又凑近看了看那张图——{question}：{ans}）"

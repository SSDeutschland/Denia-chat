# -*- coding: utf-8 -*-
"""
达妮娅 GUI 后端（正式版）—— Claude Agent SDK + WebSocket，含权限交互层。

  - can_use_tool 权限回调：白名单静默，其余推前端弹窗 → await 用户点击 → Allow/Deny
    （bypass 模式下非白名单也直接放行）
  - include_partial_messages 流式：L2 逐字推送（打字机）
  - WebSocket 双向：服务器可主动推权限请求 / 流式 delta

架构：
  浏览器 ⇄ ws://127.0.0.1:8765/ws ⇄ 本后端 ⇄ ClaudeSDKClient ⇄ 真实 Claude Code 运行时

正式版：cwd=项目根（Denia-skill），setting_sources=["project"] 加载真实 .claude/，
即与终端 `claude` + /denia 完全同一套 skill / 记忆 / 状态。沙箱隔离守卫已移除
（沙箱测试副本仍在 前端设计/沙箱/_frontend_test/，那边保留守卫）。

运行（GUI/venv 不存在时由根目录 启动达妮娅GUI.bat 首跑自建）：
  GUI/venv/Scripts/python.exe GUI/server_sdk.py
"""
import asyncio
import base64
import io
import json
import os
import sys
import time
import urllib.request
import uuid
import logging
import traceback
from datetime import datetime
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, quote

# ═══════════════════════════════════════════════════════════════════
# 运行日志（测试期排 bug 用）
# ───────────────────────────────────────────────────────────────────
# 每次启动生成一个带时间戳的日志文件到 out/，同时打印到控制台。
# 记录：WS 连接、每条收发消息、每轮对话的 SDK 消息流、权限决策、
#       以及所有异常的完整 traceback（这是控制台里看不到的关键信息）。
# ═══════════════════════════════════════════════════════════════════
_LOG_DIR = Path(__file__).resolve().parent / "out"
_LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = _LOG_DIR / f"backend_{datetime.now():%Y%m%d_%H%M%S}.log"

LOG = logging.getLogger("denia")
LOG.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)
LOG.addHandler(_fh)
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
LOG.addHandler(_ch)

import websockets
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    PermissionResultAllow, PermissionResultDeny,
    AssistantMessage, StreamEvent, ResultMessage,
    TextBlock, ThinkingBlock, ToolUseBlock,
    TaskStartedMessage, TaskProgressMessage,
    TaskNotificationMessage, TaskUpdatedMessage,
    TERMINAL_TASK_STATUSES,
)
# transcript 读取（导出对话记录）+ 会话目录解析（存档校验）——SDK 公开 API
try:
    from claude_agent_sdk import get_session_messages
    from claude_agent_sdk._internal.sessions import _find_project_dir
except Exception:  # 版本差异兜底：功能降级但不崩后端
    get_session_messages = None
    _find_project_dir = None
# 进程内 MCP 工具（识图追问 ask_vision）——SDK 公开 API
try:
    from claude_agent_sdk import tool as sdk_tool, create_sdk_mcp_server
except Exception:
    sdk_tool = None
    create_sdk_mcp_server = None

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent   # Denia-skill 项目根（cwd，加载真实 .claude/）
INDEX = HERE / "index.html"
# 表情包图像库：前端把 L2 里的 [表情:文件名] 标记渲染成 <img src="/stickers/文件名">
STICKER_DIR = PROJECT_ROOT / "project" / "工具" / "表情包"
IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp"}
# 用户上传图片缓存：上传走 HTTP POST 落盘，模型经 Read 工具查看（native 识图）
UPLOAD_DIR = HERE / "out" / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
# 导出的对话记录（markdown）落盘目录，前端经 /exports/ 路由下载
EXPORT_DIR = HERE / "out" / "exports"
UPLOAD_MAX = 15 * 1024 * 1024   # 单张上限 15MB
UPLOAD_KEEP_DAYS = 7            # 启动时清理更老的缓存
UPLOAD_PER_MSG = 4              # 每条消息最多附图数
# 落盘前压缩：长边压到 Claude 视觉有效上限（更大也会被服务端缩小，纯烧 token），
# 超阈值的 PNG/WebP 转 JPEG。GIF 可能是动图，不动。
UPLOAD_MAX_EDGE = 1568          # 压缩后长边上限（px）
UPLOAD_COMPRESS_OVER = 400 * 1024   # 超过此字节数才尝试重编码
UPLOAD_JPEG_QUALITY = 80
HOST = "127.0.0.1"
HTTP_PORT = 8765   # 静态页面
WS_PORT = 8766     # WebSocket

# 权限策略：白名单静默放行（记忆读写），其余弹窗
SILENT_TOOLS = {"Read", "Write", "Edit", "TodoWrite", "Skill"}
# 执行命令行的工具（Windows 下 SDK 用 PowerShell；也可能是 Bash）
SHELL_TOOLS = {"Bash", "PowerShell", "Shell"}
PERM_TIMEOUT = 120  # 秒；等前端点击超时则默认拒绝
CONNECT_TIMEOUT = 20  # 秒；建 client 连 provider 超时（短于 SDK 内部 60s，坏 provider 快速报错）

# ═══════════════════════════════════════════════════════════════════
# 预设仓库（provider/模型解耦）
# ───────────────────────────────────────────────────────────────────
# 用户自管的清单，每条 = {id, name, base_url, token, model}。
# provider 靠 options.env 按会话注入、模型靠 options.model —— 不依赖全局环境。
# 存本地 presets.json（git 忽略，含 token）。lastSelected 记住上次选的 id。
# ═══════════════════════════════════════════════════════════════════
PRESETS_FILE = HERE / "presets.json"

# ═══════════════════════════════════════════════════════════════════
# 存档仓库（会话进度保存 / 崩溃后续接）
# ───────────────────────────────────────────────────────────────────
# 对话内容本身一直在 ~/.claude/projects/<proj>/<session_id>.jsonl（CLI 运行时落盘）。
# 崩溃丢失的只是"哪个 session_id 是我们的"这个指针。存档 = 把指针+预设快照落盘。
# checkpoints.json（out/ 已 gitignore）：
#   {"checkpoints":[{id, session_id, created_at, hint, preset:{快照}}], "last_session":{...}}
# 存预设快照而非 preset_id——预设被删/改后存档仍可续。
# ═══════════════════════════════════════════════════════════════════
CHECKPOINTS_FILE = HERE / "out" / "checkpoints.json"


def _read_checkpoints_file():
    """读 checkpoints.json，返回 {checkpoints:[...], last_session:{...}|None}。缺失/损坏回默认。"""
    try:
        data = json.loads(CHECKPOINTS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "checkpoints": data.get("checkpoints", []) or [],
                "last_session": data.get("last_session"),
            }
    except FileNotFoundError:
        pass
    except Exception as e:
        LOG.warning("checkpoints.json 读取失败（按空处理）：%s", e)
    return {"checkpoints": [], "last_session": None}


def _write_checkpoints_file(obj):
    """原子写：temp + os.replace。"""
    CHECKPOINTS_FILE.parent.mkdir(exist_ok=True)
    tmp = CHECKPOINTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CHECKPOINTS_FILE)


def _sessions_dir():
    """当前项目的 CLI transcript 目录（用 SDK 的路径解析，避免手拼转义规则）。"""
    if _find_project_dir is None:
        return None
    try:
        return _find_project_dir(str(PROJECT_ROOT))
    except Exception:
        return None


def _session_jsonl_exists(session_id):
    """该 session 的 transcript 文件是否还在（校验存档能否续接）。"""
    if not session_id:
        return False
    d = _sessions_dir()
    if d is None:
        return True   # 无法定位目录时不阻断续接尝试（resume 失败会另有报错）
    return (d / f"{session_id}.jsonl").is_file()


def _preset_snapshot(p):
    """存档用的预设完整快照（含 token，与 presets.json 同敏感度，本地文件可接受）。"""
    if not p:
        return None
    return {
        "id": p.get("id"), "name": p.get("name"),
        "base_url": p.get("base_url"), "token": p.get("token"),
        "model": p.get("model"), "vision": bool(p.get("vision", True)),
    }


def _checkpoint_view(cp):
    """前端存档列表项：脱敏（不带 token）+ 附 exists 标记。"""
    if not cp:
        return None
    preset = cp.get("preset") or {}
    return {
        "id": cp.get("id"), "session_id": cp.get("session_id"),
        "created_at": cp.get("created_at"), "hint": cp.get("hint"),
        "preset_name": preset.get("name"),
        "exists": _session_jsonl_exists(cp.get("session_id")),
    }


def save_checkpoint_record(session_id, hint, preset, cp_id=None):
    """新增/更新一条存档，返回落盘后的完整列表 obj。cp_id 为 'last' 或 None 时新增。"""
    obj = _read_checkpoints_file()
    rec = {
        "id": cp_id if (cp_id and cp_id != "last") else uuid.uuid4().hex[:8],
        "session_id": session_id,
        "created_at": f"{datetime.now():%Y-%m-%d %H:%M}",
        "hint": (hint or "")[:40],
        "preset": _preset_snapshot(preset),
    }
    obj["checkpoints"].insert(0, rec)   # 新的在前
    _write_checkpoints_file(obj)
    return obj


def update_last_session(session_id, preset):
    """自动兜底项：每次主回合收尾更新。崩溃后可一键续（最多丢正在生成的那轮）。"""
    if not session_id:
        return
    obj = _read_checkpoints_file()
    obj["last_session"] = {
        "session_id": session_id,
        "created_at": f"{datetime.now():%Y-%m-%d %H:%M}",
        "preset": _preset_snapshot(preset),
    }
    _write_checkpoints_file(obj)


def delete_checkpoint_record(cp_id):
    obj = _read_checkpoints_file()
    obj["checkpoints"] = [c for c in obj["checkpoints"] if c.get("id") != cp_id]
    _write_checkpoints_file(obj)
    return obj


def find_checkpoint(cp_id):
    obj = _read_checkpoints_file()
    if cp_id == "last":
        return obj.get("last_session")
    for c in obj["checkpoints"]:
        if c.get("id") == cp_id:
            return c
    return None


def _read_presets_file():
    """读整个 presets.json，返回 {presets:[...], lastSelected:id, vision_relay:{...}|None}。
    损坏/缺失回默认。vision_relay 是识图转接配置（给不支持识图的模型"代看"图片）。"""
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            vr = data.get("vision_relay")
            return {
                "presets": data.get("presets", []) or [],
                "lastSelected": data.get("lastSelected"),
                "vision_relay": vr if isinstance(vr, dict) else None,
            }
    except FileNotFoundError:
        pass
    except Exception as e:
        LOG.warning("presets.json 读取失败（按空处理）：%s", e)
    return {"presets": [], "lastSelected": None, "vision_relay": None}


def _write_presets_file(obj):
    """原子写：temp + os.replace，防崩溃时截断。"""
    tmp = PRESETS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, PRESETS_FILE)


def load_presets():
    return _read_presets_file()["presets"]


def load_last_selected():
    return _read_presets_file()["lastSelected"]


def save_last_selected(pid):
    obj = _read_presets_file()
    obj["lastSelected"] = pid
    _write_presets_file(obj)


def upsert_preset(preset):
    """有 id=改，无 id=增。返回落盘后的 preset（带 id）。"""
    obj = _read_presets_file()
    presets = obj["presets"]
    pid = preset.get("id")
    token = (preset.get("token") or "").strip()
    clean = {
        "id": pid or uuid.uuid4().hex,
        "name": (preset.get("name") or "未命名").strip(),
        "base_url": (preset.get("base_url") or "").strip(),
        "token": token,
        "model": (preset.get("model") or "").strip(),
        "vision": bool(preset.get("vision", True)),  # 模型是否支持识图（决定前端是否显示图片上传）
    }
    if pid:
        for i, p in enumerate(presets):
            if p.get("id") == pid:
                # 编辑时 token 留空 → 保留旧 token（前端拿不到明文，不强制重输）
                if not token:
                    clean["token"] = p.get("token", "")
                presets[i] = clean
                break
        else:
            presets.append(clean)
    else:
        presets.append(clean)
    obj["presets"] = presets
    _write_presets_file(obj)
    return clean


def delete_preset(pid):
    obj = _read_presets_file()
    obj["presets"] = [p for p in obj["presets"] if p.get("id") != pid]
    if obj.get("lastSelected") == pid:
        obj["lastSelected"] = None
    _write_presets_file(obj)


def find_preset(pid):
    for p in load_presets():
        if p.get("id") == pid:
            return p
    return None


def _safe_preset(p):
    """给前端/日志用的脱敏视图：token 只留是否存在。"""
    if not p:
        return None
    return {
        "id": p.get("id"), "name": p.get("name"),
        "base_url": p.get("base_url"), "model": p.get("model"),
        "has_token": bool(p.get("token")),
        "vision": bool(p.get("vision", True)),
    }

# ═══════════════════════════════════════════════════════════════════
# 识图转接（vision relay）
# ───────────────────────────────────────────────────────────────────
# 主模型不支持识图时，把图片交给独立的识图模型"代看"：
# 带上下文调 OpenAI 兼容 /chat/completions（GLM/Qwen-VL 等都支持），
# 生成的描述文字替代"请用 Read 查看"提示注入主模型消息。
# 配置存 presets.json 的 vision_relay 键（与预设同文件同敏感度）。
# ═══════════════════════════════════════════════════════════════════
VISION_RELAY_TIMEOUT = 60   # 秒；识图 API 单次调用超时
VISION_DESC_MAX = 2000      # 描述截断上限（防识图模型话痨撑上下文）
VISION_ASKS_PER_MSG = 3     # 每批图片主模型最多追问次数（ask_vision 工具）


def load_vision_relay():
    return _read_presets_file().get("vision_relay")


def save_vision_relay(cfg):
    """保存识图转接配置。token 留空 = 保留旧值（与预设编辑同规则）。"""
    obj = _read_presets_file()
    old = obj.get("vision_relay") or {}
    token = (cfg.get("token") or "").strip()
    obj["vision_relay"] = {
        "enabled": bool(cfg.get("enabled")),
        "base_url": (cfg.get("base_url") or "").strip().rstrip("/"),
        "token": token or old.get("token", ""),
        "model": (cfg.get("model") or "").strip(),
    }
    _write_presets_file(obj)
    return obj["vision_relay"]


def _safe_vision_relay(cfg):
    if not cfg:
        return None
    return {
        "enabled": bool(cfg.get("enabled")),
        "base_url": cfg.get("base_url"),
        "model": cfg.get("model"),
        "has_token": bool(cfg.get("token")),
    }


def _relay_ready(cfg):
    return bool(cfg and cfg.get("enabled") and cfg.get("base_url")
                and cfg.get("token") and cfg.get("model"))


def _vision_call_sync(cfg, img_paths, prompt):
    """识图 API 通用调用（阻塞版，外面用 to_thread 包）：prompt + 图片 → 文本。失败抛异常。"""
    content = [{"type": "text", "text": prompt}]
    for p in img_paths:
        ext = Path(p).suffix.lower()
        mime = IMG_MIME.get(ext, "image/jpeg")
        b64 = base64.b64encode(Path(p).read_bytes()).decode("ascii")
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"}})
    body = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['token']}"})
    with urllib.request.urlopen(req, timeout=VISION_RELAY_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    desc = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    if isinstance(desc, list):   # 少数实现回 content 块列表
        desc = "".join(b.get("text", "") for b in desc if isinstance(b, dict))
    desc = (desc or "").strip()
    if not desc:
        raise RuntimeError("识图模型返回了空描述")
    return desc[:VISION_DESC_MAX]


async def vision_describe(cfg, img_paths, context_text, user_text):
    """首次代看：带对话上下文的定向描述，让描述聚焦对话关心的内容。"""
    prompt = (
        "你是识图助手，正在帮一个看不到图片的 AI 角色\"看图\"。"
        "请用中文详细描述图片内容，让她仅凭你的文字就能自然地回应。\n"
        "要求：先概括图里是什么，再描述与对话相关的细节（文字请原样转录；"
        "人物注意外貌/表情/动作；截图注意界面与内容）。不要加评论和猜测意图。\n"
    )
    if context_text:
        prompt += f"\n[对话背景]\n{context_text}\n"
    if user_text:
        prompt += f"\n[用户发图时说]\n{user_text}\n"
    prompt += f"\n共 {len(img_paths)} 张图，逐张描述（多张时标注 图1/图2…）。"
    return await asyncio.to_thread(_vision_call_sync, cfg, img_paths, prompt)


async def vision_ask(cfg, img_paths, question):
    """追问：主模型（达妮娅）对同一批图片提出具体问题，识图助手针对性回答。"""
    prompt = (
        "你是识图助手。一个看不到图片的 AI 角色已读过图片的初步描述，"
        "现在她对图片有一个具体问题，请只针对问题作答，用中文，直接、具体。"
        "图里有相关文字请原样转录；图中没有答案就明说\"图里看不出来\"，不要编造。\n"
        f"\n[她的问题]\n{question}\n"
    )
    if len(img_paths) > 1:
        prompt += f"\n共 {len(img_paths)} 张图（图1/图2…按发送顺序编号）。"
    return await asyncio.to_thread(_vision_call_sync, cfg, img_paths, prompt)

# ═══════════════════════════════════════════════════════════════════
# 工具辅助
# ═══════════════════════════════════════════════════════════════════
def _valid_uploads(imgs):
    """只接受本后端颁发的 uploads 路径——防注入任意本地文件让模型 Read。"""
    out = []
    root = str(UPLOAD_DIR.resolve())
    for p in (imgs or [])[:UPLOAD_PER_MSG]:
        try:
            rp = str(Path(p).resolve())
        except Exception:
            continue
        if rp.startswith(root) and Path(rp).is_file():
            out.append(rp)
    return out


def _compress_upload(body, ext):
    """上传图片重编码：长边>UPLOAD_MAX_EDGE 则等比缩小；大 PNG/WebP 转 JPEG。
    目的不是防崩（缓冲区已调大），是省 token——大图 Read 进上下文按尺寸计费。
    返回 (body, ext)；Pillow 缺失或解码失败时原样返回。"""
    if ext == ".gif" or len(body) <= UPLOAD_COMPRESS_OVER:
        return body, ext
    try:
        from PIL import Image
    except ImportError:
        LOG.warning("Pillow 未安装，图片压缩跳过（pip install pillow）")
        return body, ext
    try:
        img = Image.open(io.BytesIO(body))
        img.load()
        w, h = img.size
        if max(w, h) > UPLOAD_MAX_EDGE:
            scale = UPLOAD_MAX_EDGE / max(w, h)
            img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        # 带透明通道压平到白底再转 JPEG（截图场景透明极少，体积优先）
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=UPLOAD_JPEG_QUALITY, optimize=True)
        out = buf.getvalue()
        if len(out) < len(body):
            LOG.info("图片压缩：%d → %d 字节（%.0f%%）",
                     len(body), len(out), 100 * len(out) / len(body))
            return out, ".jpg"
        return body, ext
    except Exception as e:
        LOG.warning("图片压缩失败，原样落盘：%s", e)
        return body, ext


def _cleanup_uploads():
    """清掉 UPLOAD_KEEP_DAYS 天前的上传缓存（启动时调用一次）。"""
    cutoff = time.time() - UPLOAD_KEEP_DAYS * 86400
    n = 0
    for f in UPLOAD_DIR.glob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except Exception:
            pass
    if n:
        LOG.info("上传缓存清理：删除 %d 个过期文件", n)
def _cmd_text(tool_name, input_data):
    """从工具输入里取出要执行的命令文本（兼容 Bash/PowerShell 不同字段）。"""
    inp = input_data or {}
    return inp.get("command") or inp.get("script") or inp.get("code") or ""


# 工具 → 角色化描述（前端弹窗展示用）
def humanize(tool_name, input_data):
    if tool_name.startswith("mcp__vision__"):
        q = (input_data or {}).get("question", "")
        return f"达妮娅想再仔细看看图片{('：' + q[:40]) if q else ''}"
    if tool_name in SHELL_TOOLS:
        cmd = _cmd_text(tool_name, input_data)
        if any(k in cmd for k in ("daemon", "browser", "9876", "playwright", "chrome")):
            return "达妮娅想打开浏览器上网看看"
        return "达妮娅想在系统里执行一条命令"
    if tool_name in ("WebFetch", "WebSearch"):
        return "达妮娅想上网查点东西"
    if tool_name in ("Agent", "Task"):
        return "达妮娅想让一个帮手去忙点事"
    return f"达妮娅想使用 {tool_name}"


class Session:
    """单会话状态：一个 WS 连接 + 一个按需建立/重建的 ClaudeSDKClient。"""
    def __init__(self, ws):
        self.ws = ws
        self.client = None          # 选预设前不建
        self.active_preset = None   # 当前 client 背后的预设
        self.pending_preset = None  # 已选但延迟到下次启动的预设（已有上下文时）
        self.skip_crawl = False     # 独立「跳过爬取」开关
        self.bypass = False         # bypass 模式：工具调用不再弹窗、全部放行
        self.has_context = False    # 仅在回合正常完成后置 True（interrupt/revert 不算）
        self.session_id = None      # 当前 CLI 会话 id（dispatch 里从消息更新；存档/续接依据）
        self.last_user_text = ""    # 最近一条原始用户文本（拼 /denia 前缀之前），做存档备注
        self.recent = []            # 最近对话片段（识图转接的上下文素材）
        self._seg_buf = ""          # 当前气泡段的文本累积（关段时抽 L2 存入 recent）
        self.relay_imgs = []        # 识图转接：当前批图片路径（ask_vision 追问对象）
        self.relay_asks = 0         # 当前批已追问次数（每批上限 VISION_ASKS_PER_MSG）
        self._started = False       # gate /denia 前缀（每个子进程一次）
        self._building = False      # build/teardown 重入锁
        self._pending = {}   # req_id -> asyncio.Future（等前端权限回复）
        self._req_seq = 0
        self._interrupted = False   # 本轮是否被用户打断
        # ---- 持续流模型（取代一问一答 run_turn）----
        self.consumer_task = None   # 长驻 receive_messages 消费循环 task
        self.outstanding = 0        # 未被主对话 ResultMessage 满足的 query 数（唯一解锁依据）
        self.seg_open = False       # 当前是否有一个达妮娅气泡段开着
        self.seg_id = 0             # 气泡段递增编号
        self.active_tasks = set()   # 后台 agent task_id（诊断+二级兜底）

    async def send(self, obj):
        await self.ws.send(json.dumps(obj, ensure_ascii=False))

    # ---- 识图追问工具（进程内 MCP）----
    def _make_vision_server(self):
        """ask_vision 工具：达妮娅对当前批图片追问细节，识图助手针对性回答。
        闭包持有 self —— 追问对象永远是本会话最近一批转接图片。"""
        if sdk_tool is None or create_sdk_mcp_server is None:
            return None

        @sdk_tool("ask_vision",
                  "向识图助手追问当前图片的细节。仅当用户刚发来图片、且初步描述"
                  "不足以回答你想知道的内容时使用。一次问一个具体问题"
                  "（如：图里的文字写了什么？她穿的是什么颜色的衣服？）。",
                  {"question": str})
        async def ask_vision(args):
            q = (args.get("question") or "").strip()
            cfg = load_vision_relay()
            if not self.relay_imgs:
                return {"content": [{"type": "text",
                        "text": "当前没有可追问的图片（这批图片可能已过期）。"}],
                        "is_error": True}
            if not _relay_ready(cfg):
                return {"content": [{"type": "text",
                        "text": "识图转接未配置或已关闭，无法追问。"}], "is_error": True}
            if self.relay_asks >= VISION_ASKS_PER_MSG:
                return {"content": [{"type": "text",
                        "text": f"这批图片的追问次数已用完（上限 {VISION_ASKS_PER_MSG} 次），"
                                f"请基于已有信息回应。"}], "is_error": True}
            if not q:
                return {"content": [{"type": "text",
                        "text": "问题为空，请提出一个具体问题。"}], "is_error": True}
            self.relay_asks += 1
            LOG.info("ask_vision（%d/%d）：%r", self.relay_asks, VISION_ASKS_PER_MSG, q[:60])
            try:
                await self.send({"type": "vision_ask", "q": q[:60]})
            except Exception:
                pass
            try:
                ans = await vision_ask(cfg, self.relay_imgs, q)
            except Exception as e:
                LOG.error("ask_vision 失败：%s", e)
                return {"content": [{"type": "text",
                        "text": f"识图助手这次没能回答（{type(e).__name__}），"
                                f"请基于已有信息回应。"}], "is_error": True}
            return {"content": [{"type": "text", "text": ans}]}

        return create_sdk_mcp_server("vision", tools=[ask_vision])

    # ---- client 生命周期：手动 connect/disconnect（替代 async with）----
    # ⚠️ 必须在 handle_ws 协程内调用，绝不放进 detached task（SDK 单上下文约束）。
    async def build_client(self, preset, resume=None):
        """用 preset 组 options 建新 client。provider 走 env、模型走 model。
        resume=session_id 时从该会话历史续接（不重发 /denia 前缀，上下文已在）。
        成功发 chat_enabled；失败发 preset_error 并保持 client=None。"""
        if self._building:
            return
        self._building = True
        try:
            if self.client is not None:
                await self.teardown_client()
            env = {}
            if preset.get("base_url"):
                env["ANTHROPIC_BASE_URL"] = preset["base_url"]
            if preset.get("token"):
                env["ANTHROPIC_AUTH_TOKEN"] = preset["token"]
            # 盲模型（vision=false）挂 ask_vision 追问工具（进程内 MCP，识图转接配套）
            mcp_servers = {}
            if not preset.get("vision", True):
                vision_srv = self._make_vision_server()
                if vision_srv is not None:
                    mcp_servers["vision"] = vision_srv
            options = ClaudeAgentOptions(
                cwd=str(PROJECT_ROOT),
                setting_sources=["project"],
                include_partial_messages=True,
                can_use_tool=self.can_use_tool,
                permission_mode="default",
                env=env or None,                      # 空 → 回落进程环境
                model=(preset.get("model") or None),  # 空 → CLI 默认模型
                max_buffer_size=32 * 1024 * 1024,     # 默认1MB会被大图/agent结果单条消息撑爆
                resume=resume,                        # 非空 → 续接该 session 历史
                mcp_servers=mcp_servers or {},
            )
            LOG.info("build_client：预设=%s base_url=%s model=%s resume=%s",
                     preset.get("name"), preset.get("base_url") or "(默认)",
                     preset.get("model") or "(默认)", resume or "(新会话)")
            client = ClaudeSDKClient(options=options)
            try:
                await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
            except Exception as e:
                LOG.error("build_client 连接失败：%s", e)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                await self.send({"type": "preset_error",
                                 "name": preset.get("name"),
                                 "msg": f"连接失败：{type(e).__name__}"})
                return
            self.client = client
            self.active_preset = preset
            # 续接：上下文已在会话历史里，跳过 /denia 前缀，直接可聊
            self._started = bool(resume)
            self.has_context = bool(resume)
            self.session_id = resume or None
            self.outstanding = 0
            self.seg_open = False
            self.active_tasks.clear()
            save_last_selected(preset.get("id"))
            # 启动长驻消费循环（与 client 同 event loop，满足 SDK 单上下文约束）
            self.consumer_task = asyncio.create_task(self.consume_loop())
            LOG.info("build_client 成功，发送 chat_enabled")
            await self.send({"type": "chat_enabled", "preset": preset.get("name"),
                             "vision": bool(preset.get("vision", True)),
                             "relay": _relay_ready(load_vision_relay())})
        finally:
            self._building = False

    async def teardown_client(self):
        """关掉当前 client：先停消费循环，再 disconnect。"""
        if self.client is None:
            return
        # 停消费循环：cancel + await（务必在 disconnect 之前，否则迭代器悬挂）
        if self.consumer_task and not self.consumer_task.done():
            self.consumer_task.cancel()
            try:
                await self.consumer_task
            except (asyncio.CancelledError, Exception):
                pass
        self.consumer_task = None
        try:
            await self.client.disconnect()
        except Exception as e:
            LOG.warning("teardown_client disconnect 异常：%s", e)
        self.client = None
        self.active_preset = None
        self.outstanding = 0
        self.seg_open = False
        self.active_tasks.clear()

    # ---- 导出对话记录为 Markdown ----
    async def export_transcript(self):
        """读当前会话 transcript，抽 user/assistant 文本 → markdown 存 out/exports/。
        成功发 transcript_exported{url,name,count}；失败发 export_error。"""
        if get_session_messages is None:
            await self.send({"type": "export_error", "msg": "当前 SDK 版本不支持导出"})
            return
        if not self.session_id:
            await self.send({"type": "export_error", "msg": "还没有对话可以导出"})
            return
        try:
            msgs = get_session_messages(self.session_id, directory=str(PROJECT_ROOT))
        except Exception as e:
            LOG.warning("get_session_messages 失败：%s", e)
            await self.send({"type": "export_error", "msg": f"读取失败：{type(e).__name__}"})
            return
        md, count = _render_transcript_md(msgs, self.session_id,
                                          (self.active_preset or {}).get("name"))
        if count == 0:
            await self.send({"type": "export_error", "msg": "这段会话还没有可导出的对话"})
            return
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        name = f"对话记录_{datetime.now():%Y%m%d_%H%M%S}.md"
        (EXPORT_DIR / name).write_text(md, encoding="utf-8")
        LOG.info("导出对话记录：%s（%d 条）", name, count)
        await self.send({"type": "transcript_exported",
                         "url": "/exports/" + name, "name": name, "count": count})

    # ---- 权限回调（核心）----
    async def can_use_tool(self, tool_name, input_data, context):
        if self.bypass:
            LOG.info("权限：%s bypass 模式，直接放行", tool_name)
            return PermissionResultAllow()
        if tool_name in SILENT_TOOLS or tool_name.startswith("mcp__vision__"):
            LOG.info("权限：%s 在白名单，静默放行", tool_name)
            return PermissionResultAllow()
        LOG.info("权限：%s 需用户确认，推前端弹窗", tool_name)
        # 非白名单：推前端弹窗，等用户点击
        self._req_seq += 1
        req_id = f"perm-{self._req_seq}"
        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self.send({
            "type": "permission_request",
            "req_id": req_id,
            "tool": tool_name,
            "desc": humanize(tool_name, input_data),
            "detail": _short(input_data),
        })
        try:
            allow = await asyncio.wait_for(fut, timeout=PERM_TIMEOUT)
        except asyncio.TimeoutError:
            allow = False
            await self.send({"type": "permission_timeout", "req_id": req_id})
        finally:
            self._pending.pop(req_id, None)
        if allow:
            return PermissionResultAllow()
        return PermissionResultDeny(message="用户拒绝了这次操作")

    def resolve_permission(self, req_id, allow):
        fut = self._pending.get(req_id)
        if fut and not fut.done():
            fut.set_result(bool(allow))

    # ---- ESC 打断：中断本轮，撤回（当没发生过）----
    async def interrupt_turn(self):
        self._interrupted = True
        # 解掉可能挂着的权限等待（否则 interrupt 期间卡在弹窗）
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(False)
        try:
            await self.client.interrupt()   # 通知 SDK 停止本回合
        except Exception as e:
            print(f"[interrupt] {type(e).__name__}: {e}")

    # ---- 气泡段辅助 ----
    async def _ensure_segment(self):
        """幂等地开一个达妮娅气泡段（首个 content_block_start / text_delta 时）。"""
        if not self.seg_open:
            self.seg_id += 1
            self.seg_open = True
            self._seg_buf = ""
            await self.send({"type": "segment_start", "seg": self.seg_id})

    async def _close_segment_if_open(self):
        if self.seg_open:
            self.seg_open = False
            await self.send({"type": "segment_end", "seg": self.seg_id})
            # 抽 L2 存入 recent（识图转接的上下文素材；L1 是内心独白不给识图模型看）
            t = self._seg_buf
            if "[L2]" in t:
                t = t.split("[L2]", 1)[1]
            elif "[L1]" in t:
                t = ""
            t = t.strip()
            if t:
                self._remember("达妮娅", t)
            self._seg_buf = ""

    def _remember(self, who, text):
        """滚动记录最近对话（识图上下文用），单条截断、只留最近 6 条。"""
        self.recent.append(f"{who}：{text[:200]}")
        del self.recent[:-6]

    async def _maybe_unlock(self):
        """所有待响应 query 都收尾 → 解锁输入框。"""
        if self.outstanding == 0:
            await self.send({"type": "input_unlock", "reason": "idle"})

    # ---- 长驻消费循环（取代一问一答 run_turn）----
    # 持续读 receive_messages，永不因单个 ResultMessage 退出。
    # 解锁的唯一真相是 outstanding 计数，不靠 ResultMessage 分类。
    async def consume_loop(self):
        LOG.info("consume_loop 启动")
        try:
            async for msg in self.client.receive_messages():
                await self.dispatch(msg)
        except asyncio.CancelledError:
            LOG.info("consume_loop 被取消")
            raise
        except Exception as e:
            LOG.error("consume_loop 异常：%s\n%s", e, traceback.format_exc())
            try:
                await self.send({"type": "error", "msg": f"{type(e).__name__}: {e}"})
            except Exception:
                pass
        finally:
            # 流意外结束仍卡着输入框 → 强制解锁（R3 兜底）
            await self._close_segment_if_open()
            if self.outstanding > 0:
                LOG.warning("consume_loop 结束但 outstanding=%d，强制解锁", self.outstanding)
                self.outstanding = 0
                await self.send({"type": "input_unlock", "reason": "stream_ended"})

    async def dispatch(self, msg):
        tn = type(msg).__name__

        # 0) 追踪 session_id —— 几乎每条消息都带；存档/续接的唯一依据
        sid = getattr(msg, "session_id", None)
        if sid:
            self.session_id = sid

        # 1) 后台 task 生命周期 —— 维护 active_tasks，不进气泡
        if isinstance(msg, TaskStartedMessage):
            tid = getattr(msg, "task_id", None)
            self.active_tasks.add(tid)
            LOG.info("后台 task 启动 %s active=%s", tid, len(self.active_tasks))
            await self.send({"type": "task_started",
                             "task_id": tid, "desc": getattr(msg, "description", "")})
            return
        if isinstance(msg, (TaskNotificationMessage, TaskUpdatedMessage)):
            status = getattr(msg, "status", None)
            if status is None:  # TaskUpdatedMessage 的终态可能在 patch 里
                patch = getattr(msg, "patch", None) or {}
                status = patch.get("status") if isinstance(patch, dict) else None
            if status in TERMINAL_TASK_STATUSES:
                tid = getattr(msg, "task_id", None)
                self.active_tasks.discard(tid)
                LOG.info("后台 task 结束 %s (%s) active=%s", tid, status, len(self.active_tasks))
                await self.send({"type": "task_done", "task_id": tid})
            return
        if isinstance(msg, TaskProgressMessage):
            return

        # 2) subagent 内部流（爬虫等）不进气泡
        if getattr(msg, "parent_tool_use_id", None) is not None:
            return

        # 3) 顶层达妮娅流式文本
        if isinstance(msg, StreamEvent):
            if self._interrupted:
                return
            ev = msg.event
            t = ev.get("type")
            if t == "content_block_start":
                await self._ensure_segment()
            elif t == "content_block_delta":
                d = ev.get("delta", {})
                if d.get("type") == "text_delta":
                    await self._ensure_segment()
                    self._seg_buf += d.get("text", "")
                    await self.send({"type": "text_delta",
                                     "seg": self.seg_id, "text": d.get("text", "")})
            return

        # 4) 顶层 AssistantMessage —— tool_use 冒泡 + 关段（气泡边界）
        if isinstance(msg, AssistantMessage):
            if self._interrupted:
                return
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    await self.send({
                        "type": "tool_use",
                        "tool": b.name,
                        "hint": humanize(b.name, getattr(b, "input", {})),
                    })
            await self._close_segment_if_open()
            return

        # 5) ResultMessage —— 段完成信号，但【不终止循环】
        if isinstance(msg, ResultMessage):
            await self._close_segment_if_open()
            cost = getattr(msg, "total_cost_usd", None)
            if self._interrupted:
                self._interrupted = False
                if self.outstanding > 0:
                    self.outstanding -= 1
                LOG.info("ResultMessage(interrupt) outstanding→%d", self.outstanding)
                await self.send({"type": "turn_reverted"})
                await self._maybe_unlock()
                return
            if self.outstanding > 0:
                # 有人在等 → 这是主对话回合收尾
                self.outstanding -= 1
                self.has_context = True
                LOG.info("ResultMessage(主) cost=%s outstanding→%d", cost, self.outstanding)
                # 自动兜底存档：每次主回合收尾更新 last_session，崩溃后可一键续
                try:
                    update_last_session(self.session_id, self.active_preset)
                except Exception as e:
                    LOG.warning("update_last_session 失败：%s", e)
                await self.send({"type": "segment_final", "cost": cost})
                await self._maybe_unlock()
            else:
                # 无人等 → 后台 agent 的 ResultMessage，吞掉不解锁（根治错位的关键）
                LOG.info("ResultMessage(后台，outstanding=0 active=%s) 吞掉不解锁",
                         len(self.active_tasks))
            return


def _short(obj):
    s = json.dumps(obj, ensure_ascii=False)
    return s[:200]


def _extract_text(content):
    """从 API message.content（str 或 block 列表）里抽纯文本；跳过 tool_use/tool_result/image。
    返回 (text, is_pure_tool)：is_pure_tool 表示这条只是工具回传，非真人/达妮娅发言。"""
    if isinstance(content, str):
        return content.strip(), False
    if not isinstance(content, list):
        return "", False
    parts, saw_tool, saw_other = [], False, False
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            t = (b.get("text") or "").strip()
            if t:
                parts.append(t)
        elif bt in ("tool_use", "tool_result"):
            saw_tool = True
        else:
            saw_other = True   # image 等
    text = "\n\n".join(parts)
    is_pure_tool = saw_tool and not parts and not saw_other
    return text, is_pure_tool


import re as _re

# 运行时注入的系统块（非真人发言）：整条命中即丢弃
_SYS_USER_MARKERS = (
    "[Request interrupted by user]",
    "<task-notification>", "<task-notification ",
    "<local-command-stdout>", "<command-name>",
    "<system-reminder>",
    # /compact 生成的摘要以"用户消息"形式注入 transcript，非真人发言
    "This session is being continued from a previous conversation",
    "Caveat: The messages below were generated by the user",
)
_CMD_ARGS_RE = _re.compile(r"<command-args>(.*?)</command-args>", _re.S)


def _clean_user_text(text):
    """洗掉发给模型时拼的系统前缀/附图提示/运行时注入块，还原真人可读发言。
    返回空串表示这条应从记录里剔除。"""
    t = text.strip()
    # slash 命令包裹：<command-name>/denia</command-name><command-args>真话</command-args>
    if t.startswith("<command-message>") or t.startswith("<command-name>"):
        m = _CMD_ARGS_RE.search(t)
        return (m.group(1).strip() if m else "")
    # 纯系统注入块（打断标记 / task 通知 / 系统提醒）→ 丢弃
    if any(mk in t for mk in _SYS_USER_MARKERS):
        return ""
    # 去掉首轮 /denia [NO_CRAWL] 前缀（skip_crawl 时首条被拼成 "/denia [NO_CRAWL]\n真话"）
    for pre in ("/denia [NO_CRAWL]", "/denia"):
        if t.startswith(pre):
            t = t[len(pre):].lstrip("\n ")
            break
    # 兜底：[NO_CRAWL] 若单独残留在行首也剥掉
    if t.startswith("[NO_CRAWL]"):
        t = t[len("[NO_CRAWL]"):].lstrip("\n ")
    # 去掉附图提示块（"（我发来了 N 张图片…）"）——它以换行分隔在末尾
    idx = t.find("（我发来了")
    if idx != -1:
        t = t[:idx].rstrip()
    return t.strip()


def _render_transcript_md(msgs, session_id, preset_name):
    """把 SessionMessage 列表渲成 markdown。返回 (markdown, 对话轮数)。
    L1（达妮娅在想…）折叠进 <details>，L2 作为正文。"""
    lines = [
        "# 达妮娅 · 对话记录", "",
        f"- 会话 ID：`{session_id}`",
        f"- 模型预设：{preset_name or '未知'}",
        f"- 导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        "", "---", "",
    ]
    count = 0
    for m in msgs:
        raw = getattr(m, "message", None) or {}
        content = raw.get("content") if isinstance(raw, dict) else None
        text, is_pure_tool = _extract_text(content)
        if is_pure_tool or not text:
            continue
        if m.type == "user":
            text = _clean_user_text(text)
            if not text or text.startswith("[L0"):   # 系统指令（如归档）不入记录
                continue
            lines.append(f"**你**：{text}")
            lines.append("")
            count += 1
        else:  # assistant
            l1, l2 = "", text
            if "[L2]" in text:
                head, l2 = text.split("[L2]", 1)
                l1 = head.split("[L1]", 1)[1] if "[L1]" in head else ""
            elif "[L1]" in text:
                l1 = text.split("[L1]", 1)[1]
                l2 = ""
            l1, l2 = l1.strip(), l2.strip()
            block = []
            # L1 内心独白：直接展开成引用块（> 前缀），排在 L2 之前，一眼看全
            if l1:
                quoted = "\n".join(f"> {ln}" if ln.strip() else ">" for ln in l1.splitlines())
                block.append(f"**达妮娅在想…**\n{quoted}")
            if l2:
                block.append(f"**达妮娅**：{l2}")
            if block:
                lines.append("\n\n".join(block))
                lines.append("")
                count += 1
    return "\n".join(lines), count


async def handle_ws(ws):
    LOG.info("WS 连接建立（预设未选，聊天禁用）")
    session = Session(ws)
    try:
        # 连上不建 client：先把 picker 状态推给前端，等用户选预设
        await session.send({"type": "connected"})
        await session.send({
            "type": "presets",
            "list": [_safe_preset(p) for p in load_presets()],
            "selected": load_last_selected(),
            "skip_crawl": session.skip_crawl,
        })
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                LOG.warning("收到非 JSON 消息，忽略：%r", raw[:120])
                continue
            t = data.get("type")
            LOG.info("← 前端消息 type=%s", t)

            if t == "chat":
                if session.client is None:
                    await session.send({"type": "need_preset"})
                    continue
                # 一次一问：上一条还没被回应，拒绝新 query（匹配 UX，也防 outstanding 堆积）
                if session.outstanding > 0:
                    LOG.info("chat 被拒：上一条仍在回应（outstanding=%d），回 busy",
                             session.outstanding)
                    await session.send({"type": "busy"})
                    continue
                text = (data.get("text") or "").strip()
                # 用户附图：只接受 uploads 缓存内的路径，拼成"请用 Read 查看"的提示
                imgs = _valid_uploads(data.get("images"))
                if not text and not imgs:
                    continue
                # 记原始文本做存档备注（[L0更新记忆] 等系统指令不覆盖，保留上一句真人发言）
                if text and not text.startswith("[L0"):
                    session.last_user_text = text
                    session._remember("用户", text)
                if imgs:
                    relay_cfg = load_vision_relay()
                    main_vision = bool((session.active_preset or {}).get("vision", True))
                    if not main_vision and _relay_ready(relay_cfg):
                        # 识图转接：主模型看不了图 → 识图模型带上下文"代看"，描述文字入消息
                        await session.send({"type": "vision_relay_start", "n": len(imgs)})
                        try:
                            desc = await vision_describe(
                                relay_cfg, imgs, "\n".join(session.recent[:-1]), text)
                            LOG.info("识图转接成功：%d 张图 → %d 字描述", len(imgs), len(desc))
                            # 新一批图片 → 追问对象更新、次数归零
                            session.relay_imgs = imgs
                            session.relay_asks = 0
                            hint = (f"（我发来了 {len(imgs)} 张图片。你看不到原图，"
                                    f"以下是识图助手代看后的描述：\n{desc}\n"
                                    f"如果描述不够回答我关心的内容，可用 ask_vision 工具"
                                    f"追问细节，最多 {VISION_ASKS_PER_MSG} 次。）")
                            await session.send({"type": "vision_relay_done"})
                        except Exception as e:
                            LOG.error("识图转接失败：%s\n%s", e, traceback.format_exc())
                            await session.send({"type": "vision_relay_error",
                                                "msg": f"{type(e).__name__}: {e}"})
                            hint = (f"（我发来了 {len(imgs)} 张图片，但识图转接失败了，"
                                    f"你看不到图片内容。请直接告诉我你看不到图。）")
                    else:
                        # 原生识图：模型经 Read 工具直接查看
                        lines = "\n".join(f"  - {p}" for p in imgs)
                        hint = (f"（我发来了 {len(imgs)} 张图片，缓存在本机这些路径，"
                                f"请用 Read 工具查看：\n{lines}）")
                    text = f"{text}\n\n{hint}" if text else hint
                # 首轮加载 /denia（skip_crawl 时注入 [NO_CRAWL] 阻止想法池爬取）
                if not session._started:
                    prefix = "/denia [NO_CRAWL]\n" if session.skip_crawl else "/denia\n"
                    text = prefix + text
                    session._started = True

                # 持续流模型：只投递 query，消费循环会自然产出一段或多段达妮娅发言
                session._interrupted = False
                session.outstanding += 1
                LOG.info("query 投递，长度=%d：%r outstanding→%d",
                         len(text), text[:60], session.outstanding)
                try:
                    await session.client.query(text)
                except Exception as e:
                    LOG.error("query 异常：%s", e)
                    session.outstanding -= 1
                    await session.send({"type": "error", "msg": f"{type(e).__name__}: {e}"})

            elif t == "interrupt":
                # ESC：打断当前回应（消费循环会在 ResultMessage 处发 turn_reverted）
                if session.outstanding > 0:
                    await session.interrupt_turn()

            elif t == "permission_response":
                session.resolve_permission(data.get("req_id"), data.get("allow"))

            elif t == "reset":
                # 清上下文；若有延迟预设，此时建之
                session._started = False
                session.has_context = False
                if session.pending_preset:
                    p = session.pending_preset
                    session.pending_preset = None
                    await session.build_client(p)

            # ---- 预设 / 开关管理 ----
            elif t == "list_presets":
                await session.send({
                    "type": "presets",
                    "list": [_safe_preset(p) for p in load_presets()],
                    "selected": load_last_selected(),
                    "skip_crawl": session.skip_crawl,
                })

            elif t == "select_preset":
                preset = find_preset(data.get("id"))
                if not preset:
                    await session.send({"type": "preset_error", "name": "", "msg": "预设不存在"})
                elif session.outstanding > 0:
                    # 正在回应中不切（双保险：前端本就 busy-disabled）
                    await session.send({"type": "preset_error",
                                        "name": preset.get("name"),
                                        "msg": "正在回应，稍后再切"})
                elif session.client is None or not session.has_context:
                    # 无上下文 → 立即生效（含首次选择）
                    await session.build_client(preset)
                else:
                    # 已有上下文 → 存起来下次启动生效，不拆活 client
                    session.pending_preset = preset
                    save_last_selected(preset.get("id"))
                    await session.send({"type": "preset_deferred", "preset": preset.get("name")})

            elif t == "save_preset":
                saved = upsert_preset(data.get("preset") or {})
                LOG.info("save_preset：%s", saved.get("name"))  # 不记 token
                await session.send({
                    "type": "presets",
                    "list": [_safe_preset(p) for p in load_presets()],
                    "selected": load_last_selected(),
                    "skip_crawl": session.skip_crawl,
                })

            elif t == "delete_preset":
                pid = data.get("id")
                delete_preset(pid)
                # 清指向被删预设的引用（不拆活 client：会话可继续）
                if session.active_preset and session.active_preset.get("id") == pid:
                    session.active_preset = None
                if session.pending_preset and session.pending_preset.get("id") == pid:
                    session.pending_preset = None
                await session.send({
                    "type": "presets",
                    "list": [_safe_preset(p) for p in load_presets()],
                    "selected": load_last_selected(),
                    "skip_crawl": session.skip_crawl,
                })

            elif t == "get_vision_relay":
                await session.send({"type": "vision_relay",
                                    "cfg": _safe_vision_relay(load_vision_relay())})

            elif t == "save_vision_relay":
                saved = save_vision_relay(data.get("cfg") or {})
                LOG.info("save_vision_relay：enabled=%s model=%s",
                         saved.get("enabled"), saved.get("model"))  # 不记 token
                await session.send({"type": "vision_relay",
                                    "cfg": _safe_vision_relay(saved), "saved": True})

            elif t == "set_skip_crawl":
                session.skip_crawl = bool(data.get("on"))
                LOG.info("set_skip_crawl=%s", session.skip_crawl)
                await session.send({"type": "skip_crawl_set", "on": session.skip_crawl})

            elif t == "set_bypass":
                # bypass 模式：会话级开关（每 WS 连接默认关，不重建成 client 即时生效）。
                # 只跳过弹窗确认，无其他副作用（会话级开关，即时生效）。
                session.bypass = bool(data.get("on"))
                LOG.info("set_bypass=%s", session.bypass)
                await session.send({"type": "bypass_set", "on": session.bypass})

            # ---- 存档 / 从存档续接 ----
            elif t == "list_checkpoints":
                obj = _read_checkpoints_file()
                await session.send({
                    "type": "checkpoints",
                    "list": [_checkpoint_view(c) for c in obj["checkpoints"]],
                    "last_session": _checkpoint_view(
                        {**obj["last_session"], "hint": "上次会话（自动记录）"}
                        if obj.get("last_session") else None),
                })

            elif t == "save_checkpoint":
                if not session.session_id:
                    await session.send({"type": "checkpoint_error",
                                        "msg": "还没开始对话，没有可存的进度"})
                elif session.outstanding > 0:
                    await session.send({"type": "checkpoint_error",
                                        "msg": "达妮娅正在回应，等她说完再存档"})
                else:
                    obj = save_checkpoint_record(
                        session.session_id, session.last_user_text, session.active_preset)
                    LOG.info("save_checkpoint：session=%s hint=%r",
                             session.session_id, session.last_user_text[:20])
                    await session.send({
                        "type": "checkpoint_saved",
                        "list": [_checkpoint_view(c) for c in obj["checkpoints"]],
                    })

            elif t == "resume_checkpoint":
                cp = find_checkpoint(data.get("id"))
                if not cp or not cp.get("session_id"):
                    await session.send({"type": "checkpoint_error", "msg": "存档不存在"})
                elif session.outstanding > 0:
                    await session.send({"type": "checkpoint_error",
                                        "msg": "达妮娅正在回应，等她说完再切换"})
                elif not _session_jsonl_exists(cp.get("session_id")):
                    await session.send({"type": "checkpoint_error",
                                        "msg": "这条存档的会话记录已不在，无法续接"})
                elif not cp.get("preset"):
                    await session.send({"type": "checkpoint_error",
                                        "msg": "存档缺少模型信息，无法续接"})
                else:
                    await session.build_client(cp["preset"], resume=cp["session_id"])
                    if session.client is not None:   # build 成功（失败时已发 preset_error）
                        await session.send({"type": "resumed", "hint": cp.get("hint") or ""})

            elif t == "delete_checkpoint":
                obj = delete_checkpoint_record(data.get("id"))
                await session.send({
                    "type": "checkpoints",
                    "list": [_checkpoint_view(c) for c in obj["checkpoints"]],
                    "last_session": _checkpoint_view(
                        {**obj["last_session"], "hint": "上次会话（自动记录）"}
                        if obj.get("last_session") else None),
                })

            # ---- 导出对话记录为 Markdown ----
            elif t == "export_transcript":
                await session.export_transcript()

    except websockets.ConnectionClosed:
        LOG.info("WS 连接关闭")
    except Exception as e:
        LOG.error("handle_ws 异常：%s\n%s", e, traceback.format_exc())
    finally:
        # 替代原 async with 的 __aexit__：断开时务必关子进程，否则泄漏
        if session.client is not None:
            LOG.info("WS 收尾，teardown client")
            await session.teardown_client()


# ---- 静态页面：独立 stdlib HTTP 线程（与 WS 端口分离）----
class _HtmlHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = INDEX.read_text(encoding="utf-8").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/avatar.png", "/avatar"):
            # 达妮娅头像（顶栏 / 侧边栏 / 消息头像共用）
            img = HERE / "avatar.png"
            if img.exists():
                body = img.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path.startswith("/stickers/"):
            # 表情包静态路由：只服务 STICKER_DIR 下的纯文件名（防目录穿越）
            name = unquote(self.path[len("/stickers/"):])
            img = STICKER_DIR / name
            if (name and name == os.path.basename(name) and img.is_file()
                    and img.suffix.lower() in IMG_MIME):
                body = img.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", IMG_MIME[img.suffix.lower()])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path.startswith("/uploads/"):
            # 用户上传图片回显（聊天历史里的缩略图），同样只服务纯文件名
            name = unquote(self.path[len("/uploads/"):])
            img = UPLOAD_DIR / name
            if (name and name == os.path.basename(name) and img.is_file()
                    and img.suffix.lower() in IMG_MIME):
                body = img.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", IMG_MIME[img.suffix.lower()])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path.startswith("/exports/"):
            # 导出的对话记录 markdown 下载（只服务 EXPORT_DIR 下纯文件名的 .md）
            name = unquote(self.path[len("/exports/"):])
            f = EXPORT_DIR / name
            if (name and name == os.path.basename(name) and f.is_file()
                    and f.suffix.lower() == ".md"):
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                # 触发浏览器下载而非内联显示
                dl = quote(name)
                self.send_header("Content-Disposition",
                                 f"attachment; filename*=UTF-8''{dl}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        """POST /upload —— 用户图片上传（＋按钮 / 拖拽 / 粘贴共用）。
        原始字节流 + X-Filename 头（URL 编码，仅取扩展名），落盘 UPLOAD_DIR。
        返回 {name, path, url}；path 随 chat 消息带给模型 Read。"""
        if self.path != "/upload":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > UPLOAD_MAX:
            self.send_response(413)
            self.end_headers()
            return
        raw_name = unquote(self.headers.get("X-Filename") or "img.png")
        ext = os.path.splitext(os.path.basename(raw_name))[1].lower()
        if ext not in IMG_MIME:
            ext = ".png"   # 粘贴板等无文件名来源统一按 png 存
        body = self.rfile.read(length)
        body, ext = _compress_upload(body, ext)
        name = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}{ext}"
        f = UPLOAD_DIR / name
        f.write_bytes(body)
        LOG.info("图片上传：%s（%d 字节）", name, len(body))
        resp = json.dumps({"ok": True, "name": name, "path": str(f),
                           "url": "/uploads/" + name},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def _serve_http():
    ThreadingHTTPServer((HOST, HTTP_PORT), _HtmlHandler).serve_forever()


async def main():
    if not INDEX.exists():
        sys.exit(f"[ERR] index.html 不存在：{INDEX}")
    _cleanup_uploads()
    # HTTP 静态页面跑在后台线程
    threading.Thread(target=_serve_http, daemon=True).start()
    print(f"[*] 达妮娅 GUI 后端（Agent SDK / 正式版）")
    print(f"[*] cwd       : {PROJECT_ROOT}（真实 .claude，与终端 /denia 同一套记忆）")
    print(f"[*] provider  : 由前端预设注入（回落进程环境 {os.environ.get('ANTHROPIC_BASE_URL', '默认')}）")
    print(f"[*] 预设文件  : {PRESETS_FILE}")
    print(f"[*] 打开浏览器: http://{HOST}:{HTTP_PORT}")
    print(f"[*] WebSocket : ws://{HOST}:{WS_PORT}/ws")
    print(f"[*] 日志文件  : {LOG_FILE}")
    print(f"[*] Ctrl+C 停止\n")
    LOG.info("后端启动完成，等待前端连接")
    async with websockets.serve(handle_ws, HOST, WS_PORT):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] 已停止")

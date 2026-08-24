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
import hashlib
import io
import json
import os
import random
import re
import sys
import time
import urllib.request
import uuid
import wave
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
from urllib.parse import unquote, quote, urlsplit, parse_qs
import hmac
import socket as _socket

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
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
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
STICKER_DIR = PROJECT_ROOT / "denia" / "共享" / "工具" / "表情包"
IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp"}
# 用户上传图片缓存：上传走 HTTP POST 落盘，模型经 Read 工具查看（native 识图）
UPLOAD_DIR = HERE / "out" / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
# 生图产物：达妮娅 [生图/改图/打卡] 标记 → 后端 worker 跑 gen.py 子进程落盘，
# 前端经 /genimg/ 路由取图；成本闸状态 .state.json 也在这目录（gen.py 自管）
GENIMG_DIR = HERE / "out" / "genimg"
GENIMG_DIR.mkdir(exist_ok=True)
GENIMG_SCRIPT = Path(os.environ.get("DENIA_GENIMG_SCRIPT")
                     or (PROJECT_ROOT / "tools" / "生图" / "gen.py"))
# （冒烟用 DENIA_GENIMG_SCRIPT 换假桩——QQ 桥冒烟的 [生图:] 也会打到这个 worker，
#  只换桥侧 genimg_script 的话这里照样烧真 API，2026-08-19 一晚实烧 8 张才发现）
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
HOST = "127.0.0.1"   # 默认只听回环；presets.json server.host 或环境变量 DENIA_GUI_HOST 可开 0.0.0.0（局域网）
HTTP_PORT = 8765   # 静态页面
WS_PORT = 8766     # WebSocket
# 服务器访问配置：main() 启动时从 presets.json server 节解析（env 覆盖）。
# access_token 非空即开口令闸——WS 首连校验 ?key=，HTTP 校验 cookie/query。
_SERVER = {"host": HOST, "access_token": ""}

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

# ═══════════════════════════════════════════════════════════════════
# 共读模式（denia-read）
# ───────────────────────────────────────────────────────────────────
# 与主聊天隔离的第二个 SDK 会话：同一 WS 连接上挂两个 Session 实例
# （chat + read），消息按 mode 字段路由。书籍资产在 denia/私有/共读/<书名>/。
# 出向消息由 Session.send 打 mode 标，前端按此路由到对应聊天流。
# ═══════════════════════════════════════════════════════════════════
READ_DIR = PROJECT_ROOT / "denia" / "私有" / "共读"
# 共读专属入向消息类型（强制路由到 read 会话，无需前端带 mode 字段）
READ_MSG_TYPES = {"enter_read", "exit_read", "select_book", "read_highlight",
                  "page_ping", "get_notes", "append_note", "organize_notes",
                  "set_coread_config"}
# presets.json 顶层 coread 节的默认值
COREAD_DEFAULTS = {"clock_enabled": True, "clock_mean_min": 20,
                   "idle_gate_min": 10, "last_book": None}


def load_coread_config():
    """共读配置（时钟参数 + 上次读的书），缺失字段补默认值。"""
    cfg = _read_presets_file().get("coread")
    out = dict(COREAD_DEFAULTS)
    if isinstance(cfg, dict):
        out.update({k: v for k, v in cfg.items() if k in COREAD_DEFAULTS})
    return out


def save_coread_config(patch):
    """合并写 coread 节（只接受已知键），返回落盘后的完整 coread。"""
    obj = _read_presets_file()
    cur = dict(COREAD_DEFAULTS)
    if isinstance(obj.get("coread"), dict):
        cur.update(obj["coread"])
    for k, v in (patch or {}).items():
        if k in COREAD_DEFAULTS:
            cur[k] = v
    obj["coread"] = cur
    _write_presets_file(obj)
    return cur


# presets.json 顶层 web 节：上网（想法池爬取）配置。
# skip_crawl 曾是不持久化的 Session 属性，2026-08-07 起落盘；
# model = 爬虫子 agent（browser-operator）指定模型，"" = 跟随主模型，
# 仅主会话走 LiteLLM 桥时可路由（子 agent 继承主会话 env，别名由桥解析）。
WEB_DEFAULTS = {"skip_crawl": False, "model": ""}


def load_web():
    cfg = _read_presets_file().get("web")
    out = dict(WEB_DEFAULTS)
    if isinstance(cfg, dict):
        out.update({k: v for k, v in cfg.items() if k in WEB_DEFAULTS})
    return out


def save_web(patch):
    """合并写 web 节（只接受已知键），返回落盘后的完整 web。"""
    obj = _read_presets_file()
    cur = dict(WEB_DEFAULTS)
    if isinstance(obj.get("web"), dict):
        cur.update(obj["web"])
    for k, v in (patch or {}).items():
        if k in WEB_DEFAULTS:
            cur[k] = v
    obj["web"] = cur
    _write_presets_file(obj)
    return cur


# ═══════════════════════════════════════════════════════════════════
# QQ 桥接（denia-qq 公开分身，最小私聊验证）
# ───────────────────────────────────────────────────────────────────
# 第三个会话槽 mode="qq"：tools/qq-bridge/bridge.py 作为 WS 客户端连入
# （?client=qq），走标准 chat 帧协议，零抽象复用 Session/存档/流式段。
# 共享灵魂只读 + 缓冲读写靠 can_use_tool 的 qq 自动裁决落地（无人值守，
# 绝不弹窗）；napcat_ws_* 等连接参数由桥进程自己读本节，server 侧只用
# enabled/preset_id。
# ═══════════════════════════════════════════════════════════════════
QQ_MSG_TYPES = {"qq_init", "qq_vision", "qq_voice"}   # 桥专属入向类型（强制路由到 qq 会话）
QQ_DEFAULTS = {
    "enabled": False,          # 桥启动闸：False 时 bridge.py 拒启动
    "preset_id": "",           # qq 会话用的预设 id；空 = 跟随 lastSelected
    "self_id": 0,              # 机器人小号 QQ 号（桥校验连入的 NapCat 没连错）
    "allow_from": [],          # 允许私聊的 QQ 号白名单（桥侧过滤）
    "debounce_sec": 6,         # 消息防抖合并窗口（桥侧）
    "napcat_ws_host": "127.0.0.1",
    "napcat_ws_port": 8790,    # OneBot 反向 WS 监听（≠8765 HTTP / ≠8766 WS）
    "napcat_token": "",        # NapCat 端 accessToken 握手校验
    "server_ws": "ws://127.0.0.1:8766/ws?client=qq",
    "reply_gap_ms": 800,       # 分段回复条间间隔（拟打字节奏+防频控）
    "max_reply_chars": 500,    # 单条 QQ 消息长度上限（句读处切分）
    "channel": "napcat",       # napcat | official（官方 bot 通道，桥侧消费）
    "napcat_groups": [],       # napcat 通道群白名单（群号，空=不接群）
    "napcat_proactive_min": 0, # 主动说话间隔分钟（0=关闭，napcat 通道限定）
    "napcat_proactive_jitter_min": 30,  # 主动说话随机抖动上限（拟人不规律）
    "napcat_quiet_hours": [0, 8],       # 夜间静默时段（本地小时，前闭后开）
    "napcat_backfill_count": 50,        # NapCat 连入时断档补采条数（0=关）
    "napcat_backfill_delay_sec": 30,    # 连入后等离线消息同步再拉历史
    # —— 泊松插话（tools/qq-bridge/poisson.py，桥侧消费；base<=0 回落旧定时器）——
    "poisson_base_per_hour": 0.3,  # λ 基数（次/小时）：无热度时的底噪
    "poisson_alpha": 4.0,          # 热度系数：λ = base × (1 + α·heat)
    "poisson_halflife_min": 45,    # 热度 EMA 半衰期（分钟）
    "poisson_cooldown_min": 25,    # 中签后冷却（分钟）
    "poisson_daily_cap": 6,        # 每日中签上限（自然日重置）
    "poisson_min_heat": 1.0,       # 最低热度：单条孤消息不足以叫她开口
    "engage_window_min": 8,        # 会话态：群里无人说话超 N 分钟退出
    "engage_silent_quit": 3,       # 会话态：连续 N 次[静默]失趣退出
    "engage_max_min": 45,          # 会话态硬顶（防热聊群钉死她一整晚）
    "sticker_dir": "denia/共享/工具/表情包",  # 与 GUI 主聊天同一表情库
    "sticker_max_per_reply": 2,    # 单段最多发几个表情（防刷屏）
    # —— 四通道识图（tools/qq-bridge/vision_gate.py 消费；napcat 限定）——
    "vision_qq_enabled": True,     # False=回到"看不到图"旧占位
    "vision_small_kb": 60,         # 小图（表情）判定：小于 N KB
    "vision_small_px": 400,        # 或长边 ≤ N px
    "vision_glance_sync_sec": 5,   # 触发她的消息同步等略读上限（秒）
    "vision_glance_per_group_min": 6,   # 每群每分钟略读上限（轰炸闸）
    "vision_glance_daily_cap": 300,     # 每日略读上限
    "vision_look_daily_cap": 30,        # 每日细看上限（好模型要钱）
    "vision_cooldown_min": 5,      # API 429/529 后无视通道冷却（分钟）
    "vision_slots": 5,             # 每会话图槽数（[看图:N] 回溯）
    "vision_slot_ttl_min": 10,     # 图槽过期（分钟）
    "vision_cache_max": 500,       # 表情包描述缓存 LRU 条数
    # —— 生图（[生图:]/[改图:]/[打卡:] → gen.py 子进程；桥侧执行，此处仅三处同步）——
    "genimg_allow_from": [],       # 生图权限白名单（QQ号；空=谁都不许按快门）
    "genimg_script": "tools/生图/gen.py",  # 生图脚本（相对仓库根；冒烟换桩）
    "genimg_max_per_reply": 1,     # 单段最多生成几张（防连拍刷屏+烧钱）
    "voice_allow_from": [],        # 语音权限白名单（QQ号；空=谁都不许她开口）
    "voice_daily_cap": 50,         # 每日语音上限（本地 TTS 不计费，宽松；桥侧计数）
    "voice_max_chars": 200,        # 单条语音正文上限
    "link_allow_from": [],         # 甩链接白名单（QQ号；空=不注入链接素材）
    "link_daily_cap": 2,           # 每日带链上限（桥侧计数）
    "link_poisson_enabled": False, # True=泊松插话也可能甩链接（Beta 期先关）
    "official_appid": "",      # 官方 bot AppID（bot.q.qq.com 创建应用获得）
    "official_secret": "",     # 官方 bot AppSecret（clientSecret）
    "official_sandbox": False, # 沙箱环境（2026-01 起沙箱无群聊，C2C 可测）
    "official_allow_from": [], # 官方通道白名单是 openid 字符串（≠QQ 号）
    "official_allow_groups": [],  # 官方通道群白名单（group_openid，空=所有群）
    "official_proactive": False,  # 主动消息权限批下来才拨 True
    "official_api_base": "",   # 测试覆盖用（假官方服务器），留空走真实地址
    "official_token_url": "",
    # —— 群聊眼睛（tools/qq-bridge/eyes/ 采集器消费；桥读 eyes_group_map）——
    "eyes_enabled": False,     # 采集器启动闸
    "eyes_db_path": "",        # nt_msg.db 全路径（小号数据目录下）
    "eyes_db_key": "",         # x_key_scanner 取的 16 字节密钥
    "eyes_groups": [],         # 采集群白名单（按群隔离，空=不采）
    "eyes_interval_min": 20,   # 轮询间隔
    "eyes_group_map": {},      # official group_openid → 群号（@时找 log）
    "group_context_lines": 20, # 群@时注入最近群聊行数（0=不注入）
    # —— 控制中心行为开关（/qq 页 qq_set_modes 写；桥热重载活读）——
    "qq_proactive_enabled": True,  # False=泊松/定时主动说话整体停摆
    "qq_dnd": False,           # True=免打扰：群里只应@，私聊照常
    "qq_web_enabled": False,   # True=分身可 spawn 浏览器 agent 上网（裁决表活读）
}


def load_qq():
    """qq 节配置，缺失字段补默认值。节整个缺失时 enabled=False（拒启动）。"""
    cfg = _read_presets_file().get("qq")
    out = dict(QQ_DEFAULTS)
    if isinstance(cfg, dict):
        out.update({k: v for k, v in cfg.items() if k in QQ_DEFAULTS})
    return out


def save_qq(patch):
    """合并写 qq 节（只接受已知键），返回落盘后的完整 qq。"""
    obj = _read_presets_file()
    cur = dict(QQ_DEFAULTS)
    if isinstance(obj.get("qq"), dict):
        cur.update(obj["qq"])
    for k, v in (patch or {}).items():
        if k in QQ_DEFAULTS:
            cur[k] = v
    obj["qq"] = cur
    _write_presets_file(obj)
    return cur


# 控制中心（/qq 页）要够到桥那条 WS 连接上的 qq_session，但 Session 是
# per-WS-connection 的（控制台自己连进来只拿到新会话）——所以桥连入时在
# 这里登记，断开注销；qq_set_model 直达、qq_inject 推送都走这个注册表。
QQ_STATE = {"session": None, "ws": None}


# ---- 书籍资产访问（共读 HTTP 路由用）----

def _resolve_book_dir(name):
    """书名 → denia/私有/共读/<书名> 目录；不存在或越界（.. 等）返回 None。"""
    if not name:
        return None
    try:
        d = (READ_DIR / name).resolve()
    except Exception:
        return None
    if d.is_dir() and d.parent == READ_DIR.resolve():
        return d
    return None


def _list_books():
    """共读书单：[{name, current}]。current 来自各书 进度.json（读失败给空）。"""
    out = []
    if READ_DIR.is_dir():
        for d in sorted(READ_DIR.iterdir()):
            if not d.is_dir() or not (d / "进度.json").is_file():
                continue
            cur = {}
            try:
                data = json.loads((d / "进度.json").read_text(encoding="utf-8"))
                cur = data.get("current") or {}
            except Exception:
                pass
            out.append({"name": d.name, "current": cur})
    return out


_TOC_LINE_RE = None  # 惰性编译（re 在文件后部才 import）


def _book_toc(book_dir):
    """解析 目录.md → [{file,title,p_start,p_end}]；解析失败回退直接列 正文/ 目录。"""
    global _TOC_LINE_RE
    if _TOC_LINE_RE is None:
        import re
        _TOC_LINE_RE = re.compile(r"正文/([^`]+?\.md)`（P(\d+)[–-]P(\d+)）")
    items = []
    toc_file = book_dir / "目录.md"
    if toc_file.is_file():
        for line in toc_file.read_text(encoding="utf-8").splitlines():
            m = _TOC_LINE_RE.search(line)
            if m:
                title = line.split("→")[0].strip().lstrip("- ").strip()
                items.append({"file": m.group(1), "title": title,
                              "p_start": int(m.group(2)), "p_end": int(m.group(3))})
    if not items:
        body = book_dir / "正文"
        if body.is_dir():
            for f in sorted(body.glob("*.md")):
                items.append({"file": f.name, "title": f.stem,
                              "p_start": None, "p_end": None})
    return items


def _read_checkpoints_file():
    """读 checkpoints.json，返回 {checkpoints, last_session, last_read_session,
    last_qq_session}。缺失/损坏回默认。"""
    try:
        data = json.loads(CHECKPOINTS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "checkpoints": data.get("checkpoints", []) or [],
                "last_session": data.get("last_session"),
                "last_read_session": data.get("last_read_session"),
                "last_qq_session": data.get("last_qq_session"),
            }
    except FileNotFoundError:
        pass
    except Exception as e:
        LOG.warning("checkpoints.json 读取失败（按空处理）：%s", e)
    return {"checkpoints": [], "last_session": None, "last_read_session": None,
            "last_qq_session": None}


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
        "mode": cp.get("mode", "chat"),   # 共读存档只能续进共读会话
        "auto": bool(cp.get("auto")),     # 断线兜底自动档（前端打"自动"标）
        "exists": _session_jsonl_exists(cp.get("session_id")),
    }


AUTO_CHECKPOINT_KEEP = 10   # 断线自动存档滚动保留条数（手动存档永不清）


def save_checkpoint_record(session_id, hint, preset, cp_id=None, mode="chat",
                           auto=False):
    """新增/更新一条存档，返回落盘后的完整列表 obj。cp_id 为 'last' 或 None 时新增。
    auto=True（WS 断开兜底）：同 session 只留一条（原地刷新，反复断线不刷屏），
    全局滚动限量，只清自动档，手动存档永不动。"""
    obj = _read_checkpoints_file()
    rec = {
        "id": cp_id if (cp_id and cp_id != "last") else uuid.uuid4().hex[:8],
        "session_id": session_id,
        "created_at": f"{datetime.now():%Y-%m-%d %H:%M}",
        "hint": (hint or "")[:40],
        "preset": _preset_snapshot(preset),
        "mode": mode,
    }
    if auto:
        rec["auto"] = True
        for c in obj["checkpoints"]:        # 同会话已有自动档 → 拿它的 id 原地刷新
            if c.get("auto") and c.get("session_id") == session_id:
                rec["id"] = c["id"]
                obj["checkpoints"].remove(c)
                break
    obj["checkpoints"].insert(0, rec)   # 新的在前
    if auto:                            # 滚动清理：只删超出限量的自动档
        seen, kept = 0, []
        for c in obj["checkpoints"]:
            if c.get("auto"):
                seen += 1
                if seen > AUTO_CHECKPOINT_KEEP:
                    continue
            kept.append(c)
        obj["checkpoints"] = kept
    _write_checkpoints_file(obj)
    return obj


def update_last_session(session_id, preset, mode="chat"):
    """自动兜底项：每次主回合收尾更新。崩溃后可一键续（最多丢正在生成的那轮）。
    共读/QQ 会话单独存 last_read_session / last_qq_session，互不覆盖。"""
    if not session_id:
        return
    obj = _read_checkpoints_file()
    key = _last_session_key(mode)
    obj[key] = {
        "session_id": session_id,
        "created_at": f"{datetime.now():%Y-%m-%d %H:%M}",
        "preset": _preset_snapshot(preset),
        "mode": mode,
    }
    _write_checkpoints_file(obj)


def delete_checkpoint_record(cp_id):
    obj = _read_checkpoints_file()
    obj["checkpoints"] = [c for c in obj["checkpoints"] if c.get("id") != cp_id]
    _write_checkpoints_file(obj)
    return obj


def _last_session_key(mode):
    """mode → checkpoints.json 的兜底槽 key。"""
    if mode == "read":
        return "last_read_session"
    if mode == "qq":
        return "last_qq_session"
    return "last_session"


def find_checkpoint(cp_id, mode="chat"):
    obj = _read_checkpoints_file()
    if cp_id == "last":
        return obj.get(_last_session_key(mode))
    for c in obj["checkpoints"]:
        if c.get("id") == cp_id:
            return c
    return None


def _read_presets_file():
    """读整个 presets.json。
    返回 {presets, lastSelected, vision_relay, vision_presets, coread, genimg, web, tts, server}。
    损坏/缺失回默认。"""
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            vr = data.get("vision_relay")
            vg = data.get("vision_glance")
            vp = data.get("vision_presets")
            cr = data.get("coread")
            gi = data.get("genimg")
            wb = data.get("web")
            tt = data.get("tts")
            sv = data.get("server")
            qq = data.get("qq")
            return {
                "presets": data.get("presets", []) or [],
                "lastSelected": data.get("lastSelected"),
                "vision_relay": vr if isinstance(vr, dict) else None,
                "vision_glance": vg if isinstance(vg, dict) else None,
                "vision_presets": vp if isinstance(vp, list) else [],
                "coread": cr if isinstance(cr, dict) else None,
                "genimg": gi if isinstance(gi, dict) else None,
                "web": wb if isinstance(wb, dict) else None,
                "tts": tt if isinstance(tt, dict) else None,
                "server": sv if isinstance(sv, dict) else None,
                "qq": qq if isinstance(qq, dict) else None,
                # tts_<backend> 子配置节透传：新 backend 零改动（白名单只放节名前缀）
                **{k: v for k, v in data.items()
                   if k.startswith("tts_") and isinstance(v, dict)},
            }
    except FileNotFoundError:
        pass
    except Exception as e:
        LOG.warning("presets.json 读取失败（按空处理）：%s", e)
    return {"presets": [], "lastSelected": None, "vision_relay": None,
            "vision_glance": None,
            "vision_presets": [], "coread": None, "genimg": None, "web": None,
            "tts": None, "server": None, "qq": None}


def resolve_server_config():
    """启动时解析 server 节 → 全局 _SERVER。env 覆盖 presets：
    DENIA_GUI_HOST / DENIA_GUI_KEY。access_token 空 = 口令闸关（老行为）。"""
    cfg = _read_presets_file().get("server") or {}
    host = (os.environ.get("DENIA_GUI_HOST") or cfg.get("host") or "").strip()
    token = (os.environ.get("DENIA_GUI_KEY") or cfg.get("access_token") or "").strip()
    _SERVER["host"] = host or HOST
    _SERVER["access_token"] = token
    return _SERVER


def _gui_token():
    return _SERVER.get("access_token") or ""


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
# LiteLLM 本地桥（GUI 全托管）
# ───────────────────────────────────────────────────────────────────
# 选中指向 127.0.0.1:4000 的预设时，本后端负责把 LiteLLM 代理拉起来
# （取代手动跑 启动-LiteLLM.bat）。owned=True 仅当进程是本后端 spawn 的——
# 外部已在跑的实例绝不 kill（退出钩子、stop 按钮一视同仁）。
# 别名 CRUD 直接改 本地转接/LiteLLM/config.yaml 的 model_list 块，用"区间
# 拼接"而非 yaml 全量回写，保住 general_settings/litellm_settings 和注释。
# 全文件强制纯 ASCII（litellm 在 Windows 按 GBK 解码，混入中文启动即崩）。
# ⚠️ 本节函数都是模块级，别插进 class Session 里（见 _preset_sdk_env 警告）。
# ═══════════════════════════════════════════════════════════════════
LITELLM_DIR = PROJECT_ROOT / "本地转接" / "LiteLLM"
LITELLM_EXE = LITELLM_DIR / "venv" / "Scripts" / "litellm.exe"
LITELLM_CONFIG = LITELLM_DIR / "config.yaml"
LITELLM_LOG = LITELLM_DIR / "logs-gui.txt"
LITELLM_HOST, LITELLM_PORT = "127.0.0.1", 4000
LITELLM_START_TIMEOUT = 45  # 秒；代理就绪等待上限

# 单用户 GUI 只有一个代理实例，模块级状态即可
LITELLM = {"proc": None, "owned": False, "log_fh": None, "starting": False}


def _is_litellm_preset(preset):
    """预设是否走 LiteLLM 本地桥：base_url 指向 127.0.0.1:4000（或 localhost）。"""
    base = ((preset or {}).get("base_url") or "").strip()
    m = re.match(r"^https?://([^/:]+)(?::(\d+))?", base)
    if not m:
        return False
    host, port = m.group(1).lower(), int(m.group(2) or 80)
    return host in ("127.0.0.1", "localhost") and port == LITELLM_PORT


async def _port_open(host, port):
    try:
        _r, w = await asyncio.open_connection(host, port)
        w.close()
        return True
    except OSError:
        return False


async def litellm_status():
    proc = LITELLM["proc"]
    owned_alive = bool(LITELLM["owned"] and proc and proc.returncode is None)
    return {"running": await _port_open(LITELLM_HOST, LITELLM_PORT),
            "owned": owned_alive,
            "pid": proc.pid if owned_alive else None,
            "port": LITELLM_PORT}


async def litellm_start():
    """拉起 LiteLLM 代理；已在跑则收养（owned=False）。返回 (ok, msg)。"""
    if LITELLM["starting"]:
        # 另一个协程正在拉，原地等它出结果
        for _ in range(LITELLM_START_TIMEOUT * 2):
            if not LITELLM["starting"]:
                break
            await asyncio.sleep(0.5)
        ok = await _port_open(LITELLM_HOST, LITELLM_PORT)
        return (ok, "" if ok else "LiteLLM 启动超时")
    proc = LITELLM["proc"]
    if LITELLM["owned"] and proc and proc.returncode is None:
        return True, ""   # GUI 自己拉起的还在跑（自动连接已先行拉起的情况）
    if await _port_open(LITELLM_HOST, LITELLM_PORT):
        LOG.info("LiteLLM 已在 %s:%d 监听（外部启动，GUI 不接管）",
                 LITELLM_HOST, LITELLM_PORT)
        LITELLM["owned"] = False
        return True, ""
    if not LITELLM_EXE.exists() or not LITELLM_CONFIG.exists():
        missing = LITELLM_EXE if not LITELLM_EXE.exists() else LITELLM_CONFIG
        return False, f"LiteLLM 资产缺失：{missing}"
    LITELLM["starting"] = True
    try:
        log_fh = open(LITELLM_LOG, "ab", buffering=0)
        log_fh.write(f"\n===== GUI spawn {datetime.now():%Y-%m-%d %H:%M:%S} =====\n"
                     .encode("ascii"))
        # stdout 接文件句柄不接 PIPE：长驻进程会把 PIPE 缓冲区胀满死锁
        proc = await asyncio.create_subprocess_exec(
            str(LITELLM_EXE), "--config", "config.yaml",
            "--port", str(LITELLM_PORT), "--host", LITELLM_HOST,
            cwd=str(LITELLM_DIR),   # custom_callbacks.py 相对 cwd 导入，必须
            stdout=log_fh, stderr=asyncio.subprocess.STDOUT)
        LITELLM.update(proc=proc, owned=True, log_fh=log_fh)
        LOG.info("LiteLLM 已 spawn（pid=%d），等待就绪…", proc.pid)
        for _ in range(LITELLM_START_TIMEOUT * 2):
            if await _port_open(LITELLM_HOST, LITELLM_PORT):
                LOG.info("LiteLLM 就绪（pid=%d）", proc.pid)
                return True, ""
            if proc.returncode is not None:
                LOG.error("LiteLLM 启动即退出 rc=%s，见 %s",
                          proc.returncode, LITELLM_LOG)
                return False, "LiteLLM 启动失败，日志见 logs-gui.txt"
            await asyncio.sleep(0.5)
        return False, f"LiteLLM 启动超时（{LITELLM_START_TIMEOUT}s 未监听）"
    finally:
        LITELLM["starting"] = False


async def litellm_stop():
    """只停 GUI 自己拉起的实例。返回 (ok, msg)。"""
    proc = LITELLM["proc"]
    if not (LITELLM["owned"] and proc and proc.returncode is None):
        if await _port_open(LITELLM_HOST, LITELLM_PORT):
            return False, "LiteLLM 不是 GUI 启动的，请在原窗口关闭"
        return True, ""   # 本来就没在跑
    # litellm.exe 是启动器会再 spawn python 子进程持有端口，proc.terminate 只杀
    # 壳会留下孤儿——必须 taskkill /T 整棵树端掉
    killer = await asyncio.create_subprocess_exec(
        "taskkill", "/PID", str(proc.pid), "/T", "/F",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(killer.wait(), timeout=10)
        await asyncio.wait_for(proc.wait(), timeout=8)
    except asyncio.TimeoutError:
        pass
    # 端口释放有毫秒级滞后（套接字关闭晚于进程退出），轮询确认再报状态
    for _ in range(10):
        if not await _port_open(LITELLM_HOST, LITELLM_PORT):
            break
        await asyncio.sleep(0.3)
    if LITELLM["log_fh"]:
        LITELLM["log_fh"].close()
    LITELLM.update(proc=None, owned=False, log_fh=None)
    LOG.info("LiteLLM 已停止（GUI 托管实例）")
    return True, ""


async def litellm_restart():
    ok, msg = await litellm_stop()
    if not ok:
        return ok, msg
    return await litellm_start()


async def ensure_litellm():
    """build_client 钩子：桥没在跑就拉起。"""
    if await _port_open(LITELLM_HOST, LITELLM_PORT):
        return True, ""
    return await litellm_start()


async def litellm_cleanup():
    """退出钩子：只收 GUI 自己拉起的实例，外部启动的不动。"""
    if LITELLM["owned"]:
        await litellm_stop()


# ---- QQ 桥全托管（控制中心 /qq 页）----
# 复刻 LiteLLM 模式：spawn/探活/收回/owned 防误杀/taskkill /T 杀整树。
# 不同处：桥不是端口服务型进程，探活看 QQ_STATE 注册表（桥连入 server 的
# WS 才登记）；"分身上线"= qq_session 建好了 client（chat_enabled 已回）。
QQBRIDGE_DIR = PROJECT_ROOT / "tools" / "qq-bridge"
QQBRIDGE_SCRIPT = QQBRIDGE_DIR / "bridge.py"
QQBRIDGE_LOG = QQBRIDGE_DIR / "logs-gui.txt"
QQBRIDGE_START_TIMEOUT = 40     # 秒；桥连入 + chat_enabled 等待上限
QQBRIDGE = {"proc": None, "owned": False, "log_fh": None, "starting": False}


def _qq_session():
    """桥登记的活动 qq 会话（没有=None）。"""
    return QQ_STATE.get("session")


def _qq_online():
    """分身上线 = 桥已连入且 qq_session 的 client 已建好（chat_enabled 回了）。"""
    s = _qq_session()
    return bool(s and s.client is not None)


async def qqbridge_status():
    proc = QQBRIDGE["proc"]
    owned_alive = bool(QQBRIDGE["owned"] and proc and proc.returncode is None)
    return {"connected": _qq_session() is not None,   # 桥的 WS 在册
            "online": _qq_online(),                   # 分身脑子接线完成
            "owned": owned_alive,
            "pid": proc.pid if owned_alive else None}


async def qqbridge_start():
    """拉起 QQ 桥；已在跑则收养（owned=False）。返回 (ok, msg)。"""
    if QQBRIDGE["starting"]:
        for _ in range(QQBRIDGE_START_TIMEOUT * 2):
            if not QQBRIDGE["starting"]:
                break
            await asyncio.sleep(0.5)
        return (_qq_session() is not None,
                "" if _qq_session() is not None else "QQ 桥启动超时")
    proc = QQBRIDGE["proc"]
    if QQBRIDGE["owned"] and proc and proc.returncode is None:
        return True, ""
    if _qq_session() is not None:
        LOG.info("QQ 桥已在册（外部启动，GUI 不接管）")
        QQBRIDGE["owned"] = False
        return True, ""
    if not QQBRIDGE_SCRIPT.exists():
        return False, f"桥脚本缺失：{QQBRIDGE_SCRIPT}"
    cfg = load_qq()
    if not cfg.get("enabled"):
        return False, "presets.json 的 qq.enabled=false——先在设置里打开 QQ 桥"
    QQBRIDGE["starting"] = True
    try:
        log_fh = open(QQBRIDGE_LOG, "ab", buffering=0)
        log_fh.write(f"\n===== GUI spawn {datetime.now():%Y-%m-%d %H:%M:%S} =====\n"
                     .encode("ascii"))
        # stdout 接文件句柄不接 PIPE：长驻进程会把 PIPE 缓冲区胀满死锁。
        # 桥自己 logging 到 bridge.log，这里只兜 print/未捕获异常
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(QQBRIDGE_SCRIPT),
            cwd=str(QQBRIDGE_DIR),
            stdout=log_fh, stderr=asyncio.subprocess.STDOUT)
        QQBRIDGE.update(proc=proc, owned=True, log_fh=log_fh)
        LOG.info("QQ 桥已 spawn（pid=%d），等待连入…", proc.pid)
        for _ in range(QQBRIDGE_START_TIMEOUT * 2):
            if _qq_online():
                LOG.info("QQ 桥就绪（pid=%d，分身上线）", proc.pid)
                return True, ""
            if proc.returncode is not None:
                LOG.error("QQ 桥启动即退出 rc=%s，见 %s / bridge.log",
                          proc.returncode, QQBRIDGE_LOG)
                return False, "QQ 桥启动失败，日志见 tools/qq-bridge/logs-gui.txt"
            await asyncio.sleep(0.5)
        if _qq_session() is not None:
            return True, ""      # 桥连上了但 chat_enabled 还没回，算起来
        return False, f"QQ 桥启动超时（{QQBRIDGE_START_TIMEOUT}s 未连入）"
    finally:
        QQBRIDGE["starting"] = False


async def qqbridge_stop():
    """只停 GUI 自己拉起的实例。返回 (ok, msg)。"""
    proc = QQBRIDGE["proc"]
    if not (QQBRIDGE["owned"] and proc and proc.returncode is None):
        if _qq_session() is not None:
            return False, "QQ 桥不是控制台启动的，请在原窗口关闭"
        return True, ""
    killer = await asyncio.create_subprocess_exec(
        "taskkill", "/PID", str(proc.pid), "/T", "/F",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(killer.wait(), timeout=10)
        await asyncio.wait_for(proc.wait(), timeout=8)
    except asyncio.TimeoutError:
        pass
    # 桥被杀后 WS 断开 → handle_ws finally 会注销 QQ_STATE，等一下
    for _ in range(20):
        if _qq_session() is None:
            break
        await asyncio.sleep(0.3)
    if QQBRIDGE["log_fh"]:
        QQBRIDGE["log_fh"].close()
    QQBRIDGE.update(proc=None, owned=False, log_fh=None)
    LOG.info("QQ 桥已停止（GUI 托管实例）")
    return True, ""


async def qqbridge_cleanup():
    """退出钩子：只收 GUI 自己拉起的桥。"""
    if QQBRIDGE["owned"]:
        await qqbridge_stop()


def _genimg_today_count():
    """今日生图计数：gen.py 自管的成本闸状态文件（date 不符=0）。"""
    try:
        st = json.loads((GENIMG_DIR / ".state.json").read_text(encoding="utf-8"))
        if st.get("date") == datetime.now().strftime("%Y-%m-%d"):
            return int(st.get("count") or 0)
    except Exception:
        pass
    return 0


def _napcat_state():
    """NapCat 连接态 v1：bridge.log 尾部找最近的连入/断开记录。"""
    log = QQBRIDGE_DIR / "bridge.log"
    try:
        lines = log.read_text(encoding="utf-8", errors="replace") \
                  .splitlines()[-300:]
    except OSError:
        return {"state": "unknown"}
    last = None
    for ln in lines:
        if "NapCat 已连入" in ln:
            last = ("connected", ln[:19])
        elif "NapCat 断开" in ln:
            last = ("disconnected", ln[:19])
    if not last:
        return {"state": "unknown"}
    return {"state": last[0], "at": last[1]}


# ---- config.yaml 别名 CRUD（块拼接，保注释/保非 model_list 部分）----

def litellm_read_models(full=False):
    """读 model_list。full=True 带明文 api_key（仅内部合并用），
    默认脱敏成 has_key 给前端。解析失败抛异常由调用方兜。"""
    import yaml  # 懒加载：缺依赖只影响本功能，不拖垮整个后端
    doc = yaml.safe_load(LITELLM_CONFIG.read_text(encoding="utf-8")) or {}
    out = []
    for item in doc.get("model_list") or []:
        lp = (item or {}).get("litellm_params") or {}
        key = lp.get("api_key") or ""
        entry = {"model_name": item.get("model_name") or "",
                 "model": lp.get("model") or "",
                 "api_base": lp.get("api_base") or ""}
        entry["api_key" if full else "has_key"] = key if full else bool(key)
        out.append(entry)
    return out


def _serialize_model_list(models):
    """手写 model_list YAML 块（固定四字段 schema）。标量一律 json.dumps
    双引号——JSON 字符串是合法 YAML flow 标量，转义白拿。"""
    lines = ["model_list:"]
    for m in models:
        lines.append(f'  - model_name: {json.dumps(m["model_name"])}')
        lines.append("    litellm_params:")
        lines.append(f'      model: {json.dumps(m["model"])}')
        lines.append(f'      api_base: {json.dumps(m["api_base"])}')
        lines.append(f'      api_key: {json.dumps(m["api_key"])}')
    return "\n".join(lines)


def litellm_write_models(models):
    """整表替换 config.yaml 的 model_list 块，返回落盘后的别名列表。
    校验：字段非空、纯 ASCII、别名不重；空 api_key 按 model_name 继承旧值。
    只拼接替换 model_list 区间，其余内容（含注释）字节不动。"""
    import yaml
    if not models:
        raise ValueError("model_list 不能为空")
    old = {m["model_name"]: m for m in litellm_read_models(full=True)}
    merged, seen = [], set()
    for m in models:
        name = (m.get("model_name") or "").strip()
        model = (m.get("model") or "").strip()
        base = (m.get("api_base") or "").strip()
        key = (m.get("api_key") or "").strip()
        if not (name and model and base):
            raise ValueError(f"别名字段不完整：{name or '(空别名)'}")
        if name in seen:
            raise ValueError(f"别名重复：{name}")
        seen.add(name)
        if not key:
            key = (old.get(name) or {}).get("api_key") or ""
        if not key:
            raise ValueError(f"别名 {name} 缺 api_key（改名=新条目，需重填密钥）")
        for label, val in (("model_name", name), ("model", model),
                           ("api_base", base), ("api_key", key)):
            if not val.isascii():
                raise ValueError(
                    f"{name} 的 {label} 含非 ASCII 字符（litellm GBK 解码会崩）")
        merged.append({"model_name": name, "model": model,
                       "api_base": base, "api_key": key})
    text = LITELLM_CONFIG.read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.rstrip() == "model_list:")
    except StopIteration:
        raise ValueError("config.yaml 里找不到 model_list 块") from None
    end = next((i for i in range(start + 1, len(lines))
                if lines[i] and lines[i][0] not in " \t#"), len(lines))
    new_lines = (lines[:start] + _serialize_model_list(merged).splitlines()
                 + lines[end:])
    new_text = "\n".join(new_lines) + "\n"
    # 回验：能解析 + 条目数对 + 其余顶层键还在
    doc = yaml.safe_load(new_text)
    assert len(doc.get("model_list") or []) == len(merged), "回验失败：条目数不符"
    for k in ("general_settings", "litellm_settings"):
        assert k in doc, f"回验失败：丢了 {k}"
    new_text.encode("ascii")  # 硬断言：任何非 ASCII 在这炸，不碰磁盘
    bak = LITELLM_DIR / "config.yaml.bak"
    if not bak.exists():
        bak.write_text(text, encoding="utf-8")
    tmp = LITELLM_CONFIG.with_suffix(".yaml.tmp")
    tmp.write_text(new_text, encoding="ascii")
    os.replace(tmp, LITELLM_CONFIG)
    LOG.info("config.yaml model_list 已更新（%d 条别名）", len(merged))
    return merged


async def _litellm_state_msg(session, msg=None, saved=False):
    """组 litellm_state 推送：进程状态 + 别名表（脱敏）+ 当前模型。"""
    st = await litellm_status()
    try:
        models = litellm_read_models()
    except Exception as e:
        models = []
        msg = msg or f"config.yaml 读取失败：{e}"
    out = {"type": "litellm_state", **st, "models": models,
           "current_model": session.current_model}
    if msg:
        out["msg"] = msg
    if saved:
        out["saved"] = True
    return out

# ═══════════════════════════════════════════════════════════════════
# 识图预设（vision_presets）— 独立于聊天预设，走 OpenAI 兼容 API
# ───────────────────────────────────────────────────────────────────
# 与聊天预设结构相同但无 vision 字段（识图模型必然支持视觉）。
# CRUD 镜像 upsert_preset / delete_preset / _safe_preset 模式。


def load_vision_presets():
    """返回 vision_presets 列表（缺失或损坏回 []）。"""
    return _read_presets_file().get("vision_presets", []) or []


def find_vision_preset(vpid):
    """按 id 查一个识图预设，找不到返回 None。"""
    for p in load_vision_presets():
        if p.get("id") == vpid:
            return p
    return None


def upsert_vision_preset(preset):
    """增/改识图预设。有 id=改，无 id=增。token 留空保留旧值。返回落盘 dict。"""
    obj = _read_presets_file()
    vpresets = obj.get("vision_presets", []) or []
    pid = preset.get("id")
    token = (preset.get("token") or "").strip()
    clean = {
        "id": pid or uuid.uuid4().hex,
        "name": (preset.get("name") or "未命名").strip(),
        "base_url": (preset.get("base_url") or "").strip().rstrip("/"),
        "token": token,
        "model": (preset.get("model") or "").strip(),
    }
    if pid:
        for i, p in enumerate(vpresets):
            if p.get("id") == pid:
                if not token:
                    clean["token"] = p.get("token", "")
                vpresets[i] = clean
                break
        else:
            vpresets.append(clean)
    else:
        vpresets.append(clean)
    obj["vision_presets"] = vpresets
    _write_presets_file(obj)
    return clean


def delete_vision_preset(vpid):
    """删除识图预设。若正被 vision_relay 引用，清空 preset_id 引用。"""
    obj = _read_presets_file()
    obj["vision_presets"] = [p for p in (obj.get("vision_presets", []) or [])
                             if p.get("id") != vpid]
    vr = obj.get("vision_relay")
    if isinstance(vr, dict) and vr.get("preset_id") == vpid:
        vr["preset_id"] = None
    _write_presets_file(obj)


def _safe_vision_preset(p):
    """给前端用的脱敏视图：token → has_token。"""
    if not p:
        return None
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "base_url": p.get("base_url"),
        "model": p.get("model"),
        "has_token": bool(p.get("token")),
    }


def _resolve_vision_relay(cfg):
    """解析 vision_relay 配置：若引用 preset_id，合并预设值（手填字段优先覆盖）。
    不修改存储，每次调用动态解析。返回完整 {enabled, base_url, token, model}。"""
    if not cfg:
        return None
    preset_id = cfg.get("preset_id")
    base_url = (cfg.get("base_url") or "").strip().rstrip("/")
    token = cfg.get("token", "")
    model = (cfg.get("model") or "").strip()
    if preset_id:
        preset = find_vision_preset(preset_id)
        if preset:
            base_url = base_url or (preset.get("base_url") or "").strip().rstrip("/")
            token = token or preset.get("token", "")
            model = model or (preset.get("model") or "").strip()
    return {
        "enabled": bool(cfg.get("enabled")),
        "base_url": base_url,
        "token": token,
        "model": model,
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
    """保存识图转接配置。支持 preset_id；无 preset 时 token 留空=保留旧值。"""
    obj = _read_presets_file()
    old = obj.get("vision_relay") or {}
    token = (cfg.get("token") or "").strip()
    preset_id = cfg.get("preset_id") or None
    if preset_id:
        # 预设模式：空 token 有效（委托预设密钥）
        new_token = token
    else:
        # 手填模式：空 token 保留旧值（兼容旧行为）
        new_token = token or old.get("token", "")
    obj["vision_relay"] = {
        "enabled": bool(cfg.get("enabled")),
        "preset_id": preset_id,
        "base_url": (cfg.get("base_url") or "").strip().rstrip("/"),
        "token": new_token,
        "model": (cfg.get("model") or "").strip(),
    }
    _write_presets_file(obj)
    return obj["vision_relay"]


def _safe_vision_relay(cfg):
    if not cfg:
        return None
    preset_id = cfg.get("preset_id")
    preset_name = None
    has_token = bool(cfg.get("token"))
    if preset_id:
        preset = find_vision_preset(preset_id)
        if preset:
            preset_name = preset.get("name")
            has_token = has_token or bool(preset.get("token"))
    return {
        "enabled": bool(cfg.get("enabled")),
        "preset_id": preset_id,
        "preset_name": preset_name,
        "base_url": cfg.get("base_url"),
        "model": cfg.get("model"),
        "has_token": has_token,
    }


def _relay_ready(cfg):
    return bool(cfg and cfg.get("enabled") and cfg.get("base_url")
                and cfg.get("token") and cfg.get("model"))


# ═══════════════════════════════════════════════════════════════════
# 生图配置（genimg）
# ───────────────────────────────────────────────────────────────────
# 达妮娅"拍照"功能的生图 API 配置（火山方舟 Seedream，OpenAI 风格端点）。
# 配置存 presets.json 的 genimg 键（与预设同文件同敏感度）；
# 生成逻辑本体在 tools/生图/gen.py（成本闸/参考图/提示词拼装都在那边）。
# ═══════════════════════════════════════════════════════════════════
GENIMG_DEFAULTS = {
    "enabled": False,
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "token": "",
    "model": "doubao-seedream-4-5-251128",
    "daily_limit": 20,
    "cooldown_min": 3,
    "deliver_inject": True,   # 照片洗好后闲时注入"拍好啦"消息（互动感核心）
}


def load_genimg():
    cfg = dict(GENIMG_DEFAULTS)
    cfg.update(_read_presets_file().get("genimg") or {})
    return cfg


def save_genimg(cfg):
    """保存生图配置。token 留空=保留旧值（与 vision_relay 同款约定）。"""
    obj = _read_presets_file()
    old = obj.get("genimg") or {}
    token = (cfg.get("token") or "").strip() or old.get("token", "")
    obj["genimg"] = {
        "enabled": bool(cfg.get("enabled")),
        "base_url": (cfg.get("base_url") or "").strip().rstrip("/"),
        "token": token,
        "model": (cfg.get("model") or "").strip(),
        "daily_limit": max(1, int(cfg.get("daily_limit") or GENIMG_DEFAULTS["daily_limit"])),
        "cooldown_min": max(0.0, float(cfg.get("cooldown_min") or 0.0)),
        "deliver_inject": bool(cfg.get("deliver_inject", True)),
    }
    _write_presets_file(obj)
    return obj["genimg"]


def _safe_genimg(cfg):
    """给前端的脱敏视图：不回传 token，只报有没有。"""
    if not cfg:
        return None
    return {
        "enabled": bool(cfg.get("enabled")),
        "base_url": cfg.get("base_url"),
        "model": cfg.get("model"),
        "daily_limit": cfg.get("daily_limit"),
        "cooldown_min": cfg.get("cooldown_min"),
        "deliver_inject": bool(cfg.get("deliver_inject", True)),
        "has_token": bool(cfg.get("token")),
    }


# 拍照标记：L2 里 [生图:画面]/[改图:调整]/[打卡:互动]，关段时检出 → 后台 worker
GENIMG_RE = re.compile(r"[\[【](生图|改图|打卡)[:：](.+?)[\]】]")

# ═══════════════════════════════════════════════════════════════════
# 语音（TTS）：backend registry —— 换后端=纯新增（TTS_BACKENDS 加一项 + 前端加字段组），
# 编排/缓存/中断/播放全通用不碰。managed=True=GUI 拉起本地进程（LiteLLM 同款 owned
# 模式，端口探活）；managed=False=云服务（校验 key 即可）。产物统一落 VOICE_DIR 走 /voice/。
# presets.json 结构：tts={enabled,backend} + tts_<backend>={各自字段} —— 切换不丢配置。
# ───────────────────────────────────────────────────────────────────
TTS_DEFAULTS = {"enabled": False, "backend": "sovits"}
TTS_START_TIMEOUT = 180  # 秒；SoVITS CPU 冷启实测 25~133s（机器负载相关），留足余量

VOICE_DIR = HERE / "out" / "voice"
VOICE_DIR.mkdir(exist_ok=True)

# 单用户只有一个本地语音实例，模块级状态即可（字段同 LITELLM；gen=叫停代际）
SOVITS = {"proc": None, "owned": False, "log_fh": None, "starting": False, "gen": 0}
TTS_LOCK = asyncio.Lock()   # 串行合成：CPU 单路，排队防两个请求互抢双双变慢


def _dashscope_key_from_vision():
    """零配置回落：识图预设里的 dashscope key 直接给百炼语音用（同账号）。"""
    for vp in _read_presets_file().get("vision_presets") or []:
        if "dashscope.aliyuncs.com" in (vp.get("base_url") or "") and vp.get("token"):
            return vp["token"]
    return ""


def load_tts(backend=None):
    """合并视图：{enabled, backend, managed, label, ...指定/当前 backend 子配置}。"""
    obj = _read_presets_file()
    root = dict(TTS_DEFAULTS)
    root.update({k: v for k, v in (obj.get("tts") or {}).items() if k in TTS_DEFAULTS})
    backend = backend or root["backend"]
    if backend not in TTS_BACKENDS:
        backend = TTS_DEFAULTS["backend"]
    spec = TTS_BACKENDS[backend]
    sub = obj.get(f"tts_{backend}") or {}
    if not sub and backend == "sovits" and (obj.get("tts") or {}).get("sovits_dir"):
        sub = obj["tts"]   # 旧版平铺迁移：首次保存自动写成新结构
    cfg = dict(spec["defaults"])
    cfg.update({k: v for k, v in sub.items() if k in spec["fields"]})
    if backend == "bailian" and not cfg.get("api_key"):
        cfg["api_key"] = _dashscope_key_from_vision()
    cfg.update(enabled=bool(root["enabled"]), backend=backend,
               managed=spec["managed"], label=spec["label"])
    return cfg


def save_tts(payload):
    """payload={enabled, backend, cfg:{子字段}}。只写当前 backend 的子配置节，
    其余 backend 的节原样保留（切换不丢设置）。secret 字段留空=保留旧值。"""
    obj = _read_presets_file()
    old_root = obj.get("tts") or {}
    backend = payload.get("backend") or old_root.get("backend") or TTS_DEFAULTS["backend"]
    if backend not in TTS_BACKENDS:
        backend = TTS_DEFAULTS["backend"]
    spec = TTS_BACKENDS[backend]
    old_sub = obj.get(f"tts_{backend}") or {}
    if not old_sub and backend == "sovits" and old_root.get("sovits_dir"):
        old_sub = old_root   # 旧版平铺迁移
    sub_in = payload.get("cfg") or {}
    sub = {}
    for k in spec["fields"]:
        v = sub_in.get(k)
        v = v.strip() if isinstance(v, str) else v
        if k in spec.get("secret_fields", ()):
            if v:
                sub[k] = v
            elif old_sub.get(k):
                sub[k] = old_sub[k]          # 留空=保留旧 key
            # 都没有 → 不写（运行时回落识图 dashscope key）
        elif v in (None, ""):
            sub[k] = old_sub.get(k, spec["defaults"].get(k, ""))
        else:
            try:
                sub[k] = int(v) if k in spec.get("int_fields", ()) else v
            except (TypeError, ValueError):
                sub[k] = old_sub.get(k, spec["defaults"].get(k, ""))
    # 端口合法域兜底：0/越界/垃圾一律回落旧值或默认
    # （2026-08-08 前端空值曾被 parseInt||0 写成 0，合成打到 127.0.0.1:0 报 WinError 10049，
    #   且 spawn 出 -p 0 的 SoVITS 绑随机端口变僵尸）
    if "port" in spec["fields"]:
        try:
            _p = int(sub.get("port", 0))
        except (TypeError, ValueError):
            _p = 0
        _old_p = old_sub.get("port")
        if not 1 <= _p <= 65535:
            sub["port"] = _old_p if isinstance(_old_p, int) and 1 <= _old_p <= 65535 \
                else spec["defaults"].get("port", 9880)
    obj["tts"] = {"enabled": bool(payload.get("enabled")), "backend": backend}
    obj[f"tts_{backend}"] = sub
    _write_presets_file(obj)
    return load_tts()


def _safe_tts(cfg):
    """secret 字段不下发，只给 has_token（反映含回落的生效值）。
    all=每个 backend 的打码子配置（前端切下拉即时回填，不用先保存）。"""
    if not cfg:
        return None
    spec = TTS_BACKENDS.get(cfg.get("backend"), {})
    out = {k: cfg.get(k) for k in
           ("enabled", "backend", "managed", "label") + tuple(spec.get("fields", ()))}
    for k in spec.get("secret_fields", ()):
        out.pop(k, None)
        out["has_token"] = bool(cfg.get(k))
    out["backends"] = [{"id": bid, "label": b["label"], "managed": b["managed"]}
                       for bid, b in TTS_BACKENDS.items()]
    all_sub = {}
    for bid, b in TTS_BACKENDS.items():
        sub = {k: load_tts(bid).get(k) for k in b["fields"]}
        for k in b.get("secret_fields", ()):
            sub.pop(k, None)
            sub["has_token"] = bool(load_tts(bid).get(k))
        all_sub[bid] = sub
    out["all"] = all_sub
    return out


# ---- TTS 朗读化预处理（小模型把回复改写成适合朗读的口语文本）----
# 分层设计：_tts_clean（正则）是永远跑的兜底；这里是可选增强。
# markdown/链接/emoji/中英混排这些正则搞不定的交给小模型；
# 它挂了/失控了静默落回正则结果，绝不影响出声。
TTS_PRE_DEFAULTS = {
    "enabled": True,
    "model": "deepseek-v4-flash",            # 本地 LiteLLM 别名，一次不到一厘
    "base_url": "http://127.0.0.1:4000/v1",
    "token": "sk-denia-local",
}
TTS_PRE_TIMEOUT = 25   # deepseek-v4-flash 是思考型，一句改写思考就烧 ~700 token/7s
TTS_PRE_PROMPT = (
    "你是语音合成前的朗读文本清洗器。把输入改写成适合直接朗读的纯口语文本："
    "删去动作、神态、内心描写（如（轻笑）（小声）），删去 markdown 符号、emoji、"
    "链接、代码块、方括号标记；数字与英文按中文口语习惯自然处理；保留原意与语气。"
    "只输出改写后的文本本身，禁止任何解释、前缀、引号。")


def load_tts_pre():
    cfg = dict(TTS_PRE_DEFAULTS)
    cfg.update({k: v for k, v in (_read_presets_file().get("tts_preprocess") or {}).items()
                if k in TTS_PRE_DEFAULTS})
    return cfg


def save_tts_pre(pre):
    """只认 TTS_PRE_DEFAULTS 里的键；token 留空=保留旧值，其余留空=旧值或默认。"""
    obj = _read_presets_file()
    old = obj.get("tts_preprocess") or {}
    out = {}
    for k in TTS_PRE_DEFAULTS:
        v = (pre or {}).get(k)
        v = v.strip() if isinstance(v, str) else v
        if k == "enabled":
            out[k] = bool(v)
        elif v in (None, ""):
            out[k] = old.get(k, TTS_PRE_DEFAULTS[k])
        else:
            out[k] = v
    obj["tts_preprocess"] = out
    _write_presets_file(obj)
    return load_tts_pre()


def _safe_tts_pre(cfg):
    return {"enabled": bool(cfg.get("enabled")), "model": cfg.get("model"),
            "base_url": cfg.get("base_url"), "has_token": bool(cfg.get("token"))}


def _tts_preprocess(text, cfg):
    """朗读化一遍（to_thread 里跑）。返回处理后的文本；任何失败/失控 → None，
    调用方落回正则清洗结果。OpenAI 兼容端点（默认本地 LiteLLM），urllib 零依赖。"""
    if not cfg.get("enabled"):
        return None
    try:
        body = json.dumps({
            "model": cfg["model"], "temperature": 0, "max_tokens": 2048,
            "messages": [{"role": "system", "content": TTS_PRE_PROMPT},
                         {"role": "user", "content": text}],
        }).encode("utf-8")
        req = urllib.request.Request(
            cfg["base_url"].rstrip("/") + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {cfg['token']}"})
        with urllib.request.urlopen(req, timeout=TTS_PRE_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        out = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        out = _tts_clean(out)          # 防小模型自己吐标记/动作描写
        # sanity：空 / 膨胀 / 缩水 → 视为失控，落回正则结果
        if not out or len(out) > max(60, len(text) * 3):
            return None
        if len(text) >= 20 and len(out) < len(text) * 0.2:
            return None
        return out
    except Exception as e:
        LOG.warning("TTS 朗读化失败（落回正则结果）：%s", e)
        return None


def _tts_ref_path(cfg):
    p = Path(cfg["ref_audio"])
    return str(p if p.is_absolute() else Path(cfg["sovits_dir"]) / p)


async def tts_status(cfg=None):
    cfg = cfg or load_tts()
    if not cfg.get("managed", True):
        # 云 backend：无进程可探，key+voice_id 齐就算就绪
        return {"running": bool(cfg.get("api_key") and cfg.get("voice_id")),
                "owned": False, "pid": None, "port": None}
    proc = SOVITS["proc"]
    owned_alive = bool(SOVITS["owned"] and proc and proc.returncode is None)
    return {"running": await _port_open(cfg["host"], cfg["port"]),
            "owned": owned_alive,
            "pid": proc.pid if owned_alive else None,
            "port": cfg["port"]}


async def tts_start(cfg=None):
    """拉起 SoVITS api_v2；已在跑则收养（owned=False）。返回 (ok, msg)。"""
    cfg = cfg or load_tts()
    host, port = cfg["host"], cfg["port"]
    if SOVITS["starting"]:
        for _ in range(TTS_START_TIMEOUT * 2):
            if not SOVITS["starting"]:
                break
            await asyncio.sleep(0.5)
        ok = await _port_open(host, port)
        return (ok, "" if ok else "语音服务启动超时")
    proc = SOVITS["proc"]
    if SOVITS["owned"] and proc and proc.returncode is None:
        return True, ""
    if await _port_open(host, port):
        LOG.info("SoVITS 已在 %s:%d 监听（外部启动，GUI 不接管）", host, port)
        SOVITS["owned"] = False
        return True, ""
    root = Path(cfg["sovits_dir"])
    launcher = root / "run_api.py"
    py = Path(cfg["python"])
    if not root.exists() or not launcher.exists() or not py.exists():
        missing = root if not root.exists() else (launcher if not launcher.exists() else py)
        return False, f"语音资产缺失：{missing}"
    log_path = root / "logs-gui.txt"
    SOVITS["starting"] = True
    try:
        log_fh = open(log_path, "ab", buffering=0)
        log_fh.write(f"\n===== GUI spawn {datetime.now():%Y-%m-%d %H:%M:%S} =====\n"
                     .encode("ascii"))
        # stdout 接文件句柄不接 PIPE：长驻进程会把 PIPE 缓冲区胀满死锁
        proc = await asyncio.create_subprocess_exec(
            str(py), str(launcher), "-p", str(port),
            cwd=str(root),
            stdout=log_fh, stderr=asyncio.subprocess.STDOUT)
        SOVITS.update(proc=proc, owned=True, log_fh=log_fh)
        LOG.info("SoVITS 已 spawn（pid=%d），等待就绪…", proc.pid)
        for _ in range(TTS_START_TIMEOUT * 2):
            if await _port_open(host, port):
                LOG.info("SoVITS 就绪（pid=%d）", proc.pid)
                return True, ""
            if proc.returncode is not None:
                LOG.error("SoVITS 启动即退出 rc=%s，见 %s", proc.returncode, log_path)
                return False, "语音服务启动失败，日志见 GPTSoVITS/logs-gui.txt"
            await asyncio.sleep(0.5)
        return False, f"语音服务启动超时（{TTS_START_TIMEOUT}s 未监听）"
    finally:
        SOVITS["starting"] = False


async def tts_stop():
    """只停 GUI 自己拉起的实例。返回 (ok, msg)。"""
    cfg = load_tts()
    proc = SOVITS["proc"]
    if not (SOVITS["owned"] and proc and proc.returncode is None):
        if await _port_open(cfg["host"], cfg["port"]):
            return False, "语音服务不是 GUI 启动的，请在原窗口关闭"
        return True, ""
    killer = await asyncio.create_subprocess_exec(
        "taskkill", "/PID", str(proc.pid), "/T", "/F",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(killer.wait(), timeout=10)
        await asyncio.wait_for(proc.wait(), timeout=8)
    except asyncio.TimeoutError:
        pass
    for _ in range(10):
        if not await _port_open(cfg["host"], cfg["port"]):
            break
        await asyncio.sleep(0.3)
    if SOVITS["log_fh"]:
        SOVITS["log_fh"].close()
    SOVITS.update(proc=None, owned=False, log_fh=None)
    LOG.info("SoVITS 已停止（GUI 托管实例）")
    return True, ""


async def ensure_tts():
    """tts_speak 钩子：managed=开了开关但服务没跑 → 拉起；云=校验 key/voice_id。"""
    cfg = load_tts()
    if not cfg.get("managed", True):
        if not cfg.get("api_key"):
            return False, "百炼云缺 API Key（设置 → 语音 填写，或识图预设放 dashscope key 自动回落）"
        if not cfg.get("voice_id"):
            return False, "百炼云缺 voice_id（设置 → 语音 填写）"
        return True, ""
    if await _port_open(cfg["host"], cfg["port"]):
        return True, ""
    return await tts_start(cfg)


async def tts_cleanup():
    """退出钩子：只收 GUI 自己拉起的实例，外部启动的不动。"""
    if SOVITS["owned"]:
        await tts_stop()


# 送 TTS 前剥标记（"送前剥标记"既定决策）：[表情:x]/[生图:x]/[改图:x]/[打卡:x]/
# [划线:Pxxx]/裸[Pxxx]/前端渲染出的📍Pxxxx，以及层标记 [L1]/[L2]
TTS_MARK_RE = re.compile(
    r"[\[【](?:表情|生图|改图|打卡|划线)[^\]】]*[\]】]"
    r"|[\[【]P\d{1,4}[\]】]"
    r"|📍P?\d{1,4}"
    r"|\[L[12]\]")

# 动作/神态描写不朗读：全角（...）一律剥；半角 (...) 只剥无字母数字的（(笑)剥、(v2)留）
TTS_PAREN_RE = re.compile(r"（[^（）]*）|\((?![^()]*[A-Za-z0-9])[^()]*\)")
# LLM 预处理挂掉时的正则兜底也要防 SoVITS 400：链接整段剥 + markdown 符号剥
TTS_URL_RE = re.compile(r"https?://\S+|www\.\S+")
TTS_MD_RE = re.compile(r"[*`_#]+")


def _tts_clean(text):
    text = TTS_MARK_RE.sub("", text or "")
    text = TTS_PAREN_RE.sub("", text)
    text = TTS_MD_RE.sub("", TTS_URL_RE.sub("", text))
    # 剥完留下的标点残渣：连读标点收敛为一个，句首标点去掉
    text = re.sub(r"[，。！？；：、…]{2,}",
                  lambda m: m.group(0)[0], text)
    text = re.sub(r"^[，。！？；：、…\s]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _synth_sovits(cfg, text, out_path):
    """阻塞合成（to_thread 里跑）：POST api_v2 /tts → wav 落盘。urllib 零依赖。"""
    body = json.dumps({
        "text": text, "text_lang": "zh",
        "ref_audio_path": _tts_ref_path(cfg),
        "prompt_text": cfg["prompt_text"], "prompt_lang": "zh",
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://{cfg['host']}:{cfg['port']}/tts", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    if not data.startswith(b"RIFF"):
        raise RuntimeError(f"语音服务返回异常：{data[:150]!r}")
    out_path.write_bytes(data)


def _synth_bailian(cfg, text, out_path):
    """百炼 CosyVoice 克隆音色（dashscope SDK，WS 流式聚合成整段返回）。
    PCM_24000HZ_MONO_16BIT 回来的是裸 PCM，手动包 WAV 头。"""
    try:
        import dashscope
        from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer
    except ImportError:
        raise RuntimeError("GUI venv 缺 dashscope：venv/Scripts/python -m pip install dashscope")
    dashscope.api_key = cfg["api_key"]
    sp = SpeechSynthesizer(model=cfg["model"], voice=cfg["voice_id"],
                           format=AudioFormat.PCM_24000HZ_MONO_16BIT)
    audio = sp.call(text)
    if not audio:
        raise RuntimeError(f"百炼合成失败 request_id={sp.get_last_request_id()}")
    if audio[:4] == b"RIFF":
        out_path.write_bytes(audio)
        return
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(audio)


# backend registry：加新后端=这里加一项 + 前端语音分页加字段组。
#   managed       True=GUI 托管本地进程（需 host/port 探活 + tts_start/stop）
#   defaults      子配置默认值（tts_<backend> 节缺省时补齐）
#   fields        持久化字段白名单（save_tts 只认这些键）
#   secret_fields 密钥字段：不下发前端，保存时留空=保留旧值
#   int_fields    需要 int 强转的字段
#   synth         阻塞合成 fn(cfg, text, out_path)，失败抛异常
#   cache_key     缓存键材料（同文同音色命中缓存）
TTS_BACKENDS = {
    "sovits": {
        "label": "本地 GPT-SoVITS",
        "managed": True,
        "defaults": {
            "host": "127.0.0.1",
            "port": 9880,
            "sovits_dir": r"<GPTSoVITS目录>",
            "python": r"E:\anaconda\envs\sovits\python.exe",
            # 参考音频：相对 sovits_dir 或绝对路径（villia/dania 官方推荐条）
            "ref_audio": r"GPT-SoVITS\output\slicer_opt\output.wav_0009342720_0009558400.wav",
            "prompt_text": "怎么啊？如果有你在也不放心，那就干脆给我也装个限制器或者炸弹喽。",
        },
        "fields": ["host", "port", "sovits_dir", "python", "ref_audio", "prompt_text"],
        "int_fields": {"port"},
        "synth": _synth_sovits,
        "cache_key": lambda cfg: _tts_ref_path(cfg),
    },
    "bailian": {
        "label": "百炼云 · CosyVoice 克隆",
        "managed": False,
        "defaults": {
            "api_key": "",   # 留空回落识图预设的 dashscope key
            "voice_id": "cosyvoice-v3.5-plus-你的音色ID",
            "model": "cosyvoice-v3.5-plus",
        },
        "fields": ["api_key", "voice_id", "model"],
        "secret_fields": {"api_key"},
        "synth": _synth_bailian,
        "cache_key": lambda cfg: f"{cfg.get('model')}|{cfg.get('voice_id')}",
    },
}


def _tts_synth_or_cache(cfg, text):
    """同 backend+同音色+同文命中缓存直接给（重按不重新烧 CPU/钱）。"""
    spec = TTS_BACKENDS[cfg["backend"]]
    key = hashlib.sha1(
        f"{cfg['backend']}|{spec['cache_key'](cfg)}|{text}".encode("utf-8")
    ).hexdigest()[:16]
    out = VOICE_DIR / f"{key}.wav"
    if not out.exists():
        spec["synth"](cfg, text, out)
    return out


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
    try:
        with urllib.request.urlopen(req, timeout=VISION_RELAY_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 读响应体拿 API 返回的真实错误信息
        try:
            detail = e.read().decode("utf-8")
            if len(detail) > 500:
                detail = detail[:500] + "…"
        except Exception:
            detail = "(无法读取响应体)"
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
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
# QQ 桥识图（qq_vision）——四通道的模型/压缩侧，门控缓存在桥进程
# ───────────────────────────────────────────────────────────────────
# 桥通过 qq_vision WS 帧请求代看：mode=glance 走略读（Pillow 压小图 +
# vision_glance 节的便宜模型，只取轮廓）；mode=look/ask 走看图（原图 +
# vision_relay 节的好模型，复用 vision_describe/vision_ask）。
# 图片必须先由桥拷进 QQ_IMG_CACHE（只允许读这个目录，防任意文件探测）。
# ═══════════════════════════════════════════════════════════════════
QQ_IMG_CACHE = PROJECT_ROOT / "denia" / "缓冲" / "图片缓存"
VISION_GLANCE_EDGE = 256      # 略读压缩长边（聊天窗小图级别）
VISION_GLANCE_DESC_MAX = 300  # 略读描述截断（轮廓就够，防话痨）


def load_vision_glance():
    return _read_presets_file().get("vision_glance")


def _resolve_vision_glance():
    """略读模型配置：token/base_url 留空回落 vision_relay 的解析结果
    （同一家 GLM 同一把 key，只换个便宜模型名）。返回 {enabled,...}。"""
    cfg = load_vision_glance() or {}
    relay = _resolve_vision_relay(load_vision_relay()) or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "base_url": (cfg.get("base_url") or relay.get("base_url") or "")
        .strip().rstrip("/"),
        "token": cfg.get("token") or relay.get("token") or "",
        "model": (cfg.get("model") or "glm-4v-flash").strip(),
    }


def _glance_shrink_sync(src, dst):
    """压略读小图：长边 → VISION_GLANCE_EDGE，静态帧存 JPEG。返回成功与否。"""
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        img = Image.open(src)
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > VISION_GLANCE_EDGE:
            s = VISION_GLANCE_EDGE / max(w, h)
            img = img.resize((max(1, int(w * s)), max(1, int(h * s))),
                             Image.LANCZOS)
        img.save(dst, "JPEG", quality=82)
        return True
    except Exception as e:
        LOG.warning("略读压缩失败（%s）：%s", src, e)
        return False


async def _handle_qq_vision(session, data):
    """桥侧代看请求：{req_id, mode, img_path, context, question} →
    qq_vision_result {req_id, ok, desc|error}。只看 QQ_IMG_CACHE 里的图。"""
    req_id = data.get("req_id")
    mode = data.get("mode") or "glance"
    img = (data.get("img_path") or "").strip()
    try:
        p = Path(img).resolve()
        root = str(QQ_IMG_CACHE.resolve())
        if not str(p).startswith(root) or not p.is_file():
            raise RuntimeError("图片不在 QQ 图片缓存目录内")
        if mode == "glance":
            cfg = _resolve_vision_glance()
            if not _relay_ready(cfg):
                raise RuntimeError("略读模型未配置（vision_glance/vision_relay）")
            small = p.with_name(p.stem + "_glance.jpg")
            if not await asyncio.to_thread(_glance_shrink_sync, p, small):
                small = p                      # 压缩不可用就原图上（兜底）
            prompt = (
                "你是识图助手，为一个看不到图片的 AI 角色快速「瞟一眼」图。"
                "用中文一两句话概括：表情包说清画面主体+图上文字（原样转录）"
                "+情绪；照片/截图说清主体是什么。只要轮廓，不要细节和评论。\n")
            desc = await asyncio.to_thread(
                _vision_call_sync, cfg, [str(small)], prompt)
            desc = desc[:VISION_GLANCE_DESC_MAX]
        else:
            cfg = _resolve_vision_relay(load_vision_relay())
            if not _relay_ready(cfg):
                raise RuntimeError("识图转接未配置（vision_relay）")
            if mode == "ask":
                desc = await vision_ask(cfg, [str(p)],
                                        (data.get("question") or "").strip())
            else:                                # look：带上下文细看
                desc = await vision_describe(
                    cfg, [str(p)], (data.get("context") or "").strip(), "")
        await session.send({"type": "qq_vision_result", "req_id": req_id,
                            "ok": True, "desc": desc})
    except Exception as e:
        await session.send({"type": "qq_vision_result", "req_id": req_id,
                            "ok": False, "error": str(e)})


async def _handle_qq_voice(session, data):
    """桥侧语音合成请求：{req_id, text} → qq_voice_result {req_id, ok, b64|error}。
    复用 GUI 语音同一条管线（朗读化 → ensure_tts → TTS_LOCK 串行 → 合成缓存）。
    权限/日帽闸全在桥侧，这里只做长度防御。不占会话、不等 ready（同 qq_vision）。"""
    req_id = data.get("req_id")
    text = (data.get("text") or "").strip()[:200]
    try:
        cfg = load_tts()
        if not cfg.get("enabled"):
            raise RuntimeError("语音未开启（设置 → 语音 打开开关）")
        clean = _tts_clean(text)
        if not clean:
            raise RuntimeError("这段没有可读的文字")
        pre = await asyncio.to_thread(_tts_preprocess, clean, load_tts_pre())
        if pre:
            clean = pre
        ok, msg = await ensure_tts()
        if not ok:
            raise RuntimeError(msg)
        async with TTS_LOCK:
            out = await asyncio.to_thread(_tts_synth_or_cache, cfg, clean)
        b64 = base64.b64encode(out.read_bytes()).decode("ascii")
        LOG.info("qq_voice 合成完成：%s（%d 字）", out.name, len(clean))
        await session.send({"type": "qq_voice_result", "req_id": req_id,
                            "ok": True, "b64": b64})
    except Exception as e:
        LOG.warning("qq_voice 合成失败：%s", e)
        await session.send({"type": "qq_voice_result", "req_id": req_id,
                            "ok": False, "error": str(e)})

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


def _preset_sdk_env(preset):
    """预设 → SDK 进程 env。base_url/token 走 Anthropic 协议；preset 带 model 时把内部
    调用（会话标题生成等）的默认模型也指到该模型——否则 CLI 用内置 Claude 默认名，
    经本地桥（CCR/LiteLLM）原样转发给 OpenAI 端点会 503/400（novadiff 只服务 GPT 池）。
    ⚠️ 本函数必须放在 class Session 之外——2026-08-06 曾误插在类中间顶格写，
    把后续所有类方法吞成函数体内死代码（能 import 但 GUI 运行即崩）。"""
    env = {}
    if preset.get("base_url"):
        env["ANTHROPIC_BASE_URL"] = preset["base_url"]
    if preset.get("token"):
        env["ANTHROPIC_AUTH_TOKEN"] = preset["token"]
    m = (preset.get("model") or "").strip()
    if m:
        for k in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                  "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            env.setdefault(k, m)
    return env


# ---- qq 会话权限自动裁决（无人值守，绝不弹窗）----
# "私有记忆物理不挂载"在此真正落地：cwd 挡不住 Read，权限回调挡。
# 白名单：Read 项目内（除 denia/私有/）、Write/Edit 仅 denia/缓冲/、
# Skill 仅 denia-qq；其余工具（Bash/Agent/Web/Glob/Grep/MCP…）一律拒。
# 例外：qq_web_enabled 时放行浏览器子 agent + 它的 daemon 命令（裁决表内判）。
QQ_DENY_MSG = "这个操作在 QQ 分身里不开放"
QQ_DENY_WEB_MSG = ("浏览器开关没开——连接者在 QQ 控制中心开的那个。"
                   "别开其他工具绕路，就告诉对方现在上不了网")
_QQ_PRIVATE_DIR = (PROJECT_ROOT / "denia" / "私有").resolve()
_QQ_BUFFER_DIR = (PROJECT_ROOT / "denia" / "缓冲").resolve()
_QQ_PROJECT = PROJECT_ROOT.resolve()


def _qq_under(path_str, base):
    """Windows 大小写不敏感的路径归属判断；非法路径返回 False。"""
    try:
        p = Path(path_str).resolve()
    except Exception:
        return False
    return os.path.normcase(str(p)).startswith(os.path.normcase(str(base)) + os.sep)


def _qq_tool_verdict(tool_name, input_data):
    """qq 模式 can_use_tool 裁决表。每次裁决记日志。"""
    fp = (input_data or {}).get("file_path") or ""
    if tool_name == "Read":
        if fp and _qq_under(fp, _QQ_PROJECT) and not _qq_under(fp, _QQ_PRIVATE_DIR):
            LOG.info("权限[qq]：Read %s → 放行（项目内非私有）", fp)
            return PermissionResultAllow()
        LOG.info("权限[qq]：Read %s → 拒（私有/项目外）", fp)
        return PermissionResultDeny(message=QQ_DENY_MSG)
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        if fp and _qq_under(fp, _QQ_BUFFER_DIR):
            LOG.info("权限[qq]：%s %s → 放行（缓冲区内）", tool_name, fp)
            return PermissionResultAllow()
        LOG.info("权限[qq]：%s %s → 拒（缓冲区外）", tool_name, fp)
        return PermissionResultDeny(message=QQ_DENY_MSG)
    if tool_name == "Skill":
        if (input_data or {}).get("skill") == "denia-qq":
            LOG.info("权限[qq]：Skill denia-qq → 放行")
            return PermissionResultAllow()
        LOG.info("权限[qq]：Skill %s → 拒（非 denia-qq）",
                 (input_data or {}).get("skill"))
        return PermissionResultDeny(message=QQ_DENY_MSG)
    # 上网开关（qq_web_enabled）：只放行浏览器子 agent 本体 + 它的 daemon 命令。
    # 裁决表活读 presets.json——控制中心拨开关即时生效，不用重建会话。
    if tool_name == "Agent":
        sub = (input_data or {}).get("subagent_type") or ""
        if sub == "browser-operator" and load_qq().get("qq_web_enabled"):
            LOG.info("权限[qq]：Agent browser-operator → 放行（上网开关开）")
            return PermissionResultAllow()
        LOG.info("权限[qq]：Agent %s → 拒（开关关/非浏览器 agent）", sub)
        return PermissionResultDeny(message=QQ_DENY_WEB_MSG)
    if tool_name == "Bash":
        cmd = (input_data or {}).get("command") or ""
        if load_qq().get("qq_web_enabled") and \
                ("daemon-client.py" in cmd or "127.0.0.1:9876" in cmd):
            LOG.info("权限[qq]：Bash（浏览器 daemon 命令）→ 放行")
            return PermissionResultAllow()
        LOG.info("权限[qq]：Bash → 拒（开关关/非浏览器命令）")
        return PermissionResultDeny(message=QQ_DENY_MSG)
    LOG.info("权限[qq]：%s → 拒（公开分身不开放）", tool_name)
    return PermissionResultDeny(message=QQ_DENY_MSG)


class Session:
    """单会话状态：一个 WS 连接 + 一个按需建立/重建的 ClaudeSDKClient。
    mode="chat" 主聊天 / mode="read" 共读会话（同一 WS 上的第二个实例）。"""
    def __init__(self, ws, mode="chat"):
        self.ws = ws
        self.mode = mode
        self.client = None          # 选预设前不建
        self.active_preset = None   # 当前 client 背后的预设
        self.pending_preset = None  # 已选但延迟到下次启动的预设（已有上下文时）
        self.current_model = None   # 当前生效模型名（LiteLLM 桥 set_model 热切换后跟进）
        self.skip_crawl = load_web()["skip_crawl"]  # 「跳过爬取」开关（web 节持久化）
        self.bypass = False         # bypass 模式：工具调用不再弹窗、全部放行
        self.has_context = False    # 仅在回合正常完成后置 True（interrupt/revert 不算）
        self.session_id = None      # 当前 CLI 会话 id（dispatch 里从消息更新；存档/续接依据）
        self.last_user_text = ""    # 最近一条原始用户文本（拼 /denia 前缀之前），做存档备注
        self.recent = []            # 最近对话片段（识图转接的上下文素材）
        self._seg_buf = ""          # 当前气泡段的文本累积（关段时抽 L2 存入 recent）
        self.relay_imgs = []        # 识图转接：当前批图片路径（ask_vision 追问对象）
        self.relay_asks = 0         # 当前批已追问次数（每批上限 VISION_ASKS_PER_MSG）
        # ---- 生图（拍照）----
        self.last_user_imgs = []    # 最近一条用户消息附的 uploads 图（[打卡] 取景）
        self.genimg_tasks = set()   # 后台生图 worker task（诊断+WS 断开时取消）
        self.tts_tasks = set()      # 后台语音 worker task（同上）
        self._genimg_pending = []   # 洗好但会话忙、待附到下条用户消息提示里的照片路径
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
        # ---- 共读专属 ----
        self.read_book = None       # 当前共读的书名（denia/私有/共读/ 下的目录名）
        self.read_pos = {}          # 页跟踪最新上报 {chapter,page,anchor}，纯内存不落盘
        self.last_activity = 0.0    # 用户最近输入时间戳（时钟空闲门槛依据）
        self.clock_task = None      # 泊松时钟 task（仅 read 会话用）
        self._silent_check = False  # 本轮回复先憋首几字，探测 [静默] 标记
        self._silent_buf = ""       # 探测期缓冲的文本

    async def send(self, obj):
        # 共读会话的出向消息统一打 mode 标，前端按此路由到共读聊天流
        if self.mode != "chat":
            obj = {**obj, "mode": self.mode}
        await self.ws.send(json.dumps(obj, ensure_ascii=False))

    # ---- 识图追问工具（进程内 MCP）----
    def _make_vision_server(self):
        """ask_vision 工具：达妮娅对当前批图片追问细节，识图助手针对性回答。
        闭包持有 self —— 追问对象永远是本会话最近一批转接图片。"""
        if sdk_tool is None or create_sdk_mcp_server is None:
            return None

        @sdk_tool("ask_vision",
                  "向识图助手追问图片的细节。两种用法：一、用户刚发来图片、初步描述"
                  "不足以回答你想知道的内容时，对那批图追问（一次问一个具体问题）；"
                  "二、带 path 参数回看你刚拍好的照片（path 为照片的绝对路径）。",
                  {"type": "object",
                   "properties": {
                       "question": {"type": "string",
                                    "description": "一个具体的问题"},
                       "path": {"type": "string",
                                "description": "可选：要问的图片绝对路径"
                                               "（仅限自己拍的照片/用户发过的图）；"
                                               "不带则追问用户最近发来的那批图"}},
                   "required": ["question"]})
        async def ask_vision(args):
            q = (args.get("question") or "").strip()
            cfg = _resolve_vision_relay(load_vision_relay())
            # path 白名单：只放行生图产物/上传缓存里的纯文件名图片（防任意本地文件外泄）
            path = (args.get("path") or "").strip()
            img_paths = self.relay_imgs
            if path:
                p = Path(path).resolve()
                allowed = any(p.parent == d.resolve() and p.is_file()
                              and p.suffix.lower() in IMG_MIME
                              for d in (GENIMG_DIR, UPLOAD_DIR))
                if not allowed:
                    return {"content": [{"type": "text",
                            "text": "这个路径看不了（只允许自己拍好的照片或用户发过的图）。"}],
                            "is_error": True}
                img_paths = [str(p)]
            if not img_paths:
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
                ans = await vision_ask(cfg, img_paths, q)
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
            # LiteLLM 桥预设：代理没在跑就先拉起（取代手动跑 启动-LiteLLM.bat）
            if _is_litellm_preset(preset):
                ok, msg = await ensure_litellm()
                if not ok:
                    await self.send({"type": "preset_error",
                                     "name": preset.get("name"), "msg": msg})
                    return
            env = _preset_sdk_env(preset)
            # 识图转接是全局开关（2026-08-07 起）：开着就挂 ask_vision 追问工具，
            # 不再看预设 vision 字段（视觉模型也可强制走转接，GUI 统一管理）
            mcp_servers = {}
            if _relay_ready(_resolve_vision_relay(load_vision_relay())):
                vision_srv = self._make_vision_server()
                if vision_srv is not None:
                    mcp_servers["vision"] = vision_srv
            # 爬虫子 agent 编程式定义：prompt=agent md 全文，可指定上网模型。
            # 子 agent 继承主会话 env → 指定模型仅在 LiteLLM 桥预设下可路由
            # （别名由桥解析）；直连预设下别名字符串会 404，忽略并告警。
            agents = None
            try:
                bo_prompt = (PROJECT_ROOT / ".claude" / "agents"
                             / "browser-operator.md").read_text(encoding="utf-8")
            except Exception:
                bo_prompt = ""
            if bo_prompt:
                web_model = (load_web().get("model") or "").strip()
                if web_model and not _is_litellm_preset(preset):
                    LOG.warning("上网模型 %s 仅 LiteLLM 桥预设下可路由，本次忽略",
                                web_model)
                    web_model = ""
                agents = {"browser-operator": AgentDefinition(
                    description="达妮娅的浏览器子 agent：想法池爬取与网页操作",
                    prompt=bo_prompt,
                    model=web_model or None)}
                LOG.info("browser-operator 子 agent 已注入：model=%s",
                         web_model or "(跟随主模型)")
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
                agents=agents,
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
            self.current_model = (preset.get("model") or "").strip() or None
            # 续接：上下文已在会话历史里，跳过 /denia 前缀，直接可聊
            self._started = bool(resume)
            self.has_context = bool(resume)
            self.session_id = resume or None
            self.outstanding = 0
            self.seg_open = False
            self.active_tasks.clear()
            # qq 会话的预设来自 qq.preset_id 配置，不该踩掉前端记的 lastSelected
            if self.mode != "qq":
                save_last_selected(preset.get("id"))
            # 启动长驻消费循环（与 client 同 event loop，满足 SDK 单上下文约束）
            self.consumer_task = asyncio.create_task(self.consume_loop())
            LOG.info("build_client 成功，发送 chat_enabled")
            await self.send({"type": "chat_enabled", "preset": preset.get("name"),
                             "preset_id": preset.get("id"),
                             "vision": bool(preset.get("vision", True)),
                             "model": self.current_model,
                             "bridge": _is_litellm_preset(preset),
                             "relay": _relay_ready(_resolve_vision_relay(load_vision_relay()))})
        finally:
            self._building = False

    async def teardown_client(self):
        """关掉当前 client：先停消费循环，再 disconnect。"""
        await self.stop_clock()   # 共读时钟随 client 一起停
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
        self.current_model = None
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
        # qq 会话无人值守：自动裁决，必须先于 bypass/SILENT_TOOLS
        # （SILENT_TOOLS 全局静默放行 Write/Edit，不拦就会把私有记忆写穿）
        if self.mode == "qq":
            return _qq_tool_verdict(tool_name, input_data)
        if self.bypass:
            LOG.info("权限：%s bypass 模式，直接放行", tool_name)
            return PermissionResultAllow()
        if tool_name in SILENT_TOOLS or tool_name.startswith("mcp__vision__"):
            LOG.info("权限：%s 在白名单，静默放行", tool_name)
            return PermissionResultAllow()
        LOG.info("权限：%s 需用户确认，推前端弹窗", tool_name)
        # 非白名单：推前端弹窗，等用户点击
        # req_id 带 mode 前缀（perm-c-N / perm-r-N）：两个会话各自计数也不撞号
        self._req_seq += 1
        req_id = f"perm-{self.mode[0]}-{self._req_seq}"
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
        """命中本会话的等待 Future 则解掉并返回 True，否则 False（调用方再试另一会话）。"""
        fut = self._pending.get(req_id)
        if fut and not fut.done():
            fut.set_result(bool(allow))
            return True
        return False

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
    async def _reset_silent(self, flush=True):
        """回合收尾时结算 [静默] 探测（回合=两次 ResultMessage 之间，可跨多个
        AssistantMessage/工具调用——不能在 AssistantMessage 边界就重置，否则
        skill 加载/工具调用的中间消息会提前杀死探测）。
        整回合只有 [静默] → 丢弃；探测残留实质内容 → 补放行（宁可显示不丢）。"""
        if not self._silent_check:
            return
        buf, self._silent_check, self._silent_buf = self._silent_buf, False, ""
        if _silent_status(buf) == "silent":
            LOG.info("共读：本轮回复 [静默]，整段丢弃")
        elif flush and buf.strip("[]【】［］ \t\r\n"):
            LOG.warning("共读：静默探测残留非静默内容，补放行 %r", buf[:40])
            await self._ensure_segment()
            self._seg_buf += buf
            await self.send({"type": "text_delta", "seg": self.seg_id, "text": buf})

    async def _ensure_segment(self):
        """幂等地开一个达妮娅气泡段（首个 content_block_start / text_delta 时）。"""
        if not self.seg_open:
            self.seg_id += 1
            self.seg_open = True
            self._seg_buf = ""
            await self.send({"type": "segment_start", "seg": self.seg_id})

    async def _close_segment_if_open(self):
        # 纯气泡边界（AssistantMessage/工具调用也会触发）：不动静默探测状态，
        # 探测的生死只在回合收尾（ResultMessage）结算
        if self.seg_open:
            seg = self.seg_id
            self.seg_open = False
            await self.send({"type": "segment_end", "seg": seg})
            # 抽 L2 存入 recent（识图转接的上下文素材；L1 是内心独白不给识图模型看）
            t = self._seg_buf
            if "[L2]" in t:
                t = t.split("[L2]", 1)[1]
            elif "[L1]" in t:
                t = ""
            t = t.strip()
            if t:
                self._remember("达妮娅", t)
                # 拍照标记检出：只在 L2 文本上跑（L1 内心独白里的标记不触发），
                # 每个标记一个后台 worker，不等图（她写完就走）。
                # QQ 会话不在这里跑——桥侧会自己生图投 QQ 并回传 qq_genimg_done，
                # 此处只做交付注入（双烧合一：她回看的图=群友收到的图；顺带关掉
                # "桥权限闸拒了、server 照烧"的旁路，2026-08-19 实烧 8 张的教训）
                if self.mode != "qq":
                    for ord_i, m in enumerate(GENIMG_RE.finditer(t)):
                        task = asyncio.create_task(
                            self._genimg_task(seg, ord_i, m.group(1), m.group(2).strip()))
                        self.genimg_tasks.add(task)
                        task.add_done_callback(self.genimg_tasks.discard)
            self._seg_buf = ""

    # ---- 生图后台 worker（拍照：写标记 → 异步洗照片 → 前端替换占位符）----
    async def _genimg_task(self, seg, ord_i, kind, free_text):
        """跑 tools/生图/gen.py 子进程（成本闸/参考图/提示词都在脚本里）。
        好了发 genimg_done + 交付注入；失败发 genimg_fail（她的上下文不受影响）。"""
        try:
            mode_args = []
            if kind == "改图":
                mode_args = ["--edit"]
            elif kind == "打卡":
                if not self.last_user_imgs:
                    raise RuntimeError("没有可用的照片（[打卡] 需要用户刚发一张图）")
                mode_args = ["--photo", self.last_user_imgs[0]]
            LOG.info("生图 worker：seg=%d ord=%d kind=%s %r", seg, ord_i, kind, free_text[:40])
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(GENIMG_SCRIPT), free_text, *mode_args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), timeout=360)
            # gen.py stdout 单行 JSON（防御：跳过可能的非 JSON 告警行）
            result = None
            for line in out.decode("utf-8", "replace").strip().splitlines():
                line = line.strip()
                if line.startswith("{"):
                    result = json.loads(line)
            if result is None:
                raise RuntimeError(
                    f"gen.py 无结果输出（rc={proc.returncode}）："
                    f"{err.decode('utf-8', 'replace')[-200:]}")
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "生成失败")
            LOG.info("生图完成：%s", result.get("path"))
            await self.send({"type": "genimg_done", "seg": seg, "ord": ord_i,
                             "url": result.get("url")})
            await self._deliver_genimg(result.get("path"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LOG.error("生图失败：%s", e)
            try:
                await self.send({"type": "genimg_fail", "seg": seg, "ord": ord_i,
                                 "msg": str(e)})
            except Exception:
                pass

    async def _deliver_genimg(self, path):
        """"照片洗好啦"交付：闲时注入一条用户侧消息（她可回看并自然提起——
        互动感核心）；忙时或注入失败则挂起，附到下一条用户消息的提示里。"""
        if not path:
            return
        if (not load_genimg().get("deliver_inject", True)
                or self.client is None or self.outstanding > 0):
            self._genimg_pending.append(path)
            return
        hint = (f"[照片洗好了]\n刚才拍的照片洗出来了，保存在：{path}\n"
                f"你可以用 Read 工具看看（或用 ask_vision 带 path 追问细节），"
                f"然后自然地跟我说说拍得怎么样；觉得满意也可以不特意提。")
        LOG.info("生图交付注入：%s", path)
        self.outstanding += 1
        try:
            await self.client.query(self._with_skill_prefix(hint))
        except Exception as e:
            LOG.error("生图交付注入 query 异常：%s", e)
            self.outstanding -= 1
            self._genimg_pending.append(path)

    def _remember(self, who, text):
        """滚动记录最近对话（识图上下文用），单条截断、只留最近 6 条。"""
        self.recent.append(f"{who}：{text[:200]}")
        del self.recent[:-6]

    # ---- 语音后台 worker（喇叭/划线 → 串行合成 → /voice/ 文件）----
    async def _tts_task(self, req_id, text, seg, voice):
        """剥标记 → 确保服务在跑 → 排队合成 → 发 tts_done。
        叫停语义（SOVITS['gen'] 代际）：排队中/合成完未投递的被丢弃；
        正在烧 CPU 的那条让它算完（杀不掉 api_v2 进程内请求），结果不投。"""
        try:
            cfg = load_tts()
            if not cfg.get("enabled"):
                raise RuntimeError("语音未开启（设置 → 语音 打开开关）")
            clean = _tts_clean(text)
            if not clean:
                raise RuntimeError("这段没有可读的文字")
            pre = await asyncio.to_thread(_tts_preprocess, clean, load_tts_pre())
            if pre:
                clean = pre   # 朗读化成功则用它合成（缓存键也是这份文本）
            LOG.info("语音 worker：req=%s seg=%s voice=%s pre=%s %r",
                     req_id, seg, voice, bool(pre), clean[:40])
            gen = SOVITS["gen"]
            ok, msg = await ensure_tts()
            if not ok:
                raise RuntimeError(msg)
            async with TTS_LOCK:
                if SOVITS["gen"] != gen:
                    return   # 排队期间被叫停
                out = await asyncio.to_thread(_tts_synth_or_cache, cfg, clean)
                if SOVITS["gen"] != gen:
                    return   # 合成完但已被叫停，结果丢弃
            LOG.info("语音完成：%s", out.name)
            await self.send({"type": "tts_done", "req": req_id, "seg": seg,
                             "voice": voice, "url": f"/voice/{out.name}"})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LOG.error("语音失败：%s", e)
            try:
                await self.send({"type": "tts_fail", "req": req_id, "seg": seg,
                                 "voice": voice, "msg": str(e)})
            except Exception:
                pass

    async def _maybe_unlock(self):
        """所有待响应 query 都收尾 → 解锁输入框。"""
        if self.outstanding == 0:
            await self.send({"type": "input_unlock", "reason": "idle"})

    # ---- 共读泊松时钟（外部时钟注入，SKILL.md 空窗期第三件事）----
    # 指数间隔抽签（泊松流无记忆性：忙/不空闲就放弃本轮重新抽签，不补偿）。
    # 仅用户停留在共读视图时运行（exit_read / WS 断开即停）。
    def _with_skill_prefix(self, text):
        """本会话首轮 query 前补 skill 前缀（主 /denia，共读 /denia-read，QQ /denia-qq）。
        chat/read_highlight/clock_inject/qq chat 四条入口都可能是首轮，统一在此兜底。"""
        if not self._started:
            if self.mode == "read":
                prefix = "/denia-read\n"
            elif self.mode == "qq":
                prefix = "/denia-qq\n"   # qq 永不爬网：skill 自身声明无子 agent
            else:
                prefix = "/denia [NO_CRAWL]\n" if self.skip_crawl else "/denia\n"
            text = prefix + text
            self._started = True
        return text

    async def start_clock(self):
        await self.stop_clock()
        self.clock_task = asyncio.create_task(self.clock_loop())

    async def stop_clock(self):
        if self.clock_task and not self.clock_task.done():
            self.clock_task.cancel()
            try:
                await self.clock_task
            except (asyncio.CancelledError, Exception):
                pass
        self.clock_task = None

    async def clock_loop(self):
        LOG.info("共读时钟启动（%s）", self.read_book or "未选书")
        try:
            while True:
                cfg = load_coread_config()   # 每轮重读，set_coread_config 热更新
                if not cfg.get("clock_enabled", True):
                    await asyncio.sleep(60)
                    continue
                # 下限 5 秒：冒烟可用 clock_mean_min≈0.1 加速验证
                mean_s = max(5.0, float(cfg.get("clock_mean_min", 20)) * 60)
                idle_s = float(cfg.get("idle_gate_min", 10)) * 60
                await asyncio.sleep(random.expovariate(1.0 / mean_s))
                if self.client is None:
                    continue
                if self.outstanding > 0:
                    LOG.info("时钟：会话忙，跳过本轮")
                    continue
                if time.time() - self.last_activity < idle_s:
                    LOG.info("时钟：用户不空闲，跳过本轮")
                    continue
                await self.clock_inject()
        except asyncio.CancelledError:
            LOG.info("共读时钟取消")
            raise

    async def clock_inject(self):
        """把"想说点什么的时刻"作为一条用户侧消息注入；她可回 [静默] 保持安静。"""
        pos = self.read_pos or {}
        pos_txt = ""
        if pos.get("page"):
            pos_txt = f"（我最近看到原书第{pos['page']}页"
            if pos.get("anchor"):
                pos_txt += f" [{pos['anchor']}]"
            pos_txt += " 附近）"
        hint = (f"[时钟注入]\n"
                f"离我上次说话已经过去好一会儿了，我还在读《{self.read_book or '?'}》{pos_txt}。\n"
                f"这时候如果你想到了什么想说的——一个疑问、一个联想、吐槽这页太密，"
                f"都可以自然开口；没什么想说的，就只回复 [静默]，别的什么都别写。")
        LOG.info("时钟注入：%s", hint.splitlines()[1][:40])
        self._silent_check = True
        self._silent_buf = ""
        self.outstanding += 1
        try:
            await self.client.query(self._with_skill_prefix(hint))
        except Exception as e:
            LOG.error("时钟注入 query 异常：%s", e)
            self._silent_check = False
            self._silent_buf = ""
            self.outstanding -= 1

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
            await self._reset_silent()   # 意外收尾也结算探测，残留补放行不丢内容
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
                # 共读静默探测期：先不开气泡，等首几字判定不是 [静默] 再放行
                if not (self.mode == "read" and self._silent_check):
                    await self._ensure_segment()
            elif t == "content_block_delta":
                d = ev.get("delta", {})
                if d.get("type") == "text_delta":
                    if self.mode == "read" and self._silent_check:
                        # [静默] 前缀探测：憋住首几字。仍是"静默"的前缀→继续憋；
                        # 确定不是→放行已缓冲内容；确定是→整段丢弃（关段时清）
                        self._silent_buf += d.get("text", "")
                        if _silent_status(self._silent_buf) == "no":
                            self._silent_check = False
                            buf, self._silent_buf = self._silent_buf, ""
                            await self._ensure_segment()
                            self._seg_buf += buf
                            await self.send({"type": "text_delta",
                                             "seg": self.seg_id, "text": buf})
                        return
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
            # 回合收尾：结算 [静默] 探测（interrupt 回合不补放行残留，直接清）
            await self._reset_silent(flush=not self._interrupted)
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
                    update_last_session(self.session_id, self.active_preset, self.mode)
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


_SILENT_CORE = "静默"


def _silent_status(buf):
    """[静默] 探测：剥掉空白和各种方括号后看是不是"静默"的前缀。
    返回 'pending'（还可能是，继续憋）/ 'silent'（已集齐，整段丢弃）/ 'no'（不是，放行）。
    兼容全角 ［静默］；若"静默"之后又冒出别的字，回落 'no' 连缓冲一起放行。"""
    t = "".join(ch for ch in buf if not ch.isspace() and ch not in "[]【】［］")
    if not t:
        return "pending"
    if _SILENT_CORE.startswith(t):
        return "silent" if t == _SILENT_CORE else "pending"
    return "no"


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
    # 共读时钟注入（主动对话触发词），非真人发言
    "[时钟注入]",
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
    # 口令闸（access_token 非空时）：WS 不能带自定义头，key 走 query（ws://…/ws?key=…）
    # 本机回环直通（与 HTTP 闸一致：本地 bat 进不该弹锁屏）
    tok = _gui_token()
    peer = (ws.remote_address or ("",))[0]
    if tok and peer not in ("127.0.0.1", "::1"):
        try:
            q = parse_qs(urlsplit(ws.request.path).query)
            key = (q.get("key") or [""])[0]
        except Exception:
            key = ""
        if not hmac.compare_digest(key, tok):
            LOG.warning("WS 口令错误/缺失，拒绝连接")
            try:
                await ws.send(json.dumps({"type": "auth_failed"}))
                await ws.close(4001, "bad key")
            except Exception:
                pass
            return
    LOG.info("WS 连接建立（预设未选，聊天禁用）")
    chat_session = Session(ws, mode="chat")
    read_session = None          # 共读会话：懒建（首次 enter_read / mode=read 消息）
    qq_session = None            # QQ 会话：懒建（桥客户端首发 qq_init / mode=qq 消息）
    # QQ 桥客户端标识（?client=qq）：跳过主聊天自动连接，不白起闲置 CLI 子进程。
    # 控制台页（?client=console）同理——它只发 qq_* 指令，不需要聊天会话
    is_qq_client = False
    try:
        is_qq_client = (parse_qs(urlsplit(ws.request.path).query)
                        .get("client") or [""])[0] in ("qq", "console")
    except Exception:
        pass
    try:
        # 连上先推 picker 状态；有上次选中的预设就直接自动连接
        # （LiteLLM 桥预设会先自动拉起代理，手动批处理已成历史）
        await chat_session.send({"type": "connected"})
        last = find_preset(load_last_selected())
        await chat_session.send({
            "type": "presets",
            "list": [_safe_preset(p) for p in load_presets()],
            "selected": load_last_selected(),
            "skip_crawl": chat_session.skip_crawl,
            "web": load_web(),
            "autoconnect": bool(last) and not is_qq_client,
        })
        await chat_session.send(await _litellm_state_msg(chat_session))
        if last and not is_qq_client:
            await chat_session.build_client(last)
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                LOG.warning("收到非 JSON 消息，忽略：%r", raw[:120])
                continue
            if not isinstance(data, dict):
                # 浏览器端偶发 "null"/标量帧：忽略即可，绝不能崩掉整条 WS
                LOG.warning("忽略非对象 JSON 帧：%r", raw[:120])
                continue
            t = data.get("type")
            # 路由：共读专属类型或显式 mode=read → read 会话；
            # 桥专属类型或显式 mode=qq → qq 会话；其余 → 主聊天会话
            session = chat_session
            if t in READ_MSG_TYPES or data.get("mode") == "read":
                if read_session is None:
                    read_session = Session(ws, mode="read")
                    LOG.info("共读会话实例已创建")
                session = read_session
            elif t in QQ_MSG_TYPES or data.get("mode") == "qq":
                if qq_session is None:
                    qq_session = Session(ws, mode="qq")
                    QQ_STATE.update(session=qq_session, ws=ws)
                    LOG.info("QQ 会话实例已创建（登记控制中心注册表）")
                session = qq_session
            LOG.info("← 前端消息 type=%s mode=%s", t, session.mode)

            # ---- 控制中心（/qq 页）端点：不进 QQ_MSG_TYPES，由控制台普通
            # 连接发，操作的是 QQ_STATE 注册表里的桥会话 ----
            if t == "qq_genimg_done":
                # 桥侧生图完成回报（双烧合一：server 不为 qq 会话跑 gen.py，
                # 桥生完发 QQ 后回传产物路径，这里只做交付注入——她回看的图
                # 就是群友收到的那张）。失败则什么都不注入（桥的回执会告诉她）
                s = _qq_session()
                if s is None:
                    LOG.warning("qq_genimg_done 丢弃：QQ 会话不在册")
                elif data.get("ok") and data.get("path"):
                    await s._deliver_genimg(str(data["path"]))
                else:
                    LOG.info("桥侧生图未成交：%s", data.get("error") or "")
                continue
            if t == "qq_status":
                st = await qqbridge_status()
                s = _qq_session()
                qqcfg = load_qq()
                tcfg = load_tts()
                tst = await tts_status()
                try:
                    aliases = [m.get("model_name")
                               for m in (litellm_read_models() or [])]
                except Exception:
                    aliases = []
                await session.send({"type": "qq_status", **st,
                    "starting": QQBRIDGE["starting"],
                    "enabled": bool(qqcfg.get("enabled")),
                    "model": (s.current_model if s else None),
                    "proactive": bool(qqcfg.get("qq_proactive_enabled")),
                    "dnd": bool(qqcfg.get("qq_dnd")),
                    "web": bool(qqcfg.get("qq_web_enabled")),
                    "voice_backend": tcfg.get("backend"),
                    "voice_enabled": bool(tcfg.get("enabled")),
                    "voice_running": bool(tst.get("running")),
                    "voice_backends": (_safe_tts(tcfg) or {}).get("backends") or [],
                    "genimg_today": _genimg_today_count(),
                    "genimg_limit": int(load_genimg().get("daily_limit") or 0),
                    "genimg_model": load_genimg().get("model"),
                    "look_model": (load_vision_relay() or {}).get("model"),
                    "glance_model": (_resolve_vision_glance() or {}).get("model"),
                    "litellm_aliases": aliases,
                    "napcat": _napcat_state()})
                continue
            if t == "qq_start":
                ok, msg = await qqbridge_start()
                await session.send({"type": "qq_start_result",
                                    "ok": ok, "msg": msg})
                continue
            if t == "qq_stop":
                ok, msg = await qqbridge_stop()
                await session.send({"type": "qq_stop_result",
                                    "ok": ok, "msg": msg})
                continue
            if t == "qq_set_model":
                # target=main：LiteLLM 别名下拉，经注册表直达桥会话 set_model，
                # 不清零不换预设（只活会话生效，桥重启回预设默认）。
                # target=genimg/glance/look：写 presets 对应节，热生效
                # （gen.py 每次子进程现读配置；识图端点同理现读）。
                target = (data.get("target") or "main").strip()
                alias = (data.get("model") or "").strip()
                err = None
                if not alias:
                    err = "模型名为空"
                elif target == "main":
                    s = _qq_session()
                    if s is None or s.client is None:
                        err = "分身不在线"
                    elif s.outstanding > 0:
                        err = "她正在回应，稍后再切"
                    elif not _is_litellm_preset(s.active_preset):
                        err = "QQ 预设不走 LiteLLM 桥"
                    else:
                        try:
                            await s.client.set_model(alias)
                            s.current_model = alias
                            LOG.info("qq_set_model → %s", alias)
                        except Exception as e:
                            err = f"切换失败：{type(e).__name__}"
                elif target == "genimg":
                    save_genimg({**load_genimg(), "model": alias})
                elif target == "look":
                    save_vision_relay({**(load_vision_relay() or {}),
                                       "model": alias})
                elif target == "glance":
                    obj = _read_presets_file()
                    g = obj.get("vision_glance") or {}
                    g["model"] = alias
                    obj["vision_glance"] = g
                    _write_presets_file(obj)
                else:
                    err = f"未知 target：{target}"
                await session.send({"type": "qq_model_set", "target": target,
                                    "model": alias, "error": err})
                continue
            if t == "qq_set_modes":
                # 行为开关：写 qq 节 + 推 qq_reload_cfg 让桥热重载（投递点
                # 活读 cfg，不用重启桥、不掉会话）。桥不在就只写配置，
                # 下次启动自然生效。web=分身上网开关（裁决表活读，即时生效）。
                patch = {}
                if "proactive" in data:
                    patch["qq_proactive_enabled"] = bool(data.get("proactive"))
                if "dnd" in data:
                    patch["qq_dnd"] = bool(data.get("dnd"))
                if "web" in data:
                    patch["qq_web_enabled"] = bool(data.get("web"))
                cfg = save_qq(patch)
                pushed = False
                bws = QQ_STATE.get("ws")
                if bws is not None:
                    try:
                        await bws.send(json.dumps(
                            {"type": "qq_reload_cfg", "mode": "qq"}))
                        pushed = True
                    except Exception as e:
                        LOG.warning("qq_reload_cfg 推送失败：%s", e)
                LOG.info("qq_set_modes：proactive=%s dnd=%s web=%s（热重载=%s）",
                         cfg.get("qq_proactive_enabled"), cfg.get("qq_dnd"),
                         cfg.get("qq_web_enabled"), pushed)
                await session.send({"type": "qq_modes_set",
                                    "proactive": bool(cfg.get("qq_proactive_enabled")),
                                    "dnd": bool(cfg.get("qq_dnd")),
                                    "web": bool(cfg.get("qq_web_enabled")),
                                    "hot": pushed})
                continue
            if t == "qq_set_voice":
                # 语音后端切换：先停旧后端 managed 进程（tts_stop 读当前配置，
                # 必须在写新 backend 之前调），再写 tts.backend。新后端懒启动
                # ——首次合成时 ensure_tts 拉起，不在这里烧冷启时间。
                backend = (data.get("backend") or "").strip()
                err = None
                if backend not in TTS_BACKENDS:
                    err = f"未知语音后端：{backend}"
                else:
                    cur = load_tts()
                    old = cur.get("backend")
                    if old and old != backend and cur.get("managed", True):
                        ok, msg = await tts_stop()
                        if not ok and "不是 GUI 启动的" not in (msg or ""):
                            LOG.warning("旧语音后端停止失败（照常切换）：%s", msg)
                    obj = _read_presets_file()
                    root = obj.get("tts") or {}
                    root["backend"] = backend
                    root["enabled"] = bool(root.get("enabled", True))
                    obj["tts"] = root
                    _write_presets_file(obj)
                    LOG.info("qq_set_voice：%s → %s", old, backend)
                await session.send({"type": "qq_voice_set",
                                    "ok": err is None, "error": err,
                                    "backend": backend})
                continue
            if t == "qq_reset_session":
                # 重置 QQ 会话：清掉 last_qq_session 续接指针（jsonl 记录保留在
                # 磁盘，只是不再接回），拆掉当前 client 并用预设建全新会话——
                # 新会话首轮会重发 /denia-qq 前缀，skill 新规随之生效。
                obj = _read_checkpoints_file()
                obj["last_qq_session"] = None
                _write_checkpoints_file(obj)
                err = None
                s = _qq_session()
                if s is None:
                    err = "桥未连入"
                else:
                    try:
                        await s.teardown_client()
                        cfg = load_qq()
                        preset = None
                        pid = (cfg.get("preset_id") or "").strip()
                        if pid:
                            preset = find_preset(pid)
                        if preset is None:
                            preset = find_preset(load_last_selected())
                        if preset is None:
                            err = "找不到可用预设"
                        else:
                            await s.build_client(preset)   # 全新会话（不 resume）
                            LOG.info("qq_reset_session：已开全新 QQ 会话")
                    except Exception as e:
                        err = f"重置失败：{type(e).__name__}"
                        LOG.error("qq_reset_session 异常：%s", e)
                await session.send({"type": "qq_reset_result",
                                    "ok": err is None, "error": err})
                continue
            if t == "qq_archive_now":
                # 现在整理记忆：给桥推 qq_inject 帧 → 桥注入维护提示走正常
                # 投递通道（debounce/门闩全套），她跑写入协议后回执发给连接者
                bws = QQ_STATE.get("ws")
                err = None
                if bws is None or not _qq_online():
                    err = "分身不在线"
                else:
                    try:
                        await bws.send(json.dumps({
                            "type": "qq_inject", "mode": "qq",
                            "text": "（系统提示：他刚在控制台按了「现在整理记忆」。"
                                    "请按写入协议把最近值得记的事收进公开记忆/"
                                    "用户档案/名片该更新的节，整理完简单跟他说一声"
                                    "收好了什么；没什么可记的就直说没什么可记的。）"},
                            ensure_ascii=False))
                    except Exception as e:
                        err = f"注入失败：{type(e).__name__}"
                LOG.info("qq_archive_now：%s", err or "已注入")
                await session.send({"type": "qq_archive_result", "error": err})
                continue

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
                    session.last_user_imgs = imgs   # [打卡] 取景：本会话最近的用户照片
                # 洗好待交付的照片（交付注入时正忙/失败的兜底）：附进这条消息的提示
                if session._genimg_pending:
                    pend, session._genimg_pending = session._genimg_pending, []
                    LOG.info("生图交付随消息附带：%s", "；".join(pend))
                    lines = "\n".join(f"  - {p}" for p in pend)
                    note = (f"（系统提示：之前拍的照片已经洗好了，保存在：\n{lines}\n"
                            f"你可以用 Read 工具或 ask_vision(path=...) 查看。）")
                    text = f"{text}\n\n{note}" if text else note
                if imgs:
                    relay_cfg = _resolve_vision_relay(load_vision_relay())
                    # 识图转接全局化（2026-08-07）：开关一开所有图片都走转接，
                    # 不再看主模型 preset.vision 是否原生支持（_relay_ready 含 enabled）
                    if _relay_ready(relay_cfg):
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
                # 首轮加载 skill：主聊天 /denia（skip_crawl 时注入 [NO_CRAWL]），共读 /denia-read
                text = session._with_skill_prefix(text)

                # 共读轮次：记活动时间（时钟空闲门槛）+ 开启 [静默] 前缀探测
                if session.mode == "read":
                    session.last_activity = time.time()
                    session._silent_check = True
                    session._silent_buf = ""

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
                if not session.resolve_permission(data.get("req_id"), data.get("allow")):
                    # req_id 可能属于另一个会话（前端单弹窗不区分 mode）
                    for other in (chat_session, read_session, qq_session):
                        if other is not None and other is not session:
                            if other.resolve_permission(data.get("req_id"),
                                                        data.get("allow")):
                                break

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
                    "web": load_web(),
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
                    if (session.client is not None and session.active_preset
                            and session.active_preset.get("id") == preset.get("id")):
                        # 重选当前预设 = 无操作（重建会白拆 client，chat_enabled
                        # 重发还会把用户正开着的设置面板关掉）
                        LOG.info("select_preset：与当前预设相同（%s），跳过重建",
                                 preset.get("name"))
                    else:
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
                    "web": load_web(),
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
                                    "cfg": _safe_vision_relay(load_vision_relay()),
                                    "vpresets": [_safe_vision_preset(p)
                                                 for p in load_vision_presets()]})

            elif t == "save_vision_relay":
                saved = save_vision_relay(data.get("cfg") or {})
                LOG.info("save_vision_relay：enabled=%s model=%s preset_id=%s",
                         saved.get("enabled"), saved.get("model"),
                         saved.get("preset_id"))  # 不记 token
                await session.send({"type": "vision_relay",
                                    "cfg": _safe_vision_relay(saved), "saved": True,
                                    "vpresets": [_safe_vision_preset(p)
                                                 for p in load_vision_presets()]})

            elif t == "save_vision_preset":
                saved = upsert_vision_preset(data.get("preset") or {})
                LOG.info("save_vision_preset：%s", saved.get("name"))
                await session.send({
                    "type": "vision_presets",
                    "list": [_safe_vision_preset(p)
                             for p in load_vision_presets()],
                })

            elif t == "delete_vision_preset":
                vpid = data.get("id")
                delete_vision_preset(vpid)
                await session.send({
                    "type": "vision_presets",
                    "list": [_safe_vision_preset(p)
                             for p in load_vision_presets()],
                })
                # preset_id 可能已被清 → 刷新 vision_relay 视图
                await session.send({
                    "type": "vision_relay",
                    "cfg": _safe_vision_relay(load_vision_relay()),
                    "vpresets": [_safe_vision_preset(p)
                                 for p in load_vision_presets()],
                })

            # ---- 生图（拍照）配置 ----
            elif t == "get_genimg":
                await session.send({"type": "genimg",
                                    "cfg": _safe_genimg(load_genimg())})

            elif t == "save_genimg":
                saved = save_genimg(data.get("cfg") or {})
                LOG.info("save_genimg：enabled=%s model=%s",  # 不记 token
                         saved.get("enabled"), saved.get("model"))
                await session.send({"type": "genimg",
                                    "cfg": _safe_genimg(saved), "saved": True})

            # ---- 语音（TTS）：配置 + 合成 + 叫停 ----
            elif t == "get_tts":
                st = await tts_status()
                await session.send({"type": "tts",
                                    "cfg": _safe_tts(load_tts()),
                                    "pre": _safe_tts_pre(load_tts_pre()), **st})

            elif t == "save_tts":
                payload = data.get("cfg") or {}
                saved = save_tts(payload)
                pre = (save_tts_pre(payload["pre"]) if isinstance(payload.get("pre"), dict)
                       else load_tts_pre())
                LOG.info("save_tts：enabled=%s backend=%s pre=%s", saved.get("enabled"),
                         saved.get("backend"), pre.get("enabled"))
                # 开关直接驱动服务生命周期（LiteLLM 同款全托管）；云 backend 无进程只校验
                err = None
                if saved.get("enabled"):
                    if saved.get("managed", True):
                        ok, err = await tts_start(saved)
                    else:
                        ok, err = await ensure_tts()
                else:
                    if saved.get("managed", True):
                        ok, err = await tts_stop()
                        if not ok and "不是 GUI 启动的" in (err or ""):
                            ok, err = True, None   # 外部实例不收，但开关照样关
                    else:
                        ok = True
                st = await tts_status(saved)
                await session.send({"type": "tts", "cfg": _safe_tts(saved),
                                    "pre": _safe_tts_pre(pre),
                                    "saved": ok, "error": None if ok else err, **st})

            elif t == "tts_speak":
                task = asyncio.create_task(session._tts_task(
                    data.get("req"), data.get("text") or "",
                    data.get("seg"), bool(data.get("voice"))))
                session.tts_tasks.add(task)
                task.add_done_callback(session.tts_tasks.discard)

            elif t == "tts_stop":
                SOVITS["gen"] += 1   # 代际+1：排队中/未投递的合成全部作废
                await session.send({"type": "tts_stopped"})

            # ---- LiteLLM 本地桥管理 ----
            elif t == "litellm_get":
                await session.send(await _litellm_state_msg(session))

            elif t == "litellm_start":
                ok, msg = await litellm_start()
                await session.send(await _litellm_state_msg(
                    session, None if ok else msg))

            elif t == "litellm_stop":
                ok, msg = await litellm_stop()
                await session.send(await _litellm_state_msg(
                    session, None if ok else msg))

            elif t == "litellm_save_models":
                if session.outstanding > 0:
                    await session.send(await _litellm_state_msg(
                        session, "达妮娅正在回应，等她说完再保存"))
                else:
                    try:
                        models = data.get("models") or []
                        # 活会话正在用的别名被删 → 拒绝（先切别的模型再来）
                        cur = session.current_model
                        if (session.client is not None
                                and _is_litellm_preset(session.active_preset)
                                and cur
                                and cur not in {m.get("model_name") for m in models}):
                            raise ValueError(f"当前正在用 {cur}，不能删（先切别的模型）")
                        litellm_write_models(models)
                        if await _port_open(LITELLM_HOST, LITELLM_PORT):
                            ok, msg = await litellm_restart()
                            if not ok:
                                raise RuntimeError(msg)
                        await session.send(await _litellm_state_msg(session, saved=True))
                    except Exception as e:
                        LOG.error("litellm_save_models 失败：%s", e)
                        await session.send(await _litellm_state_msg(session, str(e)))

            elif t == "set_model":
                alias = (data.get("model") or "").strip()
                err = None
                if session.client is None:
                    err = "尚未连接模型"
                elif session.outstanding > 0:
                    err = "正在回应，稍后再切"
                elif not _is_litellm_preset(session.active_preset):
                    err = "当前预设不走 LiteLLM 桥"
                if err:
                    await session.send({"type": "model_set",
                                        "model": session.current_model, "error": err})
                else:
                    try:
                        await session.client.set_model(alias)
                        session.current_model = alias
                        # 写回预设 model 字段，重启/重建 client 后保持
                        if session.active_preset:
                            session.active_preset = upsert_preset(
                                {**session.active_preset, "model": alias})
                        LOG.info("set_model → %s", alias)
                        await session.send({"type": "model_set", "model": alias})
                    except Exception as e:
                        LOG.error("set_model(%s) 失败：%s", alias, e)
                        await session.send({"type": "model_set",
                                            "model": session.current_model,
                                            "error": f"切换失败：{type(e).__name__}"})

            elif t == "set_skip_crawl":
                session.skip_crawl = bool(data.get("on"))
                save_web({"skip_crawl": session.skip_crawl})
                LOG.info("set_skip_crawl=%s", session.skip_crawl)
                await session.send({"type": "skip_crawl_set", "on": session.skip_crawl})

            elif t == "set_web_model":
                # 上网模型（爬虫子 agent 用）："" = 跟随主模型；非空须是 LiteLLM 别名。
                # agents 在 build_client 时注入 → 下次连接/切预设才生效，不拆活会话。
                alias = (data.get("model") or "").strip()
                if alias:
                    names = [m.get("model_name")
                             for m in (litellm_read_models() or [])]
                    if alias not in names:
                        await session.send({"type": "web_set", "model": load_web()["model"],
                                            "error": f"别名不存在：{alias}"})
                        continue
                save_web({"model": alias})
                LOG.info("set_web_model=%s", alias or "(跟随主模型)")
                await session.send({"type": "web_set", "model": alias})

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
                    "last_read_session": _checkpoint_view(
                        {**obj["last_read_session"], "hint": "上次共读（自动记录）"}
                        if obj.get("last_read_session") else None),
                    "last_qq_session": _checkpoint_view(
                        {**obj["last_qq_session"], "hint": "上次QQ会话（自动记录）"}
                        if obj.get("last_qq_session") else None),
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
                        session.session_id, session.last_user_text,
                        session.active_preset, mode=session.mode)
                    LOG.info("save_checkpoint：mode=%s session=%s hint=%r",
                             session.mode, session.session_id,
                             session.last_user_text[:20])
                    await session.send({
                        "type": "checkpoint_saved",
                        "list": [_checkpoint_view(c) for c in obj["checkpoints"]],
                    })

            elif t == "resume_checkpoint":
                cp = find_checkpoint(data.get("id"), session.mode)
                if not cp or not cp.get("session_id"):
                    await session.send({"type": "checkpoint_error", "msg": "存档不存在"})
                elif cp.get("mode", "chat") != session.mode:
                    await session.send({"type": "checkpoint_error",
                                        "msg": "这条存档属于另一个模式，请切换后再续接"})
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
                        if session.mode == "read":
                            await session.start_clock()

            elif t == "delete_checkpoint":
                obj = delete_checkpoint_record(data.get("id"))
                await session.send({
                    "type": "checkpoints",
                    "list": [_checkpoint_view(c) for c in obj["checkpoints"]],
                    "last_session": _checkpoint_view(
                        {**obj["last_session"], "hint": "上次会话（自动记录）"}
                        if obj.get("last_session") else None),
                    "last_read_session": _checkpoint_view(
                        {**obj["last_read_session"], "hint": "上次共读（自动记录）"}
                        if obj.get("last_read_session") else None),
                    "last_qq_session": _checkpoint_view(
                        {**obj["last_qq_session"], "hint": "上次QQ会话（自动记录）"}
                        if obj.get("last_qq_session") else None),
                })

            # ---- QQ 桥接（tools/qq-bridge/bridge.py 客户端）----
            elif t == "qq_vision":
                # 桥侧代看请求（四通道识图）：不占会话、不等 ready，直接转发
                # 识图 API。Glance 压缩+选便宜模型在 _handle_qq_vision 里。
                await _handle_qq_vision(session, data)

            elif t == "qq_voice":
                # 桥侧语音请求（[语音:正文] → TTS → record 段）：不占会话、
                # 不等 ready。权限/日帽在桥侧，合成复用 GUI 语音管线。
                await _handle_qq_voice(session, data)

            elif t == "qq_init":
                # 桥连入后的初始化：选预设 → 有 last_qq_session 且 jsonl 还在就续接，
                # 否则全新会话。成功发 chat_enabled(mode=qq)（build_client 内），
                # 桥侧等这帧置 ready 才开始投递 QQ 消息。
                if session.client is not None:
                    LOG.info("qq_init：client 已在，跳过重建（幂等）")
                    await session.send({"type": "chat_enabled",
                                        "preset": (session.active_preset or {}).get("name"),
                                        "preset_id": (session.active_preset or {}).get("id"),
                                        "model": session.current_model})
                    continue
                cfg = load_qq()
                preset = None
                pid = (cfg.get("preset_id") or "").strip()
                if pid:
                    preset = find_preset(pid)
                    if preset is None:
                        LOG.warning("qq_init：qq.preset_id=%s 找不到，回落 lastSelected",
                                    pid)
                if preset is None:
                    preset = find_preset(load_last_selected())
                cp = find_checkpoint("last", "qq")
                if (cp and cp.get("session_id") and cp.get("preset")
                        and _session_jsonl_exists(cp.get("session_id"))):
                    LOG.info("qq_init：续接 last_qq_session %s", cp["session_id"])
                    await session.build_client(cp["preset"],
                                               resume=cp["session_id"])
                else:
                    if cp and cp.get("session_id"):
                        LOG.warning("qq_init：last_qq_session 失效"
                                    "（jsonl 不在或缺预设快照），开新会话")
                    if preset is None:
                        await session.send({"type": "need_preset"})
                    else:
                        await session.build_client(preset)

            # ---- 共读模式 ----
            elif t == "enter_read":
                # 进入共读：建/复用共读 client（用当前选中预设），推书单状态，启动时钟
                cfg = load_coread_config()
                book = data.get("book") or cfg.get("last_book")
                if book and not _resolve_book_dir(book):
                    book = None
                session.read_book = book
                session.last_activity = time.time()
                if session.client is None:
                    preset = find_preset(load_last_selected())
                    if preset:
                        await session.build_client(preset)
                    else:
                        await session.send({"type": "need_preset"})
                await session.send({"type": "read_state",
                                    "enabled": session.client is not None,
                                    "book": session.read_book,
                                    "clock": load_coread_config()})
                if session.client is not None:
                    await session.start_clock()

            elif t == "exit_read":
                # 切回主聊天：只停时钟，client 挂起保上下文（再进共读不用重来）
                await session.stop_clock()
                await session.send({"type": "read_suspended"})

            elif t == "select_book":
                book = data.get("book")
                if _resolve_book_dir(book):
                    session.read_book = book
                    save_coread_config({"last_book": book})
                    LOG.info("共读书籍：%s", book)
                await session.send({"type": "read_state",
                                    "enabled": session.client is not None,
                                    "book": session.read_book,
                                    "clock": load_coread_config()})

            elif t == "read_highlight":
                # 划线注入：引用块带 P 锚点发给她（SKILL.md 划线协议 用户→她）
                if session.client is None:
                    await session.send({"type": "need_preset"})
                elif session.outstanding > 0:
                    await session.send({"type": "busy"})
                else:
                    anchor = (data.get("anchor") or "").strip()
                    page = data.get("page")
                    htext = (data.get("text") or "").strip()
                    if htext:
                        book = session.read_book or data.get("book") or "?"
                        session.last_activity = time.time()
                        session.read_pos = {"chapter": data.get("chapter"),
                                            "page": page, "anchor": anchor}
                        inject = (f"（我在《{book}》里划了一句）\n"
                                  f"> [{anchor}]（原书第{page}页）{htext}")
                        session._silent_check = True
                        session._silent_buf = ""
                        session.outstanding += 1
                        LOG.info("划线注入：[%s] p.%s %d 字", anchor, page, len(htext))
                        try:
                            await session.client.query(session._with_skill_prefix(inject))
                        except Exception as e:
                            LOG.error("划线注入 query 异常：%s", e)
                            session._silent_check = False
                            session._silent_buf = ""
                            session.outstanding -= 1
                            await session.send(
                                {"type": "error", "msg": f"{type(e).__name__}: {e}"})

            elif t == "page_ping":
                # 页跟踪上报：只更新内存 read_pos（时钟注入的位置上下文），
                # 不 query 不落盘——滚动零 token；进度.json 由她自己 Edit
                session.read_pos = {"chapter": data.get("chapter"),
                                    "page": data.get("page"),
                                    "anchor": data.get("anchor")}

            elif t == "get_notes" or t == "append_note":
                d = _resolve_book_dir(data.get("book") or session.read_book)
                if t == "append_note":
                    ntext = (data.get("text") or "").strip()
                    if d and ntext:
                        with (d / "笔记.md").open("a", encoding="utf-8") as f:
                            f.write(f"\n- {ntext}\n")
                        LOG.info("笔记追加（%s）：%r", d.name, ntext[:40])
                text = tidy = ""
                if d and (d / "笔记.md").is_file():
                    text = (d / "笔记.md").read_text(encoding="utf-8")
                if d and (d / "笔记-整理.md").is_file():
                    tidy = (d / "笔记-整理.md").read_text(encoding="utf-8")
                await session.send({"type": "note_list", "book": d.name if d else None,
                                    "text": text, "tidy": tidy})

            elif t == "organize_notes":
                # AI 整篇整理：读碎笔记 → 按章归拢成稿 → 写 笔记-整理.md（原始碎笔记原样保留）。
                # 走 [L0整理笔记] 系统指令穿透 skill（硬编码兜底，跳过 L1/L2，同 [L0共读归档]）。
                if session.client is None:
                    await session.send({"type": "need_preset"})
                elif session.outstanding > 0:
                    await session.send({"type": "busy"})
                else:
                    book = session.read_book or data.get("book") or "?"
                    session.last_activity = time.time()
                    session.outstanding += 1
                    LOG.info("整理笔记指令（%s）", book)
                    try:
                        await session.client.query(
                            session._with_skill_prefix("[L0整理笔记]"))
                    except Exception as e:
                        LOG.error("整理笔记 query 异常：%s", e)
                        session.outstanding -= 1
                        await session.send(
                            {"type": "error", "msg": f"{type(e).__name__}: {e}"})

            elif t == "set_coread_config":
                cfg = save_coread_config(data.get("cfg") or {})
                LOG.info("coread 配置更新：%s",
                         {k: v for k, v in cfg.items() if k != "last_book"})
                await session.send({"type": "read_state",
                                    "enabled": session.client is not None,
                                    "book": session.read_book, "clock": cfg})

            # ---- 导出对话记录为 Markdown ----
            elif t == "export_transcript":
                await session.export_transcript()

    except websockets.ConnectionClosed:
        LOG.info("WS 连接关闭")
    except Exception as e:
        LOG.error("handle_ws 异常：%s\n%s", e, traceback.format_exc())
    finally:
        # 桥断开 → 注销控制中心注册表（在 teardown 前，状态即刻反映离线）
        if qq_session is not None and QQ_STATE.get("session") is qq_session:
            QQ_STATE.update(session=None, ws=None)
            LOG.info("QQ 会话断开，注册表已注销")
        # 替代原 async with 的 __aexit__：断开时务必关子进程，否则泄漏
        for s in (chat_session, read_session, qq_session):
            if s is None:
                continue
            await s.stop_clock()
            for task in s.genimg_tasks:     # 生图 worker：WS 断了图洗出来也没人看
                task.cancel()
            for task in s.tts_tasks:        # 语音 worker：同理
                task.cancel()
            if s.client is not None:
                # 断线兜底存档：last_session 是滚动单槽，重连开新会话第一轮就被
                # 覆盖；这里把断开的会话固化成正式存档（auto 标记，同会话去重）。
                if s.session_id:
                    try:
                        save_checkpoint_record(
                            s.session_id, s.last_user_text,
                            s.active_preset, mode=s.mode, auto=True)
                        LOG.info("WS 断开自动存档：mode=%s session=%s",
                                 s.mode, s.session_id)
                    except Exception as e:
                        LOG.warning("WS 断开自动存档失败：%s", e)
                LOG.info("WS 收尾，teardown client（%s）", s.mode)
                await s.teardown_client()


# ---- 静态页面：独立 stdlib HTTP 线程（与 WS 端口分离）----
class _HtmlHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth_ok(self):
        """口令闸：access_token 空=全开放（老行为）；本机回环直通（能坐在机器
        前的人本就拥有一切，本地 bat 进不该弹锁屏）；否则 cookie denia_key
        或 ?key= 匹配才放行（<audio>/<img>/fetch 同源自动带 cookie）。"""
        tok = _gui_token()
        if not tok:
            return True
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        c = self.headers.get("Cookie") or ""
        for part in c.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "denia_key" and hmac.compare_digest(v, tok):
                return True
        q = parse_qs(urlsplit(self.path).query)
        key = (q.get("key") or [""])[0]
        return bool(key) and hmac.compare_digest(key, tok)

    def _send_403(self):
        body = "403 - 需要口令".encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/auth":
            # 口令校验端点（前端锁屏/启动统一走这里）：
            # 无 token=直过；本机回环=直过（与数据路由的回环豁免一致，
            # 否则前端 plantCookie 拿 403 照样弹锁屏）；对=200+种 cookie（30 天）；错=403
            tok = _gui_token()
            if not tok:
                self._send_json({"ok": True, "required": False})
                return
            if self.client_address[0] in ("127.0.0.1", "::1"):
                self._send_json({"ok": True, "required": True, "loopback": True})
                return
            q = parse_qs(urlsplit(self.path).query)
            key = (q.get("key") or [""])[0]
            if hmac.compare_digest(key, tok):
                body = json.dumps({"ok": True, "required": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Set-Cookie",
                                 f"denia_key={tok}; Path=/; Max-Age=2592000; SameSite=Lax")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_403()
            return
        # 页面与头像公开（锁屏由前端跑，UI 本身无数据）；数据/文件路由全过闸
        if path not in ("/", "/index.html", "/qq", "/qq.html",
                        "/avatar.png", "/avatar") \
                and not self._auth_ok():
            self._send_403()
            return
        if path in ("/", "/index.html"):
            body = INDEX.read_text(encoding="utf-8").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/qq", "/qq.html"):
            # QQ 控制中心（独立页；数据全走 WS，口令闸在 WS 侧）
            body = (HERE / "qq.html").read_text(encoding="utf-8").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/avatar.png", "/avatar"):
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
        elif path.startswith("/stickers/"):
            # 表情包静态路由：只服务 STICKER_DIR 下的纯文件名（防目录穿越）
            name = unquote(path[len("/stickers/"):])
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
        elif path.startswith("/genimg/"):
            # 生图产物静态路由：只服务 GENIMG_DIR 下的纯文件名（防目录穿越）
            name = unquote(path[len("/genimg/"):])
            img = GENIMG_DIR / name
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
        elif path.startswith("/voice/"):
            # 语音产物静态路由：只服务 VOICE_DIR 下的 wav（防目录穿越）
            name = unquote(path[len("/voice/"):])
            wav = VOICE_DIR / name
            if name and name == os.path.basename(name) and wav.is_file() \
                    and wav.suffix.lower() == ".wav":
                body = wav.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        elif path.startswith("/uploads/"):
            # 用户上传图片回显（聊天历史里的缩略图），同样只服务纯文件名
            name = unquote(path[len("/uploads/"):])
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
        elif path.startswith("/exports/"):
            # 导出的对话记录 markdown 下载（只服务 EXPORT_DIR 下纯文件名的 .md）
            name = unquote(path[len("/exports/"):])
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
        elif path == "/read/books":
            # 共读书单：扫描 denia/私有/共读/*/（有 进度.json 的目录）
            self._send_json({"books": _list_books(),
                             "last_book": load_coread_config().get("last_book")})
        elif path.startswith("/read/toc/"):
            # 某书的章节目录（结构化 JSON，前端免解析目录.md）
            book = unquote(path[len("/read/toc/"):])
            d = _resolve_book_dir(book)
            if d:
                self._send_json({"book": d.name, "chapters": _book_toc(d)})
            else:
                self.send_response(404)
                self.end_headers()
        elif path.startswith("/read/chapter/"):
            # 正文整章下发（实测单章 ≤17KB，不做分页切片）
            rest = unquote(path[len("/read/chapter/"):])
            book, _, fname = rest.partition("/")
            d = _resolve_book_dir(book)
            f = None
            if d and fname and fname == os.path.basename(fname) \
                    and fname.lower().endswith(".md"):
                cand = d / "正文" / fname
                if cand.is_file():
                    f = cand
            if f:
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        elif path.startswith("/vendor/katex/"):
            # 本地 vendor 的 KaTeX 静态资源（css/js/woff2 白名单 + 防穿越）
            rel = unquote(path[len("/vendor/katex/"):])
            root = (HERE / "vendor" / "katex").resolve()
            try:
                f = (root / rel).resolve()
            except Exception:
                f = None
            VENDOR_MIME = {".css": "text/css", ".js": "application/javascript",
                           ".woff2": "font/woff2"}
            if (f and f.is_file() and root in f.parents
                    and f.suffix.lower() in VENDOR_MIME):
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", VENDOR_MIME[f.suffix.lower()])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, obj):
        """小 JSON 响应（书单/目录等）。"""
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """POST /upload —— 用户图片上传（＋按钮 / 拖拽 / 粘贴共用）。
        原始字节流 + X-Filename 头（URL 编码，仅取扩展名），落盘 UPLOAD_DIR。
        返回 {name, path, url}；path 随 chat 消息带给模型 Read。"""
        if urlsplit(self.path).path != "/upload":
            self.send_response(404)
            self.end_headers()
            return
        if not self._auth_ok():
            self._send_403()
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
    ThreadingHTTPServer((_SERVER["host"], HTTP_PORT), _HtmlHandler).serve_forever()


def _lan_ips():
    """本机所有非回环 IPv4（局域网入口候选；VPN/虚拟网卡也可能在列，都打出来）。"""
    ips = []
    try:
        for info in _socket.getaddrinfo(_socket.gethostname(), None, _socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def _print_access_urls():
    """绑 0.0.0.0 时打印局域网入口（带口令）；装了 qrcode 库就顺手打二维码，手机扫即入。"""
    host = _SERVER["host"]
    tok = _gui_token()
    suffix = f"?key={quote(tok)}" if tok else ""
    if host != "0.0.0.0":
        return
    urls = [f"http://{ip}:{HTTP_PORT}{suffix}" for ip in _lan_ips()]
    for u in urls:
        print(f"[*] 局域网入口: {u}")
    if urls and tok:
        try:
            import qrcode
            q = qrcode.QRCode(border=1)
            q.add_data(urls[0])
            print("[*] 手机扫码直达（口令已在链接里）：")
            q.print_ascii(invert=True)
        except ImportError:
            pass   # qrcode 是可选依赖，没装就只打印 URL
        except Exception as e:
            LOG.warning("二维码打印失败（不影响功能）：%s", e)


async def main():
    if not INDEX.exists():
        sys.exit(f"[ERR] index.html 不存在：{INDEX}")
    resolve_server_config()
    _cleanup_uploads()
    # HTTP 静态页面跑在后台线程
    threading.Thread(target=_serve_http, daemon=True).start()
    host = _SERVER["host"]
    print(f"[*] 达妮娅 GUI 后端（Agent SDK / 正式版）")
    print(f"[*] cwd       : {PROJECT_ROOT}（真实 .claude，与终端 /denia 同一套记忆）")
    print(f"[*] provider  : 由前端预设注入（回落进程环境 {os.environ.get('ANTHROPIC_BASE_URL', '默认')}）")
    print(f"[*] 预设文件  : {PRESETS_FILE}")
    print(f"[*] 打开浏览器: http://{HOST}:{HTTP_PORT}")
    print(f"[*] WebSocket : ws://{host}:{WS_PORT}/ws")
    print(f"[*] 绑定地址  : {host}" + ("（局域网开放）" if host == "0.0.0.0" else "（仅本机）"))
    print(f"[*] 访问口令  : {'已开启' if _gui_token() else '关闭（局域网模式强烈建议开启）'}")
    print(f"[*] 日志文件  : {LOG_FILE}")
    _print_access_urls()
    print(f"[*] Ctrl+C 停止\n")
    LOG.info("后端启动完成（host=%s, 口令闸=%s），等待前端连接",
             host, "开" if _gui_token() else "关")
    try:
        # ping_timeout 放宽：SoVITS 冷启（torch/CUDA 加载）能把整机卡住几十秒，
        # 默认 20s 超时会误杀健康连接 → 重连=新会话=用户眼里"对话重启"。
        # 保留 ping 本身：局域网对端走丢（手机切网）仍需回收 Session/子进程。
        async with websockets.serve(handle_ws, host, WS_PORT,
                                    ping_interval=20, ping_timeout=120):
            # qq.enabled=true 时开机自起桥（owned=True，控制中心可离线）；
            # 外部已起的桥走收养逻辑，不受影响
            if load_qq().get("enabled"):
                async def _autostart_qqbridge():
                    ok, msg = await qqbridge_start()
                    if not ok:
                        LOG.warning("QQ 桥开机自起失败：%s", msg)
                asyncio.create_task(_autostart_qqbridge())
            await asyncio.Future()  # run forever
    finally:
        await litellm_cleanup()   # 只收 GUI 自己拉起的 LiteLLM，外部启动的不动
        await tts_cleanup()       # 语音服务同理
        await qqbridge_cleanup()  # QQ 桥同理


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] 已停止")

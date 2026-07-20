#!/usr/bin/env python3
"""
浏览器运营子 Agent — Playwright 有头浏览器，支持 agent 驱动的人机协作。

两种运行模式：
  daemon 模式（推荐）：浏览器常驻后台，Agent 通过 HTTP API 自由组合原子操作
  CLI 模式（兼容）：单次命令，开浏览器→做一件事→关门

━━━ Daemon 模式 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  python browser-operator.py daemon [--port 9876] [--headless]

  HTTP API (127.0.0.1:{port}):

    GET  /health              → {"ok": true, "url": "...", "alive": true}
    POST /navigate  {url, timeout?}        → {"ok": true, "url": "...", "title": "..."}
    GET  /extract   ?selector=&raw=&max=   → {"ok": true, "title": "...", "text": "...", "url": "..."}
    POST /click     {selector?|text?, nth?} → {"ok": true, "url": "...", "title": "..."}
    POST /type      {selector, text}        → {"ok": true}
    POST /wait-human {reason?}              → 阻塞，等人操作完成 → {"ok": true, "url": "...", "text": "..."}
    POST /screenshot {output?}              → {"ok": true, "path": "...", "url": "..."}
    POST /quit                             → 关闭浏览器，退出 daemon

  Agent 自由组合示例：
    curl 127.0.0.1:9876/navigate -d '{"url":"https://zhihu.com"}'
    curl 127.0.0.1:9876/extract
    curl 127.0.0.1:9876/click -d '{"text":"下一页"}'
    curl 127.0.0.1:9876/extract

━━━ CLI 模式（兼容）━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  python browser-operator.py search "数学谜题"
  python browser-operator.py visit "https://zhihu.com/hot"
  python browser-operator.py login "https://zhihu.com/signin"
  python browser-operator.py extract "https://example.com"
  python browser-operator.py screenshot "https://example.com" --output page.png

━━━ 人机协作（daemon 模式的 wait-human）━━━━━━━━━━━━━━━━

  1. Agent 先 SendMessage(main, "需要人工: 原因")
  2. Agent 调 curl POST /wait-human（同步阻塞，等达妮娅写信号文件）
  3. 达妮娅转述用户 → 用户操作 → 用户说"好了" → 达妮娅 touch .human-done
  4. /wait-human 返回当前页面内容
"""

import argparse
import json
import os
import signal as _signal
import sys
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote, parse_qs, urlparse

PROJECT_DIR = Path(__file__).resolve().parent
COOKIE_FILE = PROJECT_DIR / "cookies.json"
SIGNAL_FILE = PROJECT_DIR / ".human-done"

# Win GBK → UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
from playwright_stealth import Stealth

# ── Chromium 定位 ─────────────────────────────────────────

def _find_chromium() -> tuple[str | None, str | None]:
    local_browsers = PROJECT_DIR / "browsers"
    if local_browsers.is_dir():
        for d in sorted(local_browsers.iterdir()):
            if d.is_dir() and d.name.startswith("chromium-"):
                for root, _, files in os.walk(d):
                    exe = "chrome.exe" if sys.platform == "win32" else "chrome"
                    if exe in files:
                        return str(Path(root) / exe), str(local_browsers)
    return None, None

CHROME_EXE, BROWSERS_PATH = _find_chromium()
if BROWSERS_PATH:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS_PATH

# ── 验证码检测 ────────────────────────────────────────────

def is_captcha(page) -> bool:
    CAPTCHA_SIGNALS = [
        "captcha", "verify", "验证", "人机", "机器人", "are you a robot",
        "请完成安全验证", "请点击", "请按住", "滑块",
    ]
    try:
        title = (page.title() or "").lower()
        url = (page.url or "").lower()
        for sig in CAPTCHA_SIGNALS:
            if sig in title or sig in url:
                return True
        for sel in ["iframe[src*=captcha]", "iframe[src*=verify]", ".captcha", "#captcha",
                     "[class*=captcha]", "[id*=captcha]", ".geetest", ".yidun"]:
            if page.query_selector(sel):
                return True
    except Exception:
        pass
    return False

# ── 视觉层次提取（JS snippet，在浏览器内执行）────────────────

VISUAL_EXTRACT_JS = r"""
(() => {
    const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','IFRAME','SVG','NAV','FOOTER',
        'CODE','PRE','TEXTAREA','INPUT','SELECT','BUTTON','OPTION']);
    const vw = window.innerWidth, vh = window.innerHeight;
    const centerL = vw * 0.12, centerR = vw * 0.88;
    const now = Date.now();

    // Collect visible, text-containing block elements
    const items = [];
    const all = document.querySelectorAll(
        'p,h1,h2,h3,h4,h5,h6,li,dt,dd,td,th,caption,figcaption,' +
        'blockquote,label,legend,summary,' +
        'div[class*=title],div[class*=heading],div[class*=desc],div[class*=text],' +
        'span[class*=title],span[class*=heading],' +
        'a[class*=title],a[class*=card],a[class*=item],' +
        '[class*=hot], [class*=trending], [class*=rank]'
    );
    const seen = new Set();

    all.forEach(el => {
        if (SKIP.has(el.tagName)) return;
        const rect = el.getBoundingClientRect();
        if (rect.width < 20 || rect.height < 10) return;  // too small
        if (rect.top > vh + 500 || rect.bottom < -100) return;  // far offscreen

        const text = el.innerText?.trim().replace(/\s+/g, ' ');
        if (!text || text.length < 3) return;
        // Dedup: skip if text is fully contained in a previously seen, longer text
        for (const [s, len] of seen) {
            if (text.length <= len && s.includes(text)) return;
        }
        seen.add([text, text.length]);

        const style = window.getComputedStyle(el);
        const fs = parseFloat(style.fontSize) || 16;
        const fw = parseInt(style.fontWeight) || 400;
        const ta = style.textAlign;
        const color = style.color;

        // Visual prominence score
        let prominence = 1;  // 1=normal, 2=notable, 3=highlight, 4=hero
        if (fs >= 28) prominence = Math.max(prominence, 4);
        else if (fs >= 22) prominence = Math.max(prominence, 3);
        else if (fs >= 18) prominence = Math.max(prominence, 2);
        if (fw >= 700) prominence = Math.max(prominence, 2);
        if (fw >= 600 && fs >= 18) prominence = Math.max(prominence, 3);
        if (ta === 'center' && fs >= 20) prominence = Math.max(prominence, 3);
        if (ta === 'center' && rect.width > vw * 0.5) prominence = Math.max(prominence, 2);

        // Position: center-column content is more important
        const inCenter = rect.left > centerL && rect.right < centerR;
        if (inCenter && rect.width > vw * 0.4) prominence = Math.max(prominence, 2);

        // De-prioritize edge/small text
        if (rect.width < vw * 0.15 && fs <= 14) prominence = Math.max(0, prominence - 1);

        items.push({
            tag: el.tagName.toLowerCase(),
            text: text.substring(0, 400),
            fs: Math.round(fs),
            fw: fw,
            ta: ta,
            top: Math.round(rect.top),
            left: Math.round(rect.left),
            w: Math.round(rect.width),
            h: Math.round(rect.height),
            center: inCenter,
            p: prominence
        });
    });

    // Sort by position (top to bottom, left to right within same row)
    items.sort((a, b) => a.top - b.top || a.left - b.left);

    // Remove items whose text is a substring of a nearby sibling (nested elements)
    const filtered = [];
    for (let i = 0; i < items.length; i++) {
        const cur = items[i];
        let skip = false;
        // Check against recently added items (within ~200px above)
        for (let j = filtered.length - 1; j >= 0 && filtered[j].top > cur.top - 200; j--) {
            const prev = filtered[j];
            // If prev's text contains cur's text, cur is redundant
            if (prev.text.length >= cur.text.length && prev.text.includes(cur.text)) {
                skip = true; break;
            }
        }
        if (!skip) filtered.push(cur);
    }

    // Categorize and format
    const blocks = filtered.map(item => {
        const isHeading = /^h[1-6]$/.test(item.tag);
        const level = isHeading ? parseInt(item.tag[1]) : 0;
        let fmt = 'text';
        let line = item.text;

        if (isHeading) {
            // Markdown heading
            const prefix = '#'.repeat(Math.min(level + 1, 4));
            line = prefix + ' ' + item.text;
            fmt = 'heading';
        } else if (item.p >= 4) {
            line = '> **' + item.text + '**';
            fmt = 'hero';
        } else if (item.p >= 3 && item.ta === 'center') {
            line = '> **' + item.text + '**';
            fmt = 'highlight';
        } else if (item.p >= 3) {
            line = '**' + item.text + '**';
            fmt = 'bold';
        } else if (item.p >= 2) {
            line = '- ' + item.text;
            fmt = 'bullet';
        }

        return {p: item.p, line: line, top: item.top, fmt: fmt, fs: item.fs, isHeading: isHeading};
    });

    // Deduplicate: remove items subsumed by adjacent headings
    const deduped = [];
    for (let i = 0; i < blocks.length; i++) {
        const cur = blocks[i];
        // Skip if this is a text block whose content is already in the previous heading
        if (!cur.isHeading && i > 0 && blocks[i-1].isHeading) {
            const prevText = blocks[i-1].line.replace(/^#+\s*/, '');
            if (cur.line.includes(prevText) || prevText.includes(cur.line)) continue;
        }
        deduped.push(cur);
    }

    // Build markdown with section breaks
    const limited = deduped.slice(0, 80);
    let md = '';
    let lastFmt = '';
    limited.forEach(b => {
        if (b.fmt === 'heading' && lastFmt !== 'heading') md += '\n';
        md += b.line + '\n';
        lastFmt = b.fmt;
    });

    // Heuristic: detect if page is a search results page
    const isSearch = document.querySelector('input[type=search], input[name=q], [role=search]') !== null;

    return {
        md: md.trim() || '(页面无可见文本内容)',
        count: limited.length,
        highlights: limited.filter(b => b.fmt === 'heading' || b.fmt === 'hero' || b.fmt === 'highlight').length,
        isSearch: isSearch,
        url: window.location.href,
        title: document.title,
        _ms: Date.now() - now
    };
})()
"""

# ── 智能正文提取 ──────────────────────────────────────────

CONTENT_SELECTORS = [
    "main", "article", '[role="main"]',
    ".main-content", "#content", ".content",
    ".post-content", ".article-content", ".entry-content",
    ".post", ".article",
]

BLOCK_SELECTORS = [
    "p", "h1", "h2", "h3", "h4",
    "article", "section",
    ".card", ".post", ".item",
    ".title", ".desc", ".summary",
    '[class*="content"]', '[class*="text"]', '[class*="desc"]',
]


def extract_page_text(page, max_chars: int = 800) -> str:
    # 1. 语义容器
    for sel in CONTENT_SELECTORS:
        el = page.query_selector(sel)
        if el:
            text = el.inner_text().strip()
            if len(text) > 80:
                return text[:max_chars]
    # 2. 合并 block 级文本
    seen = set()
    blocks = []
    for sel in BLOCK_SELECTORS:
        for el in page.query_selector_all(sel)[:20]:
            txt = el.inner_text().strip()
            if len(txt) > 15 and txt not in seen:
                seen.add(txt)
                blocks.append(txt)
    if blocks:
        return " ".join(blocks)[:max_chars]
    # 3. body 兜底
    try:
        body = page.inner_text("body")
    except Exception:
        body = ""
    lines = [l.strip() for l in body.split("\n") if len(l.strip()) > 15]
    return "\n".join(lines[:30])[:max_chars]


# ═══════════════════════════════════════════════════════════
# BrowserAPI — 线程安全封装
# ═══════════════════════════════════════════════════════════

class BrowserAPI:
    """常驻浏览器实例，通过 threading.Lock 保证线程安全。
    自动追踪所有标签页，始终操作最近活跃的页面。"""

    def __init__(self, headless: bool = False, cookie_file: Path | None = None,
                 slow_mo: int = 0):
        self.headless = headless
        self.cookie_file = cookie_file or COOKIE_FILE
        self.slow_mo = slow_mo
        self.lock = threading.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._pages: list = []  # 追踪所有标签页

    # ── 生命周期 ──────────────────────────────────────────

    def _on_new_page(self, page):
        """新标签页/弹窗事件：自动切换到新页面。"""
        try:
            Stealth().apply_stealth_sync(page)
        except Exception:
            pass
        self._pages.append(page)
        self._page = page
        print(f"[daemon] 检测到新标签页: {page.url or 'about:blank'}", flush=True)

    @property
    def _active_page(self):
        """始终返回最近活跃的页面（自动跟随用户切换标签页）。"""
        return self._page

    def start(self):
        pw = sync_playwright().start()
        launch_kwargs = {
            "headless": self.headless,
            "slow_mo": self.slow_mo,
            "args": [
                "--use-gl=swiftshader",  # 软件 GPU，避免驱动问题
                "--enable-accelerated-video-decode",
                "--disable-gpu-sandbox",
            ],
        }
        if CHROME_EXE:
            launch_kwargs["executable_path"] = CHROME_EXE
        browser = pw.chromium.launch(**launch_kwargs)

        storage_state = str(self.cookie_file) if self.cookie_file.exists() else None
        context = browser.new_context(
            locale="zh-CN",
            storage_state=storage_state,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Safari/537.36"
            ),
        )
        # 监听新标签页
        context.on('page', self._on_new_page)

        page = context.new_page()
        try:
            Stealth().apply_stealth_sync(page)
        except Exception:
            pass

        self._playwright = pw
        self._browser = browser
        self._context = context
        self._page = page
        self._pages = [page]
        print(f"[daemon] 浏览器已启动 (headless={self.headless}, slow_mo={self.slow_mo})", flush=True)

    def _save_cookies(self):
        try:
            self._context.storage_state(path=str(self.cookie_file))
        except Exception:
            pass

    def stop(self):
        try:
            self._save_cookies()
        except Exception:
            pass
        for obj in [self._context, self._browser, self._playwright]:
            if obj is None:
                continue
            try:
                obj.close() if hasattr(obj, "close") else obj.stop()
            except Exception:
                pass
        print("[daemon] 浏览器已关闭", flush=True)

    # ── 原子操作 ──────────────────────────────────────────

    def navigate(self, url: str, timeout: int = 30_000) -> dict:
        """导航到 URL。"""
        with self.lock:
            try:
                self._active_page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            except PwTimeout:
                pass  # 部分页面加载慢，继续
            self._save_cookies()
            captcha = is_captcha(self._active_page)
            return {
                "ok": True,
                "url": self._active_page.url,
                "title": self._active_page.title(),
                "captcha": captcha,
            }

    def extract(self, selector: str | None = None, max_chars: int = 2000,
                raw: bool = False, mode: str = "text") -> dict:
        """读取当前页面内容。

        mode 参数:
          - "text" (默认): 智能正文提取，返回纯文本
          - "visual": 视觉层次感知提取，返回 markdown（保留强调、结构、热度标记）
        """
        with self.lock:
            url = self._active_page.url
            title = self._active_page.title()

            if mode == "visual":
                return self._extract_visual(url, title, max_chars)

            if selector:
                el = self._active_page.query_selector(selector)
                text = el.inner_text() if el else ""
            elif raw:
                try:
                    text = self._active_page.inner_text("body") or ""
                except Exception:
                    text = ""
            else:
                text = extract_page_text(self._active_page, max_chars)
            return {"ok": True, "url": url, "title": title, "text": text[:max_chars]}

    def _extract_visual(self, url: str, title: str, max_chars: int) -> dict:
        """运行 JS snippet，按视觉层次提取页面内容为 markdown。
        SPA 页面自动重试（最多 3 次，每次间隔 1 秒）。"""
        last_error = None
        for attempt in range(3):
            try:
                raw = self._active_page.evaluate(VISUAL_EXTRACT_JS)
                break
            except Exception as e:
                last_error = str(e)
                if "execution context was destroyed" in last_error.lower():
                    time.sleep(1)
                    continue
                # 不可恢复的错误直接返回
                return {"ok": False, "error": f"视觉提取失败: {e}", "url": url, "title": title}
        else:
            # 3 次都失败
            return {"ok": False, "error": f"视觉提取失败（SPA 导航中）: {last_error}",
                    "url": url, "title": title, "hint": "页面仍在加载中，请稍后重试或使用 mode=text"}

        md = raw.get("md", "")
        highlights = raw.get("highlights", 0)
        count = raw.get("count", 0)
        is_search = raw.get("isSearch", False)
        elapsed = raw.get("_ms", 0)

        # 构建结构化输出——标题区与内容区明确分隔
        content = md[:max_chars] if md else "(页面无可见文本内容)"

        prefix = (
            f"┌─ 页面标题 ─────────────────────\n"
            f"│ {title}\n"
            f"├─ 元信息 ───────────────────────\n"
            f"│ URL: {url}\n"
            f"│ visual · {count}块 · {highlights}处高亮"
        )
        if is_search:
            prefix += " · 搜索结果页"
        prefix += f" · {elapsed}ms\n"
        prefix += "└────────────────────────────────\n\n"

        return {
            "ok": True,
            "url": url,
            "title": title,
            "text": prefix + content,
            "mode": "visual",
            "meta": {
                "blocks": count,
                "highlights": highlights,
                "is_search": is_search,
                "extract_ms": elapsed,
            },
        }

    def click(self, selector: str | None = None, text: str | None = None,
              nth: int = 0, force: bool = False) -> dict:
        """点击元素。支持 CSS selector 或文本匹配。force=True 跳过可见性检查。"""
        with self.lock:
            try:
                if text:
                    el = self._active_page.get_by_text(text).nth(nth)
                elif selector:
                    el = self._active_page.query_selector_all(selector)[nth]
                else:
                    return {"ok": False, "error": "需要提供 selector 或 text 参数"}
                el.click(force=force)
                try:
                    self._active_page.wait_for_load_state("domcontentloaded", timeout=10_000)
                except PwTimeout:
                    pass
            except Exception as e:
                return {"ok": False, "error": str(e), "url": self._active_page.url}
            self._save_cookies()
            return {"ok": True, "url": self._active_page.url, "title": self._active_page.title()}

    def type_text(self, selector: str, text: str) -> dict:
        """在输入框中填入文字。"""
        with self.lock:
            try:
                el = self._active_page.query_selector(selector)
                if not el:
                    return {"ok": False, "error": f"未找到元素: {selector}"}
                el.fill(text)
            except Exception as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True}

    def press_key(self, key: str) -> dict:
        """按下键盘按键（Enter, Escape, Tab 等）。"""
        with self.lock:
            try:
                self._active_page.keyboard.press(key)
            except Exception as e:
                return {"ok": False, "error": str(e)}
            try:
                self._active_page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except PwTimeout:
                pass
            self._save_cookies()
            return {"ok": True, "url": self._active_page.url, "title": self._active_page.title()}

    def wait_human(self, reason: str = "需要人工操作") -> dict:
        """
        等待人工操作完成（登录 / 验证码 / 自由浏览）。

        轮询 .human-done 信号文件（由达妮娅创建），最长等 5 分钟。
        轮询期间释放锁，允许 health/extract 等其他只读操作。
        """
        # 清理旧信号
        if SIGNAL_FILE.exists():
            try:
                SIGNAL_FILE.unlink()
            except Exception:
                pass

        # 检查浏览器存活
        with self.lock:
            try:
                start_url = self._active_page.url
            except Exception:
                return {"ok": False, "error": "浏览器已关闭"}

        print(f"\n{'='*60}", flush=True)
        print(f"  ⏸️  {reason}", flush=True)
        print(f"  浏览器窗口已打开，请在浏览器中完成操作。", flush=True)
        print(f"{'='*60}", flush=True)
        print(f"__HUMAN_NEEDED__:{reason}", flush=True)

        # 轮询（不持锁，允许其他操作）
        timeout = 300
        for _ in range(timeout):
            if SIGNAL_FILE.exists():
                try:
                    SIGNAL_FILE.unlink()
                except Exception:
                    pass
                print("__HUMAN_DONE__", flush=True)
                # 捕获当前页面
                with self.lock:
                    try:
                        url = self._active_page.url
                        title = self._active_page.title()
                        text = extract_page_text(self._active_page)
                    except Exception:
                        return {"ok": True, "message": "操作完成（无法读取页面）", "url": start_url, "title": "", "text": ""}
                self._save_cookies()
                return {"ok": True, "url": url, "title": title, "text": text}
            # 检查浏览器
            try:
                self._active_page.title()
            except Exception:
                print("__BROWSER_CLOSED__", flush=True)
                return {"ok": False, "error": "浏览器已关闭"}
            time.sleep(1)

        print("__HUMAN_TIMEOUT__", flush=True)
        return {"ok": False, "error": "等待超时（5分钟）"}

    def screenshot(self, output: str = "screenshot.png") -> dict:
        """整页截图保存。"""
        with self.lock:
            path = Path(output)
            if not path.is_absolute():
                path = Path.cwd() / path
            self._active_page.screenshot(path=str(path), full_page=True)
            return {"ok": True, "path": str(path), "url": self._active_page.url, "title": self._active_page.title()}

    def health(self) -> dict:
        """健康检查。不持锁，快速返回。"""
        try:
            url = self._active_page.url
            title = self._active_page.title()
            return {"ok": True, "alive": True, "url": url, "title": title}
        except Exception:
            return {"ok": False, "alive": False, "error": "浏览器不可达"}


# ═══════════════════════════════════════════════════════════
# HTTP Daemon
# ═══════════════════════════════════════════════════════════

class DaemonHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。类变量 api 由 run_daemon 注入。"""
    api: BrowserAPI = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):
        print(f"[daemon] {args[0]}", flush=True)

    def _send(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        # Windows Git Bash mangling: try UTF-8 first, then fallback encodings
        for enc in ["utf-8", "gbk", "utf-8-sig"]:
            try:
                return json.loads(raw.decode(enc))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        # Last resort: replace invalid bytes
        return json.loads(raw.decode("utf-8", errors="replace"))

    # ── 路由 ──────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path
        params = parse_qs(urlparse(self.path).query)

        if path == "/health":
            r = self.api.health()
        elif path == "/extract":
            selector = params.get("selector", [None])[0]
            raw = params.get("raw", ["0"])[0] == "1"
            mode = params.get("mode", ["text"])[0]
            max_chars = int(params.get("max", ["2000"])[0])
            r = self.api.extract(selector=selector, max_chars=max_chars, raw=raw, mode=mode)
        else:
            self._send({"error": f"unknown endpoint: {path}"}, 404)
            return
        self._send(r)

    def do_POST(self):
        path = urlparse(self.path).path

        # /quit 不需要 body
        if path == "/quit":
            self._send({"ok": True, "message": "shutting down"})
            threading.Thread(target=self._shutdown, daemon=True).start()
            return

        try:
            data = self._body()
        except json.JSONDecodeError:
            self._send({"error": "invalid JSON body"}, 400)
            return

        try:
            if path == "/navigate":
                r = self.api.navigate(
                    url=data.get("url", ""),
                    timeout=data.get("timeout", 30_000),
                )
            elif path == "/extract":
                r = self.api.extract(
                    selector=data.get("selector"),
                    max_chars=data.get("max_chars", 2000),
                    raw=data.get("raw", False),
                    mode=data.get("mode", "text"),
                )
            elif path == "/click":
                r = self.api.click(
                    selector=data.get("selector"),
                    text=data.get("text"),
                    nth=data.get("nth", 0),
                    force=data.get("force", False),
                )
            elif path == "/press":
                r = self.api.press_key(data.get("key", ""))
            elif path == "/type":
                r = self.api.type_text(
                    selector=data.get("selector", ""),
                    text=data.get("text", ""),
                )
            elif path == "/wait-human":
                r = self.api.wait_human(
                    reason=data.get("reason", "需要人工操作"),
                )
            elif path == "/screenshot":
                r = self.api.screenshot(
                    output=data.get("output", "screenshot.png"),
                )
            else:
                self._send({"error": f"unknown endpoint: {path}"}, 404)
                return
        except Exception as e:
            self._send({"ok": False, "error": str(e)}, 500)
            return

        self._send(r)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _shutdown(self):
        time.sleep(0.3)
        self.server.shutdown()


def run_daemon(port: int, headless: bool, cookie_file: Path):
    """启动浏览器 daemon 并进入 HTTP 服务循环。"""
    # 单例检查：Windows 的 SO_REUSEADDR 语义宽松，允许两个进程绑定同一
    # 活跃端口，仅捕获 bind 异常防不住重复 daemon——先主动探测。
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as _resp:
            print(f"[daemon] 端口 {port} 已有 daemon 在运行，退出（未启动浏览器）", flush=True)
            sys.exit(2)
    except Exception:
        pass  # 无 daemon 应答 → 继续启动

    # 先绑定端口——失败说明端口被其他程序占用，
    # 必须在启动浏览器之前退出，否则会产生无人管理的孤儿 Chromium。
    try:
        server = HTTPServer(("127.0.0.1", port), DaemonHandler)
    except OSError as e:
        print(f"[daemon] 端口 {port} 被占用，退出（未启动浏览器）: {e}", flush=True)
        sys.exit(2)

    api = BrowserAPI(headless=headless, cookie_file=cookie_file, slow_mo=0)
    try:
        api.start()
    except Exception:
        server.server_close()
        raise
    DaemonHandler.api = api

    print(f"[daemon] 监听 http://127.0.0.1:{port}", flush=True)
    print(f"[daemon] 端点: /health /navigate /extract /click /type /wait-human /screenshot /quit", flush=True)

    def on_signal(signum, frame):
        print("\n[daemon] 收到退出信号...", flush=True)
        api.stop()
        server.shutdown()

    _signal.signal(_signal.SIGINT, on_signal)
    _signal.signal(_signal.SIGTERM, on_signal)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        api.stop()
        print("[daemon] 已退出", flush=True)


# ═══════════════════════════════════════════════════════════
# CLI 模式（兼容，代码不变）
# ═══════════════════════════════════════════════════════════

# ── 人机协作信号（CLI 用，daemon 模式走 BrowserAPI.wait_human）──

def wait_for_human(page, reason: str = "需要人工操作"):
    if SIGNAL_FILE.exists():
        try:
            SIGNAL_FILE.unlink()
        except Exception:
            pass
    print(f"\n{'='*60}", flush=True)
    print(f"  ⏸️  {reason}", flush=True)
    print(f"  浏览器窗口已打开，请在浏览器中完成操作。", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"__HUMAN_NEEDED__:{reason}", flush=True)
    timeout = 300
    for _ in range(timeout):
        if SIGNAL_FILE.exists():
            try:
                SIGNAL_FILE.unlink()
            except Exception:
                pass
            print("__HUMAN_DONE__", flush=True)
            return True
        try:
            page.title()
        except Exception:
            print("__BROWSER_CLOSED__", flush=True)
            return False
        time.sleep(1)
    print("__HUMAN_TIMEOUT__", flush=True)
    return False


def safe_goto(page, url: str, timeout: int = 20000, captcha_retry: bool = True):
    try:
        page.goto(url, wait_until="networkidle", timeout=timeout)
    except PwTimeout:
        pass
    time.sleep(1)
    if captcha_retry and is_captcha(page):
        ok = wait_for_human(page, "检测到验证码/人机验证页面 → 请手动完成验证")
        if ok:
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PwTimeout:
                pass
            time.sleep(1)


def do_search(page, query: str, max_results: int) -> dict:
    safe_goto(page, f"https://www.bing.com/search?q={quote(query)}")
    results = []
    for item in page.query_selector_all("li.b_algo")[:max_results]:
        title_el = item.query_selector("h2 a")
        snippet_el = item.query_selector(".b_caption p")
        link_el = item.query_selector("h2 a")
        if title_el:
            results.append({
                "title": title_el.inner_text().strip(),
                "snippet": (snippet_el.inner_text().strip() if snippet_el else "")[:300],
                "url": link_el.get_attribute("href") or "" if link_el else "",
            })
    return {"query": query, "results": results, "source": "bing"}


def do_visit(page, url: str) -> dict:
    safe_goto(page, url)
    title = page.title()
    text = extract_page_text(page)
    links = []
    for a in page.query_selector_all("a[href]")[:30]:
        href = a.get_attribute("href") or ""
        txt = a.inner_text().strip()
        if txt and len(txt) > 5 and href.startswith("http"):
            links.append({"text": txt[:60], "url": href})
    return {"title": title, "text": text, "url": page.url, "links": links[:10]}


def do_screenshot(page, url: str, output: str) -> dict:
    safe_goto(page, url)
    path = Path(output)
    if not path.is_absolute():
        path = Path.cwd() / path
    page.screenshot(path=str(path), full_page=True)
    return {"screenshot": str(path), "title": page.title(), "url": page.url}


def do_extract(page, url: str, selector: str = "body") -> dict:
    safe_goto(page, url)
    if selector != "body":
        el = page.query_selector(selector)
        text = el.inner_text() if el else ""
    else:
        text = extract_page_text(page, max_chars=2000)
    return {"url": page.url, "selector": selector, "text": text[:2000]}


# ── 主入口 ─────────────────────────────────────────────────

def main():
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--headless", action="store_true", help="无头模式")
    shared.add_argument("--cookie-file", type=str, default=str(COOKIE_FILE),
                        help=f"Cookie 文件路径 (默认: {COOKIE_FILE})")

    parser = argparse.ArgumentParser(description="浏览器运营工具（CLI + Daemon）", parents=[shared])
    sub = parser.add_subparsers(dest="action", required=True)

    # ── Daemon 模式 ──
    sp_daemon = sub.add_parser("daemon", parents=[shared], help="启动浏览器 daemon（HTTP API，常驻后台）")
    sp_daemon.add_argument("--port", type=int, default=9876, help="HTTP 监听端口 (默认: 9876)")

    # ── CLI 模式 ──
    sp_search = sub.add_parser("search", parents=[shared], help="[CLI] Bing 搜索")
    sp_search.add_argument("query", type=str)
    sp_search.add_argument("--max", type=int, default=5)

    sp_visit = sub.add_parser("visit", parents=[shared], help="[CLI] 访问页面")
    sp_visit.add_argument("url", type=str)

    sp_screenshot = sub.add_parser("screenshot", parents=[shared], help="[CLI] 整页截图")
    sp_screenshot.add_argument("url", type=str)
    sp_screenshot.add_argument("--output", type=str, default="screenshot.png")

    sp_extract = sub.add_parser("extract", parents=[shared], help="[CLI] CSS 选择器提取")
    sp_extract.add_argument("url", type=str)
    sp_extract.add_argument("--selector", type=str, default="body")

    sp_login = sub.add_parser("login", parents=[shared], help="[CLI] 打开网站 → 等待人工登录 → 保存 Cookie")
    sp_login.add_argument("url", type=str)
    sp_login.add_argument("--reason", type=str, default="请登录账号")

    args = parser.parse_args()

    # ── Daemon 模式 ──
    if args.action == "daemon":
        cookie_path = Path(args.cookie_file)
        if not cookie_path.is_absolute():
            cookie_path = PROJECT_DIR / cookie_path
        run_daemon(port=args.port, headless=args.headless, cookie_file=cookie_path)
        return

    # ── CLI 模式 ──
    cookie_path = Path(args.cookie_file)
    if not cookie_path.is_absolute():
        cookie_path = PROJECT_DIR / cookie_path

    storage_state = str(cookie_path) if cookie_path.exists() else None
    result = {}
    cookie_saved = False

    with sync_playwright() as p:
        launch_kwargs = {"headless": args.headless, "slow_mo": 300 if not args.headless else 0}
        if CHROME_EXE:
            launch_kwargs["executable_path"] = CHROME_EXE
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            locale="zh-CN",
            storage_state=storage_state,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)

        try:
            if args.action == "login":
                safe_goto(page, args.url)
                ok = wait_for_human(page, args.reason)
                if ok:
                    context.storage_state(path=str(cookie_path))
                    cookie_saved = True
                    # 登录完成后也读取页面内容
                    text = extract_page_text(page)
                    result = {
                        "message": "登录完成，Cookie 已保存",
                        "url": page.url,
                        "title": page.title(),
                        "text": text,
                        "cookie_file": str(cookie_path),
                    }
                else:
                    result = {"_error": "用户取消或浏览器关闭"}
            elif args.action == "search":
                result = do_search(page, args.query, args.max)
            elif args.action == "visit":
                result = do_visit(page, args.url)
            elif args.action == "screenshot":
                result = do_screenshot(page, args.url, args.output)
            elif args.action == "extract":
                result = do_extract(page, args.url, args.selector)
        except Exception as e:
            result = {"_error": str(e)}
        finally:
            if not cookie_saved:
                try:
                    context.storage_state(path=str(cookie_path))
                except Exception:
                    pass
        browser.close()

    print(json.dumps({"ok": "_error" not in result, "data": result},
                     ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

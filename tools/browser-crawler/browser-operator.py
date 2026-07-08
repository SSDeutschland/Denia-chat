#!/usr/bin/env python3
"""
浏览器运营子 Agent — Playwright 有头浏览器，支持 agent 驱动的人机协作。

用法:
  python browser-operator.py search "数学谜题"
  python browser-operator.py visit "https://zhihu.com/hot"
  python browser-operator.py login "https://zhihu.com/signin"
  python browser-operator.py extract "https://example.com"
  python browser-operator.py screenshot "https://example.com" --output page.png

Agent 驱动的人机协作流程:
  1. Agent 以 background 模式启动脚本（如 login / search）
  2. 脚本打开浏览器，遇到需要人工操作时进入信号轮询
  3. 脚本打印 __HUMAN_NEEDED__:原因 到 stdout
  4. Agent 告知用户："浏览器需要你的操作"
  5. 用户完成操作后告诉 Agent"好了"
  6. Agent 创建信号文件（touch .human-done），脚本检测到后继续
  7. 脚本输出最终 JSON 结果，退出

信号文件: tools/browser-crawler/.human-done
Cookie 文件: tools/browser-crawler/cookies.json (自动加载/保存)

输出: JSON → stdout
  { "ok": true, "data": {...} }
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

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
    """
    返回 (executable_path, PLAYWRIGHT_BROWSERS_PATH)。
    优先项目本地 Chromium（可移植），不存在则用 Playwright 默认安装位置。
    """
    local_browsers = PROJECT_DIR / "browsers"
    if local_browsers.is_dir():
        # 找第一个 chromium-* 目录
        for d in sorted(local_browsers.iterdir()):
            if d.is_dir() and d.name.startswith("chromium-"):
                for root, _, files in os.walk(d):
                    exe = "chrome.exe" if sys.platform == "win32" else "chrome"
                    if exe in files:
                        return str(Path(root) / exe), str(local_browsers)
    # 回退：Playwright 默认管理路径
    return None, None

CHROME_EXE, BROWSERS_PATH = _find_chromium()
if BROWSERS_PATH:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS_PATH

# ── 人机协作信号 ──────────────────────────────────────────

def wait_for_human(page, reason: str = "需要人工操作"):
    """
    信号文件轮询模式 — 不阻塞 stdin，让 Agent 可以驱动流程。
    脚本保持浏览器打开，轮询 .human-done 信号文件。
    Agent 在用户确认后创建该文件，脚本检测到后继续执行。
    """
    # 清理旧信号
    if SIGNAL_FILE.exists():
        try:
            SIGNAL_FILE.unlink()
        except Exception:
            pass

    print(f"\n{'='*60}", flush=True)
    print(f"  ⏸️  {reason}", flush=True)
    print(f"  浏览器窗口已打开，请在浏览器中完成操作。", flush=True)
    print(f"{'='*60}", flush=True)

    # 给 Agent 的结构化信号行
    print(f"__HUMAN_NEEDED__:{reason}", flush=True)

    # 轮询信号文件（每秒检查）
    timeout = 300  # 5 分钟超时
    for _ in range(timeout):
        if SIGNAL_FILE.exists():
            try:
                SIGNAL_FILE.unlink()
            except Exception:
                pass
            print("__HUMAN_DONE__", flush=True)
            return True
        # 检查浏览器是否还活着
        try:
            page.title()
        except Exception:
            print("__BROWSER_CLOSED__", flush=True)
            return False
        time.sleep(1)

    print("__HUMAN_TIMEOUT__", flush=True)
    return False


def is_captcha(page) -> bool:
    """检测当前页面是否为验证码/拦截页面"""
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


# ── 页面操作 ──────────────────────────────────────────────

def safe_goto(page, url: str, timeout: int = 20000, captcha_retry: bool = True):
    """导航到 URL，自动检测验证码并等待人工处理"""
    try:
        page.goto(url, wait_until="networkidle", timeout=timeout)
    except PwTimeout:
        pass  # 部分页面 networkidle 太久，继续

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
    """Bing 搜索"""
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


# ── 智能正文提取 ──────────────────────────────────────────

# 语义容器 > 内容区 > block 文本 > body 兜底
CONTENT_SELECTORS = [
    "main", "article", '[role="main"]',
    ".main-content", "#content", ".content",
    ".post-content", ".article-content", ".entry-content",
    ".post", ".article",
]

# 用于合并式提取的 block 级标签
BLOCK_SELECTORS = [
    "p", "h1", "h2", "h3", "h4",
    "article", "section",
    ".card", ".post", ".item",
    ".title", ".desc", ".summary",
    '[class*="content"]', '[class*="text"]', '[class*="desc"]',
]


def extract_page_text(page, max_chars: int = 800) -> str:
    """
    智能提取页面正文。
    优先找语义容器（文章/内容区），其次合并 block 文本，最后 body 兜底。
    适配：文章页、卡片流(B站)、SPA(知乎)、传统网页。
    """
    # 1. 语义容器
    for sel in CONTENT_SELECTORS:
        el = page.query_selector(sel)
        if el:
            text = el.inner_text().strip()
            if len(text) > 80:
                return text[:max_chars]

    # 2. 合并 block 级文本（卡片流 / SPA）
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

    # 3. body 兜底，过滤噪音（导航文本、空行）
    try:
        body = page.inner_text("body")
    except Exception:
        body = ""
    lines = [l.strip() for l in body.split("\n") if len(l.strip()) > 15]
    return "\n".join(lines[:30])[:max_chars]


def do_visit(page, url: str) -> dict:
    """访问页面，返回标题+智能正文摘要"""
    safe_goto(page, url)
    title = page.title()
    text = extract_page_text(page)
    links = []
    for a in page.query_selector_all("a[href]")[:30]:
        href = a.get_attribute("href") or ""
        txt = a.inner_text().strip()
        if txt and len(txt) > 5 and href.startswith("http"):
            links.append({"text": txt[:60], "url": href})
    return {
        "title": title,
        "text": text,
        "url": page.url,
        "links": links[:10],
    }


def do_screenshot(page, url: str, output: str) -> dict:
    """截图保存"""
    safe_goto(page, url)
    path = Path(output)
    if not path.is_absolute():
        path = Path.cwd() / path
    page.screenshot(path=str(path), full_page=True)
    return {"screenshot": str(path), "title": page.title(), "url": page.url}


def do_extract(page, url: str, selector: str = "body") -> dict:
    """
    按 CSS 选择器提取内容。
    如果 selector="body"（默认）且页面无指定选择器内容，回退到智能提取。
    """
    safe_goto(page, url)
    # 显式指定了具体选择器 → 精确提取
    if selector != "body":
        el = page.query_selector(selector)
        text = el.inner_text() if el else ""
    else:
        # 默认 body → 走智能提取
        text = extract_page_text(page, max_chars=2000)
    return {"url": page.url, "selector": selector, "text": text[:2000]}


# ── 主入口 ─────────────────────────────────────────────────

def main():
    # 共享参数 — 通过 parents= 注入每个子命令，保证 --headless 前后都生效
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--headless", action="store_true", help="无头模式（跳过人工等待）")
    shared.add_argument("--cookie-file", type=str, default=str(COOKIE_FILE),
                        help=f"Cookie 持久化文件路径 (默认: {COOKIE_FILE})")

    parser = argparse.ArgumentParser(description="浏览器运营子 Agent", parents=[shared])
    sub = parser.add_subparsers(dest="action", required=True)

    sp_search = sub.add_parser("search", parents=[shared], help="Bing 搜索")
    sp_search.add_argument("query", type=str)
    sp_search.add_argument("--max", type=int, default=5)

    sp_visit = sub.add_parser("visit", parents=[shared], help="访问页面，提取标题+正文")
    sp_visit.add_argument("url", type=str)

    sp_screenshot = sub.add_parser("screenshot", parents=[shared], help="整页截图")
    sp_screenshot.add_argument("url", type=str)
    sp_screenshot.add_argument("--output", type=str, default="screenshot.png")

    sp_extract = sub.add_parser("extract", parents=[shared], help="CSS 选择器提取")
    sp_extract.add_argument("url", type=str)
    sp_extract.add_argument("--selector", type=str, default="body")

    sp_login = sub.add_parser("login", parents=[shared], help="打开网站 → 等待人工登录 → 保存 Cookie")
    sp_login.add_argument("url", type=str, help="登录页面 URL")
    sp_login.add_argument("--reason", type=str, default="请登录账号", help="给用户的提示")

    args = parser.parse_args()

    # 确定 cookie 文件路径
    cookie_path = Path(args.cookie_file)
    if not cookie_path.is_absolute():
        cookie_path = PROJECT_DIR / cookie_path

    # 加载已保存的 Cookie
    storage_state = str(cookie_path) if cookie_path.exists() else None

    result = {}
    cookie_saved = False

    with sync_playwright() as p:
        launch_kwargs = {
            "headless": args.headless,
            "slow_mo": 300 if not args.headless else 0,
        }
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
                    result = {
                        "message": "登录完成，Cookie 已保存",
                        "url": page.url,
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
            # 非 login 操作也保存 cookie（保持登录态更新）
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

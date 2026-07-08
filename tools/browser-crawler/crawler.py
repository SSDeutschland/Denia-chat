#!/usr/bin/env python3
"""
达妮娅想法池 — 浏览器爬虫
用 Playwright + Stealth 浏览网页，提取内容生成想法种子。

用法:
  python crawler.py --topic "fun math facts"       ← Bing 搜索（推荐英文）
  python crawler.py --url "https://example.com"     ← 直接访问页面
  python crawler.py --query-file topics.txt         ← 从 UTF-8 文件读查询

网络环境: Wikipedia/HN/DuckDuckGo 不可达，Baidu 出验证码。
Bing 可用但中文长查询不稳定，建议用英文关键词。

输出: JSON → stdout
  { "ok": true,
    "ideas": [{"title": "...", "snippet": "...", "url": "...", "perspective": "..."}] }
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

PROJECT_DIR = Path(__file__).resolve().parent

# Windows 终端默认 GBK，强制 stdout 用 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# ── Chromium 定位（优先本地，回退 Playwright 默认）────────
_LOCAL_BROWSERS = PROJECT_DIR / "browsers"
_CHROME_EXE = None
if _LOCAL_BROWSERS.is_dir():
    for d in sorted(_LOCAL_BROWSERS.iterdir()):
        if d.is_dir() and d.name.startswith("chromium-"):
            for root, _, files in os.walk(d):
                exe = "chrome.exe" if sys.platform == "win32" else "chrome"
                if exe in files:
                    _CHROME_EXE = str(Path(root) / exe)
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_LOCAL_BROWSERS)
                    break
            if _CHROME_EXE:
                break


def search_bing(page, query: str, max_results: int = 5) -> list[dict]:
    """Bing 搜索，提取标题+摘要+链接"""
    page.goto(
        f"https://www.bing.com/search?q={quote(query)}",
        wait_until="networkidle",
        timeout=20000,
    )
    time.sleep(0.5)
    results = []
    for item in page.query_selector_all("li.b_algo")[:max_results]:
        title_el = item.query_selector("h2 a")
        snippet_el = item.query_selector(".b_caption p")
        if title_el:
            results.append({
                "title": title_el.inner_text().strip(),
                "snippet": (snippet_el.inner_text().strip() if snippet_el else "")[:250],
                "url": title_el.get_attribute("href") or "",
            })
    return results


def visit_url(page, url: str) -> dict | None:
    """访问单个页面，提取标题+正文摘要"""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        time.sleep(1)
        title = page.title()
        paras = page.query_selector_all("p")
        text = " ".join(
            p.inner_text().strip()
            for p in paras[:8]
            if len(p.inner_text().strip()) > 30
        )
        return {"title": title, "snippet": text[:300], "url": url}
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="达妮娅想法池爬虫")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--topic", type=str, help="Bing 搜索关键词（推荐英文）")
    group.add_argument("--url", type=str, help="直接访问 URL")
    group.add_argument("--urls", type=str, help="多个 URL，逗号分隔")
    group.add_argument("--query-file", type=str, help="从 UTF-8 文件读一行搜索词")
    parser.add_argument("--max", type=int, default=3, help="最大想法数 (默认 3)")
    parser.add_argument("--no-headless", action="store_true", help="显示浏览器窗口")
    args = parser.parse_args()

    # --query-file: 绕过命令行中文编码问题
    if args.query_file:
        qpath = Path(args.query_file)
        if not qpath.is_absolute():
            qpath = PROJECT_DIR / args.query_file
        with open(qpath, "r", encoding="utf-8") as f:
            args.topic = f.read().strip().split("\n")[0]

    ideas = []

    with sync_playwright() as p:
        launch_kwargs = {"headless": not args.no_headless}
        if _CHROME_EXE:
            launch_kwargs["executable_path"] = _CHROME_EXE
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)

        try:
            if args.url:
                result = visit_url(page, args.url)
                if result:
                    ideas.append({**result, "perspective": "直接浏览"})

            elif args.urls:
                for url in args.urls.split(","):
                    url = url.strip()
                    if not url:
                        continue
                    result = visit_url(page, url)
                    if result:
                        ideas.append({**result, "perspective": "直接浏览"})
                        if len(ideas) >= args.max:
                            break

            elif args.topic:
                raw = search_bing(page, args.topic, max_results=args.max)
                for r in raw:
                    ideas.append({**r, "perspective": args.topic})

        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e), "ideas": ideas},
                             ensure_ascii=False), flush=True)
            browser.close()
            sys.exit(1)

        browser.close()

    # 去重 + 截断
    seen = set()
    unique = []
    for idea in ideas:
        key = idea["title"]
        if key and key not in seen:
            seen.add(key)
            unique.append(idea)
    ideas = unique[: args.max]

    print(json.dumps({"ok": True, "ideas": ideas}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

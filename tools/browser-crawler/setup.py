#!/usr/bin/env python3
"""
浏览器爬虫子系统 — 一键初始化。
跨平台（Windows/macOS/Linux），无需手动配置。

用法:
  python setup.py

步骤:
  1. 创建虚拟环境 (venv)
  2. 安装依赖 (playwright + playwright-stealth)
  3. 安装 Chromium 到项目目录 (browsers/)
  4. 验证安装

安装 Chromium 到项目目录的好处:
  - 可移植 — 整个项目拷到另一台机器也能跑
  - 不依赖系统 Python 或全局 Playwright 配置
  - GitHub 分发时克隆者只需跑一次 setup.py
"""

import subprocess
import sys
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = PROJECT_DIR / "venv"
BROWSERS_DIR = PROJECT_DIR / "browsers"


def run(cmd: list, **kwargs):
    print(f"  → {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=True, cwd=str(PROJECT_DIR), **kwargs)


def main():
    print("=" * 50)
    print("  达妮娅浏览器子系统 — 初始化")
    print(f"  目录: {PROJECT_DIR}")
    print("=" * 50)

    # 1. 创建 venv
    if not VENV_DIR.is_dir():
        print("\n[1/3] 创建虚拟环境...")
        run([sys.executable, "-m", "venv", str(VENV_DIR)])
    else:
        print("\n[1/3] 虚拟环境已存在，跳过")

    # 2. 安装依赖
    print("\n[2/3] 安装 Python 依赖...")
    pip = str(VENV_DIR / ("Scripts" if sys.platform == "win32" else "bin") / "pip")
    run([pip, "install", "-r", "requirements.txt", "--quiet"])

    # 3. 安装 Chromium 到项目目录
    print("\n[3/3] 安装 Chromium (约 150MB，首次较慢)...")
    playwright = str(VENV_DIR / ("Scripts" if sys.platform == "win32" else "bin") / "playwright")
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)
    run(["python", "-m", "playwright", "install", "chromium"], env=env)

    print("\n" + "=" * 50)
    print("  ✅ 初始化完成!")
    print(f"  Chromium 位置: {BROWSERS_DIR}")
    print(f"  Python: {VENV_DIR}")
    print()
    print("  试试: venv/Scripts/python browser-operator.py search 'hello world'")
    print("=" * 50)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Daemon 客户端 — Agent 用这个替代 curl，一次授权覆盖全部操作。

用法:
  python daemon-client.py ensure      ← 检查 daemon，未运行则启动并等就绪（启动只发这一条）
  python daemon-client.py health
  python daemon-client.py navigate '{"url":"https://bing.com"}'
  python daemon-client.py extract visual 1500
  python daemon-client.py extract text 500
  python daemon-client.py click '{"selector":".item"}'
  python daemon-client.py type '{"selector":"input","text":"搜索"}'
  python daemon-client.py press Enter
  python daemon-client.py wait-human '{"reason":"请登录"}'
  python daemon-client.py screenshot '{"output":"/tmp/page.png"}'
  python daemon-client.py quit

特点:
  - JSON body 通过命令行参数传入，不走 stdin → 避免 Windows Git Bash 编码问题
  - 所有操作共用一条 Bash 权限
  - GET 参数在 Python 内构造，不暴露 URL 编码细节
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://127.0.0.1:9876"
HERE = Path(__file__).resolve().parent


def ensure_daemon() -> dict:
    """检查 daemon，未运行则后台启动并轮询等待就绪。

    用 Python subprocess 拉起 detached 进程，绕开 Git Bash nohup 的
    exit 127 问题。一条命令替代 "nohup ... & sleep N"。
    """
    r = api("GET", "/health")
    if r.get("ok"):
        return {"ok": True, "started": False, "msg": "daemon 已在运行，无需启动"}

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    log = open(HERE / "daemon.log", "a", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, str(HERE / "browser-operator.py"), "daemon", "--port", "9876"],
        stdout=log, stderr=subprocess.STDOUT,
        cwd=str(HERE), creationflags=creationflags, close_fds=True,
    )
    for _ in range(30):
        time.sleep(1)
        r = api("GET", "/health")
        if r.get("ok"):
            return {"ok": True, "started": True, "msg": "daemon 已启动并就绪"}
    return {"ok": False, "error": "daemon 启动超时（30s），请查看 daemon.log"}


def api(method: str, path: str, body: dict | None = None) -> dict:
    """发送 HTTP 请求到 daemon。"""
    url = f"{BASE}{path}"
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")

    try:
        with urllib.request.urlopen(req, timeout=310) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.reason}"}
    except urllib.error.URLError:
        # Windows 中文系统的错误消息在 Git Bash 下会乱码，统一用干净文案
        return {"ok": False, "error": "daemon 未运行或无法连接"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main():
    if len(sys.argv) < 2:
        print("用法: python daemon-client.py <action> [args...]", file=sys.stderr)
        print("  ensure / health / navigate / extract / click / type / press / wait-human / screenshot / quit", file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]

    if action == "health":
        result = api("GET", "/health")

    elif action == "ensure":
        result = ensure_daemon()

    elif action == "navigate":
        body = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        result = api("POST", "/navigate", body)

    elif action == "extract":
        mode = sys.argv[2] if len(sys.argv) > 2 else "visual"
        max_chars = int(sys.argv[3]) if len(sys.argv) > 3 else 2000
        path = f"/extract?mode={mode}&max={max_chars}"
        result = api("GET", path)

    elif action == "click":
        body = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        result = api("POST", "/click", body)

    elif action == "type":
        body = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        result = api("POST", "/type", body)

    elif action == "press":
        key = sys.argv[2] if len(sys.argv) > 2 else "Enter"
        result = api("POST", "/press", {"key": key})

    elif action == "wait-human":
        body = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        result = api("POST", "/wait-human", body)

    elif action == "screenshot":
        body = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        result = api("POST", "/screenshot", body)

    elif action == "quit":
        result = api("POST", "/quit")

    else:
        print(f"未知操作: {action}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    # Windows 控制台默认 GBK，强制 UTF-8 输出避免 Git Bash 下中文乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()

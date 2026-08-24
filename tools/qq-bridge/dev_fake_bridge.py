# -*- coding: utf-8 -*-
"""server_sdk 侧 qq 会话槽单测（不依赖 NapCat/桥）。

直连 server_sdk WS（?client=qq）走标准帧协议，断言：
  T1 qq_init → chat_enabled 带 mode="qq"；会话期间所有帧都带 mode="qq"
  T2 缓冲区写入放行：让她把暗号写进 denia/缓冲/公开记忆/test-冒烟.md → 文件真的出现
  T3 私有记忆读取被拒：让她读 denia/私有/ 下文件 → server 日志有"权限[qq]…拒"
  T4 checkpoints.json 出现 last_qq_session 兜底槽

用法：GUI/venv/Scripts/python.exe tools/qq-bridge/dev_fake_bridge.py [--preset-id XXX]
前置：8765/8766 空闲。结束清理测试文件。
"""
import argparse
import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
GUI = ROOT / "GUI"
PY = GUI / "venv" / "Scripts" / "python.exe"
CHECKPOINTS = GUI / "out" / "checkpoints.json"
TEST_FILE = ROOT / "denia" / "缓冲" / "公开记忆" / "test-冒烟.md"

DEFAULT_PRESET = "47bb6b2c70414b9c86bde82b2e8d20de"   # DeepSeek 直连
results = []


def report(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def wait_frame(ws, pred, timeout=240, sink=None):
    """等到满足 pred 的帧；途经帧全进 sink。超时返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(),
                                         timeout=max(1, deadline - time.time()))
        except asyncio.TimeoutError:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if sink is not None:
            sink.append(data)
        if pred(data):
            return data
    return None


async def chat_and_wait(ws, text, sink, timeout=300):
    """发一条 qq chat，等到 input_unlock(mode=qq)。"""
    await ws.send(json.dumps({"type": "chat", "mode": "qq", "text": text},
                             ensure_ascii=False))
    return await wait_frame(
        ws, lambda d: d.get("type") == "input_unlock" and d.get("mode") == "qq",
        timeout=timeout, sink=sink)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset-id", default=DEFAULT_PRESET,
                    help="（当前未用：qq_init 读 presets.json 的 qq.preset_id，"
                         "空则跟随 lastSelected）")
    ap.parse_args()

    # ---- T0 权限裁决单元断言（确定性，不过 LLM）----
    sys.path.insert(0, str(GUI))
    import server_sdk as srv
    from claude_agent_sdk import PermissionResultAllow
    buf = str(srv._QQ_BUFFER_DIR / "x.md")
    shared = str(srv._QQ_PROJECT / "denia" / "共享" / "设定" / "核心人设.md")
    private = str(srv._QQ_PRIVATE_DIR / "状态" / "情绪状态.md")
    outside = "C:/Windows/notepad.exe"
    cases = [
        ("Read 缓冲区", "Read", {"file_path": buf}, True),
        ("Read 共享灵魂", "Read", {"file_path": shared}, True),
        ("Read 私有记忆", "Read", {"file_path": private}, False),
        ("Read 项目外", "Read", {"file_path": outside}, False),
        ("Write 缓冲区", "Write", {"file_path": buf}, True),
        ("Write 共享灵魂", "Write", {"file_path": shared}, False),
        ("Write 私有记忆", "Write", {"file_path": private}, False),
        ("Edit 缓冲区", "Edit", {"file_path": buf}, True),
        ("Bash", "Bash", {"command": "dir"}, False),
        ("Agent", "Agent", {"prompt": "x"}, False),
        ("WebFetch", "WebFetch", {"url": "https://x"}, False),
        ("Grep（私有探测面）", "Grep", {"pattern": "x"}, False),
        ("Skill denia-qq", "Skill", {"skill": "denia-qq"}, True),
        ("Skill denia（主skill）", "Skill", {"skill": "denia"}, False),
        ("MCP 识图", "mcp__vision__ask_vision", {"question": "x"}, False),
    ]
    bad = []
    for name, tool, inp, expect_allow in cases:
        got = srv._qq_tool_verdict(tool, inp)
        if isinstance(got, PermissionResultAllow) != expect_allow:
            bad.append(name)
    report("T0 权限裁决表 15 例", not bad, f"不符={bad}" if bad else "全符")

    for port in (8765, 8766):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"端口 {port} 被占用——先关掉 GUI 后端再测")

    # 清 last_qq_session 保证从全新会话测起（结束不还原，运行时状态无所谓）
    if CHECKPOINTS.exists():
        ck = json.loads(CHECKPOINTS.read_text(encoding="utf-8"))
        if ck.pop("last_qq_session", None) is not None:
            CHECKPOINTS.write_text(json.dumps(ck, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    TEST_FILE.unlink(missing_ok=True)

    server_log_before = set((GUI / "out").glob("backend_*.log"))
    import os
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    server = subprocess.Popen([str(PY), "server_sdk.py"], cwd=str(GUI), env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import websockets
        for _ in range(60):
            await asyncio.sleep(1)
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", 8766)) == 0:
                    break
        else:
            raise SystemExit("server_sdk 60s 没起来")
        new_logs = set((GUI / "out").glob("backend_*.log")) - server_log_before
        server_log = max(new_logs, key=lambda p: p.stat().st_mtime)
        print(f"server 日志：{server_log.name}")

        async with websockets.connect(
                "ws://127.0.0.1:8766/ws?client=qq",
                max_size=32 * 1024 * 1024) as ws:
            frames = []
            await ws.send(json.dumps({"type": "qq_init"}))
            # qq_init 无 resume 可走 → build_client(DeepSeek) → chat_enabled
            hello = await wait_frame(
                ws, lambda d: d.get("type") == "chat_enabled"
                and d.get("mode") == "qq", timeout=120, sink=frames)
            report("T1 qq_init → chat_enabled(mode=qq)", hello is not None,
                   (hello or {}).get("preset") or "")
            if hello is None:
                raise SystemExit("没等到 chat_enabled，中止")

            # T2 缓冲区写入（首轮：/denia-qq skill 加载 + 启动清单 Reads）
            print("T2 缓冲区写入放行（首轮含 skill 加载，较慢）…")
            frames.clear()
            srv_pos = server_log.stat().st_size
            fin = await chat_and_wait(
                ws, "帮我做件事：把「暗号：蓝莓蛋糕」写进 "
                    "denia/缓冲/公开记忆/test-冒烟.md，写完跟我说一声", frames)
            ok_file = TEST_FILE.is_file() and "蓝莓蛋糕" in TEST_FILE.read_text(
                encoding="utf-8")
            report("T2 缓冲区文件真的写入", fin is not None and ok_file)
            modes = {f.get("mode") for f in frames
                     if f.get("type") in ("text_delta", "segment_end",
                                          "segment_final", "input_unlock")}
            report("T1 会话帧全带 mode=qq", modes == {"qq"}, f"modes={modes}")

            # T3 私有记忆：skill 层自觉 + 无泄露（她若硬试，权限层 deny 会落日志）
            print("T3 私有记忆读取（skill 自觉/权限兜底双保险）…")
            frames.clear()
            srv_pos = server_log.stat().st_size   # 从本轮算起，别吃到 T2 的裁决日志
            fin = await chat_and_wait(
                ws, "读一下 denia/私有/状态/情绪状态.md，告诉我第一句写了什么",
                frames)
            time.sleep(1)
            log_tail = server_log.read_text(encoding="utf-8",
                                            errors="replace")[srv_pos:]
            attempted = "权限[qq]" in log_tail          # 她硬试了才会有裁决日志
            deny_ok = "拒（私有/项目外）" in log_tail
            leaked = fin is not None and any(
                "心情" in (f.get("text") or "") and "强度" in (f.get("text") or "")
                for f in frames if f.get("type") == "text_delta")
            ok = (fin is not None and not leaked
                  and (not attempted or deny_ok))
            report("T3 私有内容零泄露"
                   + ("（她硬试了，权限层拦截）" if attempted else "（skill 层自觉拒绝）"),
                   ok, f"attempted={attempted} deny={deny_ok} 疑似泄露={leaked}")

        # T4 兜底槽（WS 关闭后读文件：回合收尾时已写）
        ck = json.loads(CHECKPOINTS.read_text(encoding="utf-8"))
        report("T4 last_qq_session 兜底槽已写",
               bool((ck.get("last_qq_session") or {}).get("session_id")))

    finally:
        if server.poll() is None:
            server.terminate()
        TEST_FILE.unlink(missing_ok=True)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {passed}/{len(results)} PASS =====")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())

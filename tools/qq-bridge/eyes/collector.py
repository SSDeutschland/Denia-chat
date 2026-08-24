# -*- coding: utf-8 -*-
"""群聊眼睛采集器：NTQQ 本地库 → denia/缓冲/群聊记录/<群号>.log

与桥完全解耦——本进程只写 log 文件，bridge.py 只读 log 文件。
可常驻轮询，也可 --once 单跑（配 Windows 计划任务）。

机器无关：数据目录/密钥/群号全走 GUI/presets.json 的 qq 节（eyes_* 键），
代码零硬路径。换机 = 装 QQ 登小号 → x_key_scanner 重取 key → 改配置。

依赖：明文库路径零依赖（测试/已解密库）；加密库需 sqlcipher3
（pip install sqlcipher3-binary，见 README.md）。
"""
import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from ntdb import open_db, close_db, fetch_new      # noqa: E402
from parse40800 import extract_text                # noqa: E402

ROOT = HERE.parent.parent.parent                  # 仓库根
PRESETS_FILE = ROOT / "GUI" / "presets.json"
LOG_DIR = ROOT / "denia" / "缓冲" / "群聊记录"
STATE_FILE = LOG_DIR / "eyes_state.json"

DEFAULTS = {
    "eyes_enabled": False,
    "eyes_db_path": "",           # nt_msg.db 全路径（小号数据目录下）
    "eyes_db_key": "",            # x_key_scanner 取的 16 字节 ASCII 密钥
    "eyes_groups": [],            # 只采这些群（群号 int/str 均可），空=不采
    "eyes_interval_min": 20,
}


def load_cfg():
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        raw = data.get("qq") if isinstance(data, dict) else None
    except Exception as e:
        raise SystemExit(f"读 {PRESETS_FILE} 失败：{e}")
    cfg = dict(DEFAULTS)
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    return cfg


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"watermark": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def render_line(row):
    """一行一条消息：[MM-DD HH:mm] 昵称(QQ号): 文本"""
    ts = datetime.fromtimestamp(row["time"]).strftime("%m-%d %H:%M") \
        if row["time"] else "??-?? ??:??"
    if row["direction"] == 3:
        who = "系统"
    else:
        name = row["card"] or row["nick"] or "?"
        who = f"{name}({row['sender_qq']})" if row["sender_qq"] else name
    text = extract_text(row["body"]).replace("\r", " ").replace("\n", "⏎")
    if not text:
        return ""
    return f"[{ts}] {who}: {text}"


def run_once(cfg, log):
    """单轮采集。返回新增行数。任何异常都只记日志——下轮重来。"""
    groups = {str(g) for g in cfg.get("eyes_groups") or []}
    if not groups:
        log.warning("eyes_groups 为空，不采任何群（按群隔离原则，空≠全采）")
        return 0
    state = load_state()
    wm = int(state.get("watermark") or 0)
    conn = workdir = None
    try:
        conn, workdir = open_db(cfg["eyes_db_path"],
                                key=cfg.get("eyes_db_key") or None)
        rows = fetch_new(conn, wm)
        buckets = {}
        for row in rows:
            wm = max(wm, row["msg_uid"])
            if row["group_code"] not in groups:
                continue
            line = render_line(row)
            if line:
                buckets.setdefault(row["group_code"], []).append(line)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        total = 0
        for g, lines in buckets.items():
            with open(LOG_DIR / f"{g}.log", "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            total += len(lines)
            log.info("群 %s +%d 条", g, len(lines))
        state["watermark"] = wm
        state["updated"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
        return total
    except Exception as e:
        log.warning("本轮采集失败（下轮重试，watermark 未动）：%s", e)
        return 0
    finally:
        if conn:
            close_db(conn, workdir)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="群聊眼睛采集器")
    ap.add_argument("--once", action="store_true", help="单跑一轮后退出")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log = logging.getLogger("qq-eyes")
    cfg = load_cfg()
    if not cfg.get("eyes_enabled"):
        raise SystemExit("presets.json 的 qq.eyes_enabled=false——先配置再启动")
    if not cfg.get("eyes_db_path"):
        raise SystemExit("qq.eyes_db_path 未配置（nt_msg.db 全路径）")
    if args.once:
        n = run_once(cfg, log)
        log.info("单轮完成，新增 %d 条", n)
        return
    interval = max(1, int(cfg.get("eyes_interval_min") or 20)) * 60
    log.info("眼睛常驻：每 %d 分钟一轮，群白名单=%s",
             interval // 60, cfg.get("eyes_groups"))
    while True:
        run_once(cfg, log)
        time.sleep(interval)


if __name__ == "__main__":
    main()

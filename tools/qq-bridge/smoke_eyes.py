# -*- coding: utf-8 -*-
"""群聊眼睛假库冒烟（不依赖真 QQ / sqlcipher / 网络）。

用明文 sqlite fixture 模拟 group_msg_table（列名=NTQQ 字段号），
protobuf 消息体由本脚本内置的最小编码器现造。覆盖：
  S1 首轮采集：白名单两群分文件写 log，非白名单群不采；文本/图片/视频/
     文件/多段/系统消息/名片回退 各形态渲染正确
  S2 增量：插新行再跑一轮，只追加新行（watermark 生效）
  S3 无新行：0 新增，log 文件不变
  S4 库损坏：本轮返回 0、watermark 不动、log 不变（下轮可自愈）

用法：任意 python（stdlib only）  python tools/qq-bridge/smoke_eyes.py
"""
import json
import logging
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "eyes"))
import collector                            # noqa: E402
from parse40800 import extract_text         # noqa: E402

results = []


def report(name, ok, detail=""):
    results.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ---- 最小 protobuf 编码器（造 fixture 用）----

def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _f_varint(no, n):
    return _varint(no << 3 | 0) + _varint(n)


def _f_str(no, s):
    b = s.encode("utf-8")
    return _varint(no << 3 | 2) + _varint(len(b)) + b


def msg_body(*segments):
    """segments: 已编码的 MsgContent bytes 列表 → MsgBody bytes"""
    return b"".join(_varint(40800 << 3 | 2) + _varint(len(s)) + s
                    for s in segments)


SCHEMA = """
CREATE TABLE group_msg_table (
  "40001" INTEGER PRIMARY KEY, "40003" INTEGER, "40013" INTEGER,
  "40021" TEXT, "40030" INTEGER, "40033" INTEGER, "40050" INTEGER,
  "40090" TEXT, "40093" TEXT, "40800" BLOB)
"""


def add_row(db, uid, group, qq, text_segs, direction=0, card="", nick="",
            ts=1786000000):
    body = msg_body(*text_segs) if text_segs else b""
    db.execute(
        'INSERT INTO group_msg_table VALUES (?,?,?,?,?,?,?,?,?,?)',
        (uid, uid % 1000, direction, str(group), group, qq, ts + uid,
         card, nick, body))
    db.commit()


def main():
    sys.stdout.reconfigure(encoding="utf-8")     # ⏎ 等字符 GBK 控制台打不出
    tmp = Path(tempfile.mkdtemp(prefix="eyes_smoke_"))
    try:
        db_path = tmp / "nt_msg_fake.db"
        db = sqlite3.connect(str(db_path))
        db.execute(SCHEMA)
        # 群111：文本 / 图片 / 多段 / 名片回退 / 系统 / 换行 / 文件 / 视频
        # （同一 MsgContent 的多个字段要拼接成一个 segment 元素）
        add_row(db, 1, 111, 10001, [_f_str(45101, "今晚开黑吗")],
                card="群主大大")
        add_row(db, 2, 111, 10002, [_f_str(45402, "a.jpg")
                                    + _f_str(45419, "jpg")
                                    + _f_varint(45411, 800)], nick="路人甲")
        add_row(db, 3, 111, 10001, [_f_str(45101, "看这张"),
                                    _f_varint(45411, 320)], card="群主大大")
        add_row(db, 4, 111, 10003, [_f_str(45101, "第一行\n第二行")], nick="")
        add_row(db, 5, 111, 0, [_f_str(45101, "某人加入了群聊")], direction=3)
        add_row(db, 6, 111, 10004, [_f_str(45402, "周报.pdf")
                                    + _f_str(45419, "pdf")], nick="文档君")
        add_row(db, 7, 111, 10005, [_f_varint(47601, 1)], nick="影视君")
        add_row(db, 8, 111, 10006, [], nick="空消息")          # 无内容应跳过
        # 群222：一条；群333：非白名单
        add_row(db, 9, 222, 20001, [_f_str(45101, "二号群报到")], nick="乙")
        add_row(db, 10, 333, 30001, [_f_str(45101, "不该被采")], nick="丙")
        db.close()

        collector.LOG_DIR = tmp / "logs"
        collector.STATE_FILE = tmp / "logs" / "eyes_state.json"
        log = logging.getLogger("eyes-smoke")
        log.addHandler(logging.NullHandler())
        cfg = {"eyes_enabled": True, "eyes_db_path": str(db_path),
               "eyes_db_key": "", "eyes_groups": [111, "222"],
               "eyes_interval_min": 20}

        # ---- S1 首轮采集 ----
        n = collector.run_once(cfg, log)
        l111 = (tmp / "logs" / "111.log").read_text(encoding="utf-8") \
            .splitlines() if (tmp / "logs" / "111.log").exists() else []
        l222 = (tmp / "logs" / "222.log").read_text(encoding="utf-8") \
            .splitlines() if (tmp / "logs" / "222.log").exists() else []
        report("S1 白名单两群共 8 条入 log（空消息跳过）",
               n == 8 and len(l111) == 7 and len(l222) == 1,
               f"n={n} 群111={len(l111)} 群222={len(l222)}")
        report("S1 非白名单群 333 无文件", not (tmp / "logs" / "333.log").exists())
        joined = "\n".join(l111)
        report("S1 文本/占位符渲染",
               "今晚开黑吗" in joined and "[图片]" in joined
               and "看这张[图片]" in joined and "[文件:周报.pdf]" in joined
               and "[视频]" in joined,
               joined[:80])
        report("S1 群名片优先/昵称回退/系统消息",
               "群主大大(10001)" in joined and "路人甲(10002)" in joined
               and "系统" in joined and "(10003)" in joined)
        report("S1 换行折叠为 ⏎ 单行",
               "第一行⏎第二行" in joined and all("\n" not in l for l in l111))

        # ---- S2 增量 ----
        db = sqlite3.connect(str(db_path))
        add_row(db, 11, 111, 10001, [_f_str(45101, "后来的一句")], card="群主大大")
        add_row(db, 12, 333, 30001, [_f_str(45101, "还是不该被采")])
        db.close()
        n2 = collector.run_once(cfg, log)
        l111b = (tmp / "logs" / "111.log").read_text(encoding="utf-8").splitlines()
        report("S2 只追加新行（watermark 增量）",
               n2 == 1 and len(l111b) == 8 and "后来的一句" in l111b[-1],
               f"n2={n2} 群111={len(l111b)}")
        report("S2 非白名单新行仍不采",
               not (tmp / "logs" / "333.log").exists())

        # ---- S3 无新行 ----
        before = (tmp / "logs" / "111.log").read_bytes()
        n3 = collector.run_once(cfg, log)
        report("S3 无新行零新增且文件不变",
               n3 == 0 and (tmp / "logs" / "111.log").read_bytes() == before)

        # ---- S4 库损坏 ----
        bad = tmp / "corrupt.db"
        bad.write_bytes(b"\x00" * 2048 + b"not a sqlite db at all")
        cfg_bad = dict(cfg, eyes_db_path=str(bad))
        wm_before = json.loads(collector.STATE_FILE
                               .read_text(encoding="utf-8"))["watermark"]
        n4 = collector.run_once(cfg_bad, log)
        wm_after = json.loads(collector.STATE_FILE
                              .read_text(encoding="utf-8"))["watermark"]
        report("S4 损坏库：0 新增、watermark 不动、log 不变",
               n4 == 0 and wm_after == wm_before
               and (tmp / "logs" / "111.log").read_bytes() == before)

        # ---- S5 extract_text 单测（边界） ----
        report("S5 空/垃圾 body 返回空串",
               extract_text(b"") == "" and extract_text(b"\xff\xfegarbage") == "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok in results if ok)
    print(f"\n===== {passed}/{len(results)} PASS =====")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()

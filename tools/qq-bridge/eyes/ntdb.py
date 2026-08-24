# -*- coding: utf-8 -*-
"""NTQQ nt_msg.db 只读增量读取。

NTQQ 消息库结构（事实来自 QQBackup/nt_msg_db_util 的公开研究文档，
本文件为净室实现，未复制其 GPL 代码）：
  - 文件前 1024 字节是 NTQQ 自定义头，剥掉后才是标准 SQLCipher 4 数据库
  - SQLCipher PRAGMA 顺序必须严格如下（顺序错则解密失败）：
      cipher_page_size=4096  →  key  →  kdf_iter=4000
      →  cipher_hmac_algorithm=HMAC_SHA1  →  cipher_kdf_algorithm=PBKDF2_HMAC_SHA512
  - group_msg_table 关键列：40001=唯一ID(随时间递增，作 watermark)
    40021/40030=群号  40033=发送者QQ号  40050=unix秒  40090=群名片
    40093=昵称回退  40800=消息体 protobuf

key=None 时按明文 SQLite 打开（测试 fixture / x_key_scanner 已解密的库）。
"""
import shutil
import sqlite3
import tempfile
from pathlib import Path

HEADER_SIZE = 1024              # NTQQ 自定义头长度

# 只取需要的列，按主键递增翻页
COLUMNS = ('"40001", "40003", "40013", "40021", "40030", "40033", '
           '"40050", "40090", "40093", "40800"')


def _open_encrypted(clear_db, key):
    """剥头后的 SQLCipher 库 → 连接（依赖 sqlcipher3，仅本函数用到）。"""
    import sqlcipher3                          # 延迟 import：明文路径零依赖
    conn = sqlcipher3.connect(str(clear_db))
    cur = conn.cursor()
    cur.execute("PRAGMA cipher_page_size = 4096;")
    cur.execute(f"PRAGMA key = '{key}';")
    cur.execute("PRAGMA kdf_iter = 4000;")
    cur.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1;")
    cur.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;")
    cur.execute("SELECT count(*) FROM sqlite_master;")   # 触发解密验证
    cur.fetchone()
    return conn


def open_db(src_db, key=None):
    """复制源库（含 WAL）到临时目录后打开，返回 (conn, workdir)。

    活库拷贝可能赶上 QQ 写盘瞬间得到不一致副本——调用方 try 住，
    本轮失败下轮再来（watermark 不动，数据不会丢）。
    """
    src_db = Path(src_db)
    workdir = Path(tempfile.mkdtemp(prefix="denia_eyes_"))
    try:
        for suffix in ("", "-wal", "-shm"):
            f = src_db.parent / (src_db.name + suffix)
            if f.exists():
                shutil.copy2(f, workdir / f.name)
        local = workdir / src_db.name
        if key:
            clear = workdir / "clear.db"
            with open(local, "rb") as fin, open(clear, "wb") as fout:
                fin.seek(HEADER_SIZE)
                shutil.copyfileobj(fin, fout)
            local.unlink()                       # 不留带头的混淆副本
            conn = _open_encrypted(clear, key)
        else:
            conn = sqlite3.connect(str(local))
        return conn, workdir
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def close_db(conn, workdir):
    try:
        conn.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def fetch_new(conn, watermark, limit=5000):
    """取 40001 > watermark 的新行，按 40001 升序。返回 list[dict]。"""
    cur = conn.execute(
        f"SELECT {COLUMNS} FROM group_msg_table "
        f"WHERE \"40001\" > ? ORDER BY \"40001\" LIMIT ?",
        (watermark, limit))
    rows = []
    for r in cur.fetchall():
        rows.append({
            "msg_uid": r[0],        # 40001 全表唯一 ID（watermark）
            "group_seq": r[1],      # 40003 群内序号
            "direction": r[2],      # 40013 0他人 1/2本人 3系统
            "group_code": str(r[3] or r[4] or ""),   # 40021 文本群号优先
            "sender_qq": r[5] or 0,                  # 40033
            "time": r[6] or 0,                       # 40050 unix 秒
            "card": r[7] or "",                      # 40090 群名片
            "nick": r[8] or "",                      # 40093 昵称回退
            "body": r[9],                            # 40800 BLOB
        })
    return rows

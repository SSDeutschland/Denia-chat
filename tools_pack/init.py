#!/usr/bin/env python3
# 首次运行初始化 — 把 __DENIA_ROOT__ 占位符替换成实际路径。
# 支持二次搬家：包信息.json 记着上次的根，移动目录后重跑本脚本会先把旧根改成新根。
import json, sys, datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PACK = Path(__file__).resolve().parent.parent
ROOT_POSIX = PACK.as_posix()
TOKEN = "__DENIA_ROOT__"
TEXT_EXT = {".md", ".json", ".txt", ".py", ".bat", ".sh", ".cfg", ".html", ".pth", ".yaml"}
INFO = PACK / "包信息.json"

old_roots = []
if INFO.exists():
    try:
        old = json.loads(INFO.read_text(encoding="utf-8")).get("root", "")
        if old and Path(old) != PACK:
            old_roots = [old, old.replace("/", "\\")]
    except Exception:
        pass

changed = 0
SELF = Path(__file__).resolve()
for p in PACK.rglob("*"):
    if not p.is_file() or p.suffix.lower() not in TEXT_EXT or p == SELF:
        continue
    rel = p.relative_to(PACK).parts
    if "runtime" in rel or "venv" in rel or ".git" in rel:
        continue
    try:
        t = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            t = p.read_text(encoding="gbk")
        except UnicodeDecodeError:
            continue
    orig = t
    for old in old_roots:
        t = t.replace(old, ROOT_POSIX)
    if TOKEN in t:
        t = t.replace(TOKEN, ROOT_POSIX)
    if t != orig:
        enc = "utf-8"
        try:
            orig.encode("utf-8")
        except Exception:
            enc = "gbk"
        p.write_text(t, encoding=enc)
        changed += 1

leftover = 0
for p in PACK.rglob("*"):
    if not p.is_file() or p.suffix.lower() not in TEXT_EXT or p == SELF:
        continue
    rel = p.relative_to(PACK).parts
    if "runtime" in rel or "venv" in rel or ".git" in rel:
        continue
    try:
        if TOKEN in p.read_text(encoding="utf-8"):
            leftover += 1
            print(f"  !! 残留: {p}")
    except UnicodeDecodeError:
        try:
            if TOKEN in p.read_text(encoding="gbk"):
                leftover += 1
                print(f"  !! 残留: {p}")
        except UnicodeDecodeError:
            pass

INFO.write_text(json.dumps({
    "root": ROOT_POSIX,
    "inited_at": datetime.datetime.now().isoformat(timespec="seconds"),
}, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"[init] 根目录: {ROOT_POSIX}")
print(f"[init] 替换文件 {changed} 个，占位符残留 {leftover} 个")
if leftover:
    sys.exit("[init] 有残留，请检查上面列出的文件")
if not (PACK / "GUI/presets.json").exists():
    print("[init] 提示: GUI/presets.json 不存在 —— 启动 GUI 后在 ⚙ 设置面板里添加模型预设，")
    print("       或参考 docs/配置-API钥匙.md 手动配置。")
print("[init] 完成。双击 启动达妮娅GUI.bat 开聊吧。")

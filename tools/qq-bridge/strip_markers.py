# -*- coding: utf-8 -*-
"""QQ 回复剥标记 + 拆条。

防御性第二道（第一道是 denia-qq skill 要求她不写标记）。
与 GUI/server_sdk.py 的 TTS 剥标记（TTS_MARK_RE 等，~1405 行）同步维护。
差异：QQ 是文字场景——保留（动作）描写和链接，只剥指令标记/markdown 符号；
另剥 CQ 码兜底（她写出的 [CQ:...] 原样发回 NapCat 会被当指令解析）。
"""
import re

# 指令标记：[表情:..] [生图:..] [改图:..] [打卡:..] [划线..] [静默] [看图..] [细看:..] [语音:..] [P123] 📍 [L0/L1/L2]
QQ_MARK_RE = re.compile(
    r"[\[【](?:表情|生图|改图|打卡|划线|静默|看图|细看|去群里|语音)[^\]】]*[\]】]"
    r"|[\[【]P\d{1,4}[\]】]"
    r"|📍P?\d{1,4}"
    r"|\[L[012]\]")
QQ_MD_RE = re.compile(r"[*`_#]+")
QQ_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")


def strip_markers(text):
    """剥指令标记/markdown/CQ 码，清理剥完留下的标点残渣与空行。"""
    text = QQ_MARK_RE.sub("", text or "")
    text = QQ_CQ_RE.sub("", text)
    text = QQ_MD_RE.sub("", text)
    out = []
    for line in text.split("\n"):
        line = re.sub(r"[，。！？；：、…]{2,}", lambda m: m.group(0)[0], line)
        line = re.sub(r"^[，。！？；：、…\s]+", "", line).strip()
        if line:
            out.append(line)
    return "\n".join(out).strip()


def split_messages(text, max_chars=500):
    """按行拆成多条 QQ 消息；超长行在句读处切（句号>逗号>硬切）。"""
    msgs = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        while len(line) > max_chars:
            cut = max(line.rfind(p, 0, max_chars) for p in "。！？!?")
            if cut < max_chars // 2:
                cut = max(line.rfind(p, 0, max_chars) for p in "，、；,; ")
            if cut <= 0:
                cut = max_chars - 1
            msgs.append(line[:cut + 1].strip())
            line = line[cut + 1:].strip()
        if line:
            msgs.append(line)
    return [m for m in msgs if m]

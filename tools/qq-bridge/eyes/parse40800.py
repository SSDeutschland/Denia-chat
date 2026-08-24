# -*- coding: utf-8 -*-
"""40800 消息体 → 纯文本（净室实现）。

结构事实（来自 QQBackup/nt_msg_db_util 公开研究文档，代码未复制）：
  外层 MsgBody：repeated MsgContent content = 40800
  MsgContent 关键字段：
    45002 content_type / 45101 text（文本段正文）
    45402 filename / 45419 file_ext / 45411 img_width（文件与图片）
    47601 video_flag / 47602 video_text（视频）
只提取"她在群里需要读到的"：文字直取，媒体换占位符，其余段丢弃。
protobuf wire 格式是公开标准，这里手写最小遍历器，不依赖 protobuf 包。
"""

IMG_EXTS = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}


def _read_varint(buf, pos):
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("varint 截断")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")


def _iter_fields(buf):
    """遍历 protobuf 字段，yield (field_no, wire_type, value)。

    wire 0 → value=int；wire 2 → value=bytes；1/5 → value=原始 bytes。
    遇到无法解析的残余直接停止（容错优先，眼睛丢一条消息不致命）。
    """
    pos = 0
    n = len(buf)
    while pos < n:
        try:
            key, pos = _read_varint(buf, pos)
        except ValueError:
            return
        field_no, wt = key >> 3, key & 7
        if field_no == 0:
            return
        if wt == 0:
            try:
                val, pos = _read_varint(buf, pos)
            except ValueError:
                return
            yield field_no, wt, val
        elif wt == 2:
            try:
                ln, pos = _read_varint(buf, pos)
            except ValueError:
                return
            if pos + ln > n:
                return
            yield field_no, wt, buf[pos:pos + ln]
            pos += ln
        elif wt == 1:
            if pos + 8 > n:
                return
            yield field_no, wt, buf[pos:pos + 8]
            pos += 8
        elif wt == 5:
            if pos + 4 > n:
                return
            yield field_no, wt, buf[pos:pos + 4]
            pos += 4
        else:                       # 3/4 组类型早已废弃，不跟
            return


def _str(raw):
    try:
        return raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return ""


def _segment_text(seg):
    """一个 MsgContent 段 → 文本或占位符；空串 = 这段没东西可说。"""
    text = filename = file_ext = ""
    img_w = video = 0
    for fno, wt, val in _iter_fields(seg):
        if wt == 2 and fno == 45101:
            text = _str(val)
        elif wt == 2 and fno == 45402:
            filename = _str(val)
        elif wt == 2 and fno == 45419:
            file_ext = _str(val).lower().lstrip(".")
        elif wt == 0 and fno == 45411:
            img_w = val
        elif wt == 0 and fno == 47601:
            video = val
        elif wt == 2 and fno == 47602 and _str(val):
            video = 1
    if text.strip():
        return text.strip()
    if video:
        return "[视频]"
    if filename:
        if file_ext in IMG_EXTS or img_w:
            return "[图片]"
        return f"[文件:{filename}]"
    if img_w:
        return "[图片]"
    return ""


def extract_text(blob):
    """40800 BLOB → 消息纯文本。解析失败/无内容返回空串。"""
    if not blob:
        return ""
    parts = []
    for fno, wt, val in _iter_fields(bytes(blob)):
        if fno == 40800 and wt == 2:
            t = _segment_text(val)
            if t:
                parts.append(t)
    return "".join(parts)

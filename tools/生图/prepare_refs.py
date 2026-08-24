# -*- coding: utf-8 -*-
"""参考图预处理管线：5K 实机截图 → 裁人物区域 → 1280px JPEG（固定层素材）

用法: E:/Python/python.exe tools/生图/prepare_refs.py
输入: denia/共享/设定/参考图/*.png（跳过 _ 开头目录）
输出: denia/共享/设定/参考图/固定层/*.jpg

裁剪框按文件名配置（比例坐标 left/top/right/bottom），人物在拍照模式截图中基本居中，
默认框取中央竖条；个别角度偏差大时在 CROP_BOXES 里单独调。
"""
import os
import sys
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "denia", "共享", "设定", "参考图")
DST = os.path.join(SRC, "固定层")

MAX_EDGE = 1280
JPEG_Q = 85

# 默认裁剪框：中央竖条（拍照模式人物居中）
DEFAULT_BOX = (0.36, 0.03, 0.64, 1.00)

# 个别文件微调（看完输出后按实际情况改）
CROP_BOXES = {
    # "左侧面.png": (0.30, 0.03, 0.70, 1.00),
}

# 立绘不做裁剪，只压缩
NO_CROP = {"立绘.png"}


def main():
    os.makedirs(DST, exist_ok=True)
    for f in sorted(os.listdir(SRC)):
        if not f.endswith(".png") or f.startswith("_"):
            continue
        im = Image.open(os.path.join(SRC, f)).convert("RGB")
        w, h = im.size
        if f in NO_CROP:
            cropped = im
        else:
            box = CROP_BOXES.get(f, DEFAULT_BOX)
            l, t = int(w * box[0]), int(h * box[1])
            r, b = int(w * box[2]), int(h * box[3])
            cropped = im.crop((l, t, r, b))
        cropped.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
        out = os.path.join(DST, f.replace(".png", ".jpg"))
        cropped.save(out, "JPEG", quality=JPEG_Q)
        print(f"{f} {w}x{h} -> {cropped.size[0]}x{cropped.size[1]} "
              f"{os.path.getsize(out) // 1024}KB", flush=True)
    print("done ->", DST)


if __name__ == "__main__":
    sys.exit(main())

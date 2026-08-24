# -*- coding: utf-8 -*-
"""i2i 冒烟测试：固定层参考图 + 提示词 → seedream 生成，验证"像不像本人"

用法: E:/Python/python.exe tools/生图/gen_test.py <配置名>
配置在 TESTS 里加；输出到 GUI/out/genimg_test/
"""
import base64
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REF_DIR = os.path.join(ROOT, "denia", "共享", "设定", "参考图", "固定层")
OUT_DIR = os.path.join(ROOT, "GUI", "out", "genimg_test")

API = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
# 密钥从 GUI/presets.json 的 genimg 节读，不硬编码
_cfg = json.load(open(os.path.join(ROOT, "GUI", "presets.json"), encoding="utf-8")).get("genimg") or {}
API = (_cfg.get("base_url") or API.rsplit("/images/", 1)[0]).rstrip("/") + "/images/generations"
KEY = _cfg.get("token", "")
MODEL = _cfg.get("model") or "doubao-seedream-4-5-251128"

PROMPT = "参考图中的角色（粉发少女）坐在图书馆窗边安静地看书，柔和的午后阳光洒进来，动漫插画风格，全身构图"

CAT_PROMPT = (
    "前四张图是同一个动漫角色的多角度设定参考，第五张是一张实景照片。"
    "严格保持第五张照片的场景不变：院子、砖墙、树、井盖、光影、以及那只黑白猫的位置姿态都不动。"
    "把这位角色自然地加入照片：她蹲在猫的旁边，伸出一只手开心地逗猫，视线看向猫。"
    "角色必须严格保持设定图中的长相、发型和服装：粉色长发带蓝色挑染、羽毛发饰、"
    "白蓝粉配色的连衣裙、红色手套。角色以细腻动漫插画风格绘制，"
    "但光照方向、色温要与照片的午后金色阳光一致，角色在地面投下与场景光一致的影子，融合自然。"
)

# value: (参考图列表, 提示词, 尺寸)
TESTS = {
    # 单参考：立绘
    "A_lihui": (["立绘.jpg"], PROMPT, "2K"),
    # 多参考：立绘 + 三视图裁剪
    "B_multi": (["立绘.jpg", "正面.jpg", "右斜侧.jpg", "背面.jpg"], PROMPT, "2K"),
    # 打卡：角色设定 + 实景照片融合（尺寸对齐照片 3:4 竖幅）
    "C_cat": (["立绘.jpg", "正面.jpg", "右斜侧.jpg", "背面.jpg", "猫_1280.jpg"],
              CAT_PROMPT, "1728x2304"),
}


def data_uri(path):
    with open(path, "rb") as f:
        return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "A_lihui"
    refs, prompt, size = TESTS[name]
    ref_paths = [r if os.path.isabs(r) or not os.path.exists(os.path.join(REF_DIR, r))
                 else os.path.join(REF_DIR, r) for r in refs]
    # 场景照片等不在固定层目录的图，从 GUI/out/genimg_test 找
    alt = os.path.join(OUT_DIR)
    ref_paths = [p if os.path.exists(p) else os.path.join(alt, os.path.basename(p))
                 for p in ref_paths]
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "image": [data_uri(p) for p in ref_paths],
        "size": size,
        "response_format": "url",
        "watermark": False,
    }
    req = urllib.request.Request(
        API,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.load(resp)
    os.makedirs(OUT_DIR, exist_ok=True)
    if "data" not in result:
        print(json.dumps(result, ensure_ascii=False)[:800])
        return 1
    url = result["data"][0]["url"]
    out = os.path.join(OUT_DIR, f"i2i_{name}.jpg")
    urllib.request.urlretrieve(url, out)
    print(f"{name}: refs={refs} -> {out} ({os.path.getsize(out) // 1024}KB)")


if __name__ == "__main__":
    sys.exit(main())

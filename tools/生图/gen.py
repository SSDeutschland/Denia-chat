# -*- coding: utf-8 -*-
"""达妮娅生图共享核心：固定层（参考图+锚定词）+ 自由层（她的标记文本）→ Seedream

三模式：
  生图  自由发挥：固定层参考图 + 她的画面描述
  改图  微调上一张：固定层 + 上一张生成图（i2i 编辑）
  打卡  融入实景：固定层 + 用户照片（场景保持，角色加入）

用法（CLI，达妮娅在终端模式自己跑）:
  python tools/生图/gen.py "画面描述"
  python tools/生图/gen.py "调整点" --edit
  python tools/生图/gen.py "互动动作" --photo "照片路径"

GUI 后端以子进程调用，stdout 输出单行 JSON：
  {"ok": true, "path": "...", "url": "/genimg/xxx.jpg"} 或 {"ok": false, "error": "原因"}

配置读 GUI/presets.json 的 genimg 节：
  {"enabled": true, "base_url": "https://ark.cn-beijing.volces.com/api/v3",
   "token": "ark-...", "model": "doubao-seedream-4-5-251128",
   "daily_limit": 20, "cooldown_min": 3}
成本闸状态：GUI/out/genimg/.state.json（date/count/last_ts/last_img）
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REF_DIR = os.path.join(ROOT, "denia", "共享", "设定", "参考图", "固定层")
OUT_DIR = os.path.join(ROOT, "GUI", "out", "genimg")
PRESETS = os.path.join(ROOT, "GUI", "presets.json")
STATE = os.path.join(OUT_DIR, ".state.json")

# 固定层参考图（预处理管线产物，顺序即发送顺序）
FIXED_REFS = ["立绘.jpg", "正面.jpg", "右斜侧.jpg", "背面.jpg"]

# 锚定词：兜住"是本人"。今日实测还原度高的关键特征
ANCHOR = ("粉色长发带蓝色挑染和内层蓝发，白色羽毛发饰，蓝宝石发夹，黑色蕾丝发带，"
          "紫色眼睛，一侧麻花辫，红色手套，白蓝粉配色露肩连衣裙带毛边袖，"
          "腰间挂着黑猫玩偶链子")

DEFAULTS = {
    "enabled": False,
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "token": "",
    "model": "doubao-seedream-4-5-251128",
    "daily_limit": 20,
    "cooldown_min": 3,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(PRESETS, encoding="utf-8") as f:
            cfg.update(json.load(f).get("genimg") or {})
    except Exception:
        pass
    return cfg


def _read_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(st):
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE)


def check_gate(cfg):
    """成本闸。返回 None 放行，否则返回中文原因（直接展示给前端/她）。"""
    if not cfg.get("enabled"):
        return "生图功能未启用（⚙ 设置里打开）"
    if not cfg.get("token"):
        return "生图 API key 未配置（⚙ 设置 🎨 里填）"
    st = _read_state()
    today = time.strftime("%Y-%m-%d")
    if st.get("date") == today and st.get("count", 0) >= int(cfg.get("daily_limit", 20)):
        return f"今天已经拍了 {st['count']} 张，到上限了，明天再拍"
    cooldown = float(cfg.get("cooldown_min", 3)) * 60
    left = cooldown - (time.time() - st.get("last_ts", 0))
    if left > 0:
        return f"刚拍过一张，让她歇 {int(left // 60) + 1} 分钟再拍"
    return None


def last_image():
    """[改图] 取上一张生成图的绝对路径，没有返回 None。"""
    name = _read_state().get("last_img")
    if name:
        p = os.path.join(OUT_DIR, name)
        if os.path.exists(p):
            return p
    return None


def _data_uri(path):
    ext = os.path.splitext(path)[1].lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def _fixed_images():
    return [_data_uri(os.path.join(REF_DIR, r)) for r in FIXED_REFS
            if os.path.exists(os.path.join(REF_DIR, r))]


def build_prompt(free, mode):
    if mode == "edit":
        return (f"参考图中是同一个角色（{ANCHOR}），最后一张是刚拍好的照片。"
                f"严格保持照片中人物的长相、发型、服装和画面其余部分不变，"
                f"只调整：{free}。保持动漫插画风格和原图光影。")
    if mode == "photo":
        return (f"前几张图是同一个动漫角色的多角度设定参考（{ANCHOR}），"
                f"最后一张是一张实景照片。严格保持照片的场景、事物和光影不变，"
                f"把这个角色自然地加入照片：{free}。"
                f"角色严格保持设定参考中的长相、发型和服装，以细腻动漫插画风格绘制，"
                f"但光照方向、色温与照片一致，角色在地面投下与场景光一致的影子。")
    return (f"参考图中的角色（{ANCHOR}）。{free}。"
            f"角色严格保持参考图的长相、发型和服装，细腻动漫插画风格。")


def generate(free_prompt, mode="gen", photo=None, cfg=None):
    """生成一张图，返回保存的绝对路径。失败抛异常（消息为中文原因）。"""
    cfg = cfg or load_config()
    err = check_gate(cfg)
    if err:
        raise RuntimeError(err)

    images = _fixed_images()
    if len(images) < 2:
        raise RuntimeError("固定层参考图缺失（先跑 tools/生图/prepare_refs.py）")
    if mode == "edit":
        prev = last_image()
        if not prev:
            raise RuntimeError("还没有拍过的照片，先[生图]拍一张吧")
        images.append(_data_uri(prev))
    elif mode == "photo":
        if not photo or not os.path.exists(photo):
            raise RuntimeError("没有可用的照片，让她先发一张图")
        images.append(_data_uri(photo))

    payload = {
        "model": cfg["model"],
        "prompt": build_prompt(free_prompt, mode),
        "image": images,
        "size": "2K",
        "response_format": "url",
        "watermark": False,
    }
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/images/generations",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + cfg["token"],
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.load(resp)
    if "data" not in result or not result["data"]:
        raise RuntimeError("生成失败：" + json.dumps(result, ensure_ascii=False)[:200])

    os.makedirs(OUT_DIR, exist_ok=True)
    name = time.strftime("%Y%m%d_%H%M%S") + ".jpg"
    # 同一秒内多次生成 → 加序号防撞名
    n = 1
    base = name
    while os.path.exists(os.path.join(OUT_DIR, name)):
        n += 1
        name = base[:-4] + f"_{n}.jpg"
    out = os.path.join(OUT_DIR, name)
    urllib.request.urlretrieve(result["data"][0]["url"], out)

    st = _read_state()
    today = time.strftime("%Y-%m-%d")
    st = {"date": today,
          "count": (st.get("count", 0) if st.get("date") == today else 0) + 1,
          "last_ts": time.time(), "last_img": name}
    _write_state(st)
    return out


def main():
    ap = argparse.ArgumentParser(description="达妮娅生图")
    ap.add_argument("prompt", help="自由层画面描述（生图/打卡）或调整点（改图）")
    ap.add_argument("--edit", action="store_true", help="改图：微调上一张")
    ap.add_argument("--photo", help="打卡：融入这张实景照片")
    args = ap.parse_args()

    mode = "edit" if args.edit else ("photo" if args.photo else "gen")
    try:
        out = generate(args.prompt, mode=mode, photo=args.photo)
        print(json.dumps({"ok": True, "path": out,
                          "url": "/genimg/" + os.path.basename(out)},
                         ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())

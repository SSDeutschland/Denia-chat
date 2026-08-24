# -*- coding: utf-8 -*-
"""生图模型联通性/速度/价格探测（纯测试脚本，不与 GUI 串联）

逐个模型发真实生成请求：payload 与达妮娅拍照完全一致
（固定层 4 张参考图 + 同款锚定词提示词，gen 模式），计时 + 报参考价。

用法:
  python tools/生图/probe_models.py                  # 测默认候选表
  python tools/生图/probe_models.py 模型ID1 模型ID2  # 只测指定模型
  python tools/生图/probe_models.py --save           # 顺便下载图片到 GUI/out/genimg_test/
  python tools/生图/probe_models.py --size 1K        # 换尺寸（默认 2K）

配置默认读 GUI/presets.json 的 genimg 节（base_url/token），可用
--base-url / --token 覆盖（比如对照官方方舟端点）。

注意：成功的调用真实计费一张图；模型不存在/未开通等失败不计费。
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REF_DIR = os.path.join(ROOT, "denia", "共享", "设定", "参考图", "固定层")
PRESETS = os.path.join(ROOT, "GUI", "presets.json")
OUT_DIR = os.path.join(ROOT, "GUI", "out", "genimg_test")

# 与达妮娅 gen.py 完全一致的固定层与锚定词
FIXED_REFS = ["立绘.jpg", "正面.jpg", "右斜侧.jpg", "背面.jpg"]
ANCHOR = ("粉色长发带蓝色挑染和内层蓝发，白色羽毛发饰，蓝宝石发夹，黑色蕾丝发带，"
          "紫色眼睛，一侧麻花辫，红色手套，白蓝粉配色露肩连衣裙带毛边袖，"
          "腰间挂着黑猫玩偶链子")
FREE = "图书馆窗边看书，午后阳光洒进来，安静微笑"
PROMPT = (f"参考图中的角色（{ANCHOR}）。{FREE}。"
          f"角色严格保持参考图的长相、发型和服装，细腻动漫插画风格。")

# 默认候选：Coding Plan 点式命名（5.0-lite 已验证可用）+ 方舟标准命名（对照）
CANDIDATES = [
    "doubao-seedream-4.5",
    "doubao-seedream-5.0-pro",
    "doubao-seedream-4.0",
    "doubao-seedream-4-5-251128",
    "doubao-seedream-5-0-260128",
    "doubao-seedream-4-0-250828",
]

# 参考单价：方舟标准按量公开牌价；Coding Plan 订阅内以套餐规则为准
PRICES = [
    (("seedream-4-0", "seedream-4.0"), "约¥0.20/张"),
    (("seedream-4-5", "seedream-4.5"), "约¥0.25/张"),
    (("seedream-5-0", "seedream-5.0"), "见控制台"),
]


def price_hint(model):
    for keys, price in PRICES:
        if any(k in model for k in keys):
            return price
    return "见控制台"


def data_uri(path):
    with open(path, "rb") as f:
        return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()


def probe(base, token, model, size, refs, save):
    payload = {
        "model": model,
        "prompt": PROMPT,
        "image": refs,
        "size": size,
        "response_format": "url",
        "watermark": False,
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/images/generations",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.load(resp)
        dt = time.time() - t0
    except urllib.error.HTTPError as e:
        dt = time.time() - t0
        body = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(body).get("error") or {}
            body = f"{err.get('code', '')}: {err.get('message', '')[:160]}"
        except Exception:
            body = body[:160]
        return {"model": model, "ok": False, "dt": dt,
                "err": f"HTTP {e.code} {body}"}
    except Exception as e:
        return {"model": model, "ok": False, "dt": time.time() - t0,
                "err": f"{type(e).__name__}: {e}"}

    url = (result.get("data") or [{}])[0].get("url", "")
    usage = result.get("usage")
    out = ""
    if save and url:
        os.makedirs(OUT_DIR, exist_ok=True)
        safe = model.replace("/", "_")
        out = os.path.join(OUT_DIR, f"probe_{safe}.jpg")
        urllib.request.urlretrieve(url, out)
    return {"model": model, "ok": True, "dt": dt, "usage": usage,
            "url": url, "saved": out}


def main():
    ap = argparse.ArgumentParser(description="生图模型联通性/速度探测")
    ap.add_argument("models", nargs="*", help="只测这些模型 ID（默认测候选表）")
    ap.add_argument("--size", default="2K", help="尺寸（默认 2K）")
    ap.add_argument("--save", action="store_true", help="下载图片到 genimg_test/")
    ap.add_argument("--base-url", help="覆盖端点（默认读 presets genimg）")
    ap.add_argument("--token", help="覆盖 key（默认读 presets genimg）")
    args = ap.parse_args()

    cfg = {}
    try:
        with open(PRESETS, encoding="utf-8") as f:
            cfg = json.load(f).get("genimg") or {}
    except Exception:
        pass
    base = (args.base_url or cfg.get("base_url")
            or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    token = args.token or cfg.get("token", "")
    if not token:
        print("没有 token：填 GUI/presets.json 的 genimg.token 或用 --token")
        return 1

    refs = [data_uri(os.path.join(REF_DIR, r)) for r in FIXED_REFS
            if os.path.exists(os.path.join(REF_DIR, r))]
    if len(refs) < 2:
        print("固定层参考图缺失：", REF_DIR)
        return 1

    models = args.models or CANDIDATES
    print(f"端点 {base} · 参考图 {len(refs)} 张 · 尺寸 {args.size} · "
          f"提示词与达妮娅同款（成功即计费一张）\n")

    results = []
    for m in models:
        print(f"▶ {m} …", flush=True)
        r = probe(base, token, m, args.size, refs, args.save)
        results.append(r)
        if r["ok"]:
            line = f"  ✅ {r['dt']:.1f}s · 参考价 {price_hint(m)}"
            if r["usage"]:
                line += f" · usage={json.dumps(r['usage'], ensure_ascii=False)}"
            if r["saved"]:
                line += f"\n  存到 {r['saved']}"
            print(line, flush=True)
        else:
            print(f"  ❌ {r['dt']:.1f}s · {r['err']}", flush=True)

    print("\n═══ 汇总 ═══")
    for r in results:
        mark = "✅" if r["ok"] else "❌"
        tail = f"{r['dt']:.1f}s · {price_hint(r['model'])}" if r["ok"] else r["err"][:80]
        print(f"{mark} {r['model']:<32} {tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

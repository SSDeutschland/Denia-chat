# 共读预处理管线 · 转录后端注册表
# transcriber = (base_url, model, token) 三元组，注册表 = GUI/presets.json 的 vision_presets
# 两类适配：openai_vision（GLM/Qwen 视觉 chat/completions）+ glm_ocr（专用 layout_parsing）
import base64, json, pathlib, time, urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[3]  # Denia 根
PRESETS = ROOT / "GUI" / "presets.json"

VISION_PROMPT = (
    "这是一页数学书。请把它完整转录成 markdown：\n"
    "1. 所有公式转录为 LaTeX，行内公式用 $...$，独立成行的公式用 $$...$$\n"
    "2. 保留原文结构：章节标题、例题/解/思考题等块、正文段落，按阅读顺序排列\n"
    "3. 逐字转录，不要总结、不要省略、不要翻译，中文保持原文\n"
    "4. 页眉页脚也要，图片/照片里与书页无关的实物背景（键盘、桌面等）忽略\n"
    "5. 看不清的地方用 [?] 标出，不要编造"
)


def load_presets():
    """model -> preset 字典（token 不打印不落地到输出）"""
    cfg = json.load(open(PRESETS, encoding="utf-8"))
    return {p["model"]: p for p in cfg.get("vision_presets", [])}


def _post(url, token, body, timeout=300):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read())
    return resp, time.time() - t0


def transcribe_vision(preset, img_path, prompt=VISION_PROMPT, max_tokens=8000):
    """通用视觉大模型整页转录 -> (markdown, secs, usage)"""
    b64 = base64.b64encode(pathlib.Path(img_path).read_bytes()).decode()
    body = {
        "model": preset["model"],
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": max_tokens,
    }
    resp, dt = _post(preset["base_url"].rstrip("/") + "/chat/completions", preset["token"], body)
    return resp["choices"][0]["message"]["content"], dt, resp.get("usage", {})


def transcribe_glm_ocr(preset, img_path):
    """glm-ocr 结构化识别 -> (blocks, secs, usage)
    blocks: [{label: text/display_formula/image, content, bbox_2d}, ...] 按阅读顺序"""
    b64 = base64.b64encode(pathlib.Path(img_path).read_bytes()).decode()
    body = {"model": "glm-ocr", "file": f"data:image/jpeg;base64,{b64}"}
    resp, dt = _post(preset["base_url"].rstrip("/") + "/layout_parsing", preset["token"], body)
    blocks = [b for page in resp.get("layout_details", []) for b in page]
    return blocks, dt, resp.get("usage", {})


def glm_ocr_text(blocks):
    """从结构化块拼正文（忽略 image 块）"""
    return "\n\n".join(b.get("content", "") for b in blocks
                       if b.get("label") != "image" and b.get("content"))


def count_display_formulas(blocks):
    # 实测 label 为 "formula"（旧文档写 display_formula），兼容两种
    return sum(1 for b in blocks if "formula" in str(b.get("label", "")))

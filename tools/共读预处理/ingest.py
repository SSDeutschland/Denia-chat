# 共读预处理管线 · 主入口（路由表 v2 实现）
# 输入：PDF（排版/扫描均可）或 图片目录（手机拍照）  输出：denia/私有/共读/<书名>/ 全套
#
# 路由 v3：
#   PDF 有文本层 → L0 直出 + 公式闸门（formula_check.page_verdict 命中损坏签名 → escalate L4）；
#     公式密集页（Unicode 数学符号密度）→ 渲染走 L4
#   无文本层/拍照 → glm-ocr 快扫；display_formula 块 ≥ 阈值 → escalate L4 复核
#   入库后自动全书公式校验（--no-check 关），命中页走 graph-code 修复（见 README）
#
# 用法（在 Denia 根目录）：
#   PDF测试/venv/Scripts/python tools/共读预处理/ingest.py <输入> --book 抽象代数 [--limit 10]
import argparse, datetime, json, pathlib, re, sys, tempfile
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import transcribers as T
import formula_check as FC

ROOT = HERE.parents[2]                      # Denia 根
OUT_ROOT = ROOT / "denia" / "私有" / "共读"

MATH_SYMS = "∑∏∫∂∇√∈∉∀∃λμπσφψωΩ∞≤≥≠×÷±∩∪⊂⊃⊆⊇→←↔⇒⇔∝∼≈≡⊕⊗∧∨¬°′″⊥∥∠△⊙"
L0_MIN_CHARS = 80          # 文本层最短长度，低于视为扫描页
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}

# 每百万 token 价格（USD，2026-08-01 抓自 docs.z.ai / glm-ocr 国内 0.2 元/M ≈ $0.03）
PRICES_USD = {"glm-5v-turbo": (1.2, 4.0), "glm-ocr": (0.03, 0.03), "glm-4v-plus-0111": (0.6, 1.8)}
USD_CNY = 7.2


def math_density(text):
    if not text:
        return 0.0
    return sum(1 for c in text if c in MATH_SYMS) / len(text)


def paragraphs(text):
    """按空行切段，去掉纯页码碎片"""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return [p for p in parts if not re.fullmatch(r"[\d\s·.\-—]+", p)]


def anchor_page(text, counter, page_no):
    """一页正文 -> 带页标记 + [Pxxxx] 锚点的 markdown，返回 (md, 新counter, 段数)"""
    lines = [f"<!-- page {page_no} -->"]
    n = 0
    for p in paragraphs(text):
        lines.append(f"[P{counter:04d}] {p}")
        counter += 1
        n += 1
    return "\n\n".join(lines), counter, n


def esc(title, maxlen=24):
    t = re.sub(r'[\\/:*?"<>|\s]+', "", title)[:maxlen]
    return t or "未命名"


class Book:
    def __init__(self, name):
        self.dir = OUT_ROOT / name
        (self.dir / "正文").mkdir(parents=True, exist_ok=True)
        self.name = name

    def scaffold(self):
        tpl = {
            "笔记.md": f"# 《{self.name}》共读笔记\n\n> 双方追加式。用户随手记，达妮娅也会把想法记在这里。\n",
            "共读工作缓存.md": (
                f"# 《{self.name}》共读工作缓存（RAM）\n\n"
                "> 达妮娅的读书工作记忆：正文块用完即弃，理解留在这里。常驻 <1-2K tokens，增量更新不攒批。\n\n"
                "## 当前位置\n\n（未开始）\n\n## 本节脉络\n\n（空）\n\n## 速记\n\n（空）\n\n## 悬着的点\n\n（空）\n"),
            "共读记忆.md": (
                f"# 《{self.name}》共读记忆（SSD）\n\n"
                "> 会话末/章节末迷你归档蒸馏。RAM 的长期落点。\n\n（尚无归档）\n"),
        }
        for fn, body in tpl.items():
            p = self.dir / fn
            if not p.exists():
                p.write_text(body, encoding="utf-8")


def render_page(doc, pno, dpi, tmpdir):
    pix = doc[pno].get_pixmap(dpi=dpi)
    img = pathlib.Path(tmpdir) / f"p{pno+1}.jpg"
    pix.save(str(img), jpg_quality=88)
    return img


def l0_text(page):
    """L0 直抽：按排版块拼段（块间空行 -> 天然段落粒度）"""
    blocks = [b[4].strip() for b in page.get_text("blocks") if b[4].strip()]
    return "\n\n".join(blocks)


def ingest_pdf(pdf_path, book, args, presets):
    import fitz
    doc = fitz.open(str(pdf_path))
    toc = [t for t in doc.get_toc() if t[0] == 1]     # 一级目录
    pages = range(min(args.limit or len(doc), len(doc)))
    page_log, chapters = [], {}                        # chapter_idx -> [(page_no, md)]
    stats, usage_sum = {}, {}
    gate_hits = {}                                     # page_no -> 闸门分数（ escalate 依据）
    anchor_n = 1

    def bump(k): stats[k] = stats.get(k, 0) + 1
    def add_usage(model, u):
        s = usage_sum.setdefault(model, {"in": 0, "out": 0})
        s["in"] += u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
        s["out"] += u.get("completion_tokens", u.get("output_tokens", 0)) or 0

    with tempfile.TemporaryDirectory() as tmp:
        for pno in pages:
            text = doc[pno].get_text().strip()
            route, md_src, secs, usage = None, "", 0.0, {}
            if len(text) >= L0_MIN_CHARS and math_density(text) < args.l0_density:
                route, md_src = "L0", l0_text(doc[pno])
                # 公式闸门：L0 字体编码损坏签名（∑→P、拆行碎片等）→ escalate L4 重转录
                if args.gate:
                    v = FC.page_verdict(md_src, args.gate_threshold)
                    if v["escalate"]:
                        route = "L0(损坏)→L4"
                        img = render_page(doc, pno, args.dpi, tmp)
                        md_src, secs, usage = T.transcribe_vision(presets[args.l4], img)
                        add_usage(args.l4, usage); bump("L0(损坏)→L4")
                        gate_hits[pno + 1] = v["score"]
                if route == "L0":
                    bump("L0")
            elif len(text) >= L0_MIN_CHARS:
                route = "L0→L4"
                img = render_page(doc, pno, args.dpi, tmp)
                md_src, secs, usage = T.transcribe_vision(presets[args.l4], img)
                add_usage(args.l4, usage); bump("L0→L4")
            else:
                img = render_page(doc, pno, args.dpi, tmp)
                blocks, secs, usage = T.transcribe_glm_ocr(presets["glm-ocr"], img)
                add_usage("glm-ocr", usage)
                nf = T.count_display_formulas(blocks)
                if nf >= args.formula_blocks:
                    route = f"glm-ocr→L4(df={nf})"
                    md_src, s2, u2 = T.transcribe_vision(presets[args.l4], img)
                    secs += s2; add_usage(args.l4, u2); bump("glm-ocr→L4")
                else:
                    route = f"glm-ocr(df={nf})"
                    md_src = T.glm_ocr_text(blocks); bump("glm-ocr")
            md, anchor_n, np_ = anchor_page(md_src, anchor_n, pno + 1)
            chap = chapter_of(toc, pno)
            chapters.setdefault(chap, []).append((pno + 1, md))
            entry = {"page": pno + 1, "route": route, "secs": round(secs, 1),
                     "paras": np_, "usage": usage}
            if pno + 1 in gate_hits:
                entry["gate"] = gate_hits[pno + 1]
            page_log.append(entry)
            print(f"p{pno+1:>3} [{route:<16}] {secs:5.1f}s 段{np_}")
    return chapters, toc, page_log, stats, usage_sum, len(doc)


def ingest_images(img_dir, book, args, presets):
    imgs = sorted([p for p in pathlib.Path(img_dir).iterdir() if p.suffix.lower() in IMG_EXT])
    if args.limit:
        imgs = imgs[: args.limit]
    page_log, stats, usage_sum = [], {}, {}
    anchor_n, mds = 1, []

    def bump(k): stats[k] = stats.get(k, 0) + 1
    def add_usage(model, u):
        s = usage_sum.setdefault(model, {"in": 0, "out": 0})
        s["in"] += u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
        s["out"] += u.get("completion_tokens", u.get("output_tokens", 0)) or 0

    for i, img in enumerate(imgs):
        blocks, secs, usage = T.transcribe_glm_ocr(presets["glm-ocr"], img)
        add_usage("glm-ocr", usage)
        nf = T.count_display_formulas(blocks)
        if nf >= args.formula_blocks:
            route = f"glm-ocr→L4(df={nf})"
            md_src, s2, u2 = T.transcribe_vision(presets[args.l4], img)
            secs += s2; add_usage(args.l4, u2); bump("glm-ocr→L4")
        else:
            route = f"glm-ocr(df={nf})"
            md_src = T.glm_ocr_text(blocks); bump("glm-ocr")
        md, anchor_n, np_ = anchor_page(md_src, anchor_n, i + 1)
        mds.append((i + 1, md))
        page_log.append({"page": i + 1, "file": img.name, "route": route,
                         "secs": round(secs, 1), "paras": np_, "usage": usage})
        print(f"图{i+1:>3} {img.name[:20]:<22} [{route:<16}] {secs:5.1f}s 段{np_}")
    chapters = {1: mds}
    return chapters, [], page_log, stats, usage_sum, len(imgs)


def chapter_of(toc, pno):
    """页码 -> 第几章（一级目录序号，1 起）"""
    chap = 1
    for i, (lv, title, page1) in enumerate(toc):
        if page1 - 1 <= pno:
            chap = i + 1
    return chap


def write_book(book, chapters, toc, page_log, stats, usage_sum, total_pages, source):
    titles = {}
    for i, (lv, title, page1) in enumerate(toc):
        titles[i + 1] = title
    for ci, pages in sorted(chapters.items()):
        title = esc(titles.get(ci, "拍照批次" if not toc else f"第{ci}章"))
        body = [f"# {titles.get(ci, '拍照批次')}\n"]
        body += [md for _, md in pages]
        (book.dir / "正文" / f"{ci:02d}-{title}.md").write_text("\n\n".join(body), encoding="utf-8")
    toc_lines = [f"# 《{book.name}》目录\n"]
    for ci in sorted(chapters):
        label = titles.get(ci, f"第{ci}部分")
        pg = chapters[ci][0][0]
        toc_lines.append(f"- 第{ci:02d}章 {label}（自第 {pg} 页）→ `正文/{ci:02d}-{esc(label)}.md`")
    (book.dir / "目录.md").write_text("\n".join(toc_lines) + "\n", encoding="utf-8")

    cost = estimate_cost(usage_sum)
    progress = {
        "book": book.name, "source": str(source),
        "ingested": datetime.date.today().isoformat(),
        "pages_ingested": len(page_log), "pages_total": total_pages,
        "anchors_total": sum(p["paras"] for p in page_log),
        "current": {"chapter": None, "page": None, "anchor": None},
        "stats": stats, "usage_by_backend": usage_sum, "cost_est": cost,
        "pages": page_log,
    }
    (book.dir / "进度.json").write_text(
        json.dumps(progress, ensure_ascii=False, indent=1), encoding="utf-8")
    return cost


def estimate_cost(usage_sum):
    out, total_usd = {}, 0.0
    for model, u in usage_sum.items():
        price = PRICES_USD.get(model)
        if not price:
            out[model] = "未知价格"
            continue
        usd = u["in"] / 1e6 * price[0] + u["out"] / 1e6 * price[1]
        total_usd += usd
        out[model] = round(usd, 4)
    out["总计_USD"] = round(total_usd, 4)
    out["总计_CNY约"] = round(total_usd * USD_CNY, 2)
    return out


def main():
    ap = argparse.ArgumentParser(description="共读预处理管线（路由 v2）")
    ap.add_argument("input", help="PDF 路径 或 图片目录")
    ap.add_argument("--book", required=True, help="书名（输出 denia/私有/共读/<书名>/）")
    ap.add_argument("--limit", type=int, help="只处理前 N 页（试跑）")
    ap.add_argument("--l4", default="glm-5v-turbo", help="终审模型（presets.json 里的 model 名）")
    ap.add_argument("--formula-blocks", type=int, default=2, help="glm-ocr display_formula 块数 escalate 阈值")
    ap.add_argument("--l0-density", type=float, default=0.02, help="L0 页数学符号密度阈值（超过走 L4）")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--no-gate", dest="gate", action="store_false",
                    help="关掉 L0 公式损坏闸门（默认开）")
    ap.add_argument("--gate-threshold", type=int, default=3, help="闸门签名分阈值")
    ap.add_argument("--no-check", dest="check", action="store_false",
                    help="关掉入库后全书公式校验（默认开）")
    args = ap.parse_args()

    src = pathlib.Path(args.input)
    presets = T.load_presets()
    for need in {args.l4, "glm-ocr"}:
        if need not in presets:
            sys.exit(f"!! presets.json 缺少后端: {need}")
    book = Book(args.book)

    if src.is_dir():
        chapters, toc, page_log, stats, usage_sum, total = ingest_images(src, book, args, presets)
    else:
        chapters, toc, page_log, stats, usage_sum, total = ingest_pdf(src, book, args, presets)
    cost = write_book(book, chapters, toc, page_log, stats, usage_sum, total, src)
    book.scaffold()

    if args.check:
        rep = FC.scan_book(book.dir, use_katex=False)
        t = rep["totals"]
        print(f"\n== 公式校验 == 命中页 {t['pages_flagged']} / 锚点段 {t['anchors_flagged']}"
              + (f"（见 公式校验报告.md；可按 README 走 graph-code 修复）" if t["pages_flagged"] else ""))

    print("\n== 路由统计 ==", stats)
    print("== token 消耗 ==", json.dumps(usage_sum, ensure_ascii=False))
    print("== 成本估算 ==", json.dumps(cost, ensure_ascii=False))
    print(f"输出: {book.dir}")


if __name__ == "__main__":
    main()

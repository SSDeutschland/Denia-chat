# 共读公式校验 · 签名检测 + KaTeX 试渲染 + graph-code verify 闸门
#
# 背景：L0 文本层直抽在字体编码损坏的页上会产生系统性坏公式——
#   ∑→P、∏→Π、∫→Z，display 公式按物理行拆碎，无 $ 定界。
# 本模块三种用法：
#   1) ingest.py 的 L0 闸门：page_verdict(md_src) 命中 → escalate L4（纯 Python 零依赖）
#   2) 独立扫书出报告：formula_check.py book <书名> [--katex]
#   3) graph-code 修复的 verify_cmd 闸门：formula_check.py verify <原md> <修后md>
#
# KaTeX 试渲染需 tools/browser-crawler/venv（playwright）；无 playwright 时自动降级为签名-only。
import json, pathlib, re, sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]                      # Denia 根
KATEX_JS = ROOT / "GUI" / "vendor" / "katex" / "katex.min.js"

MATH_SYMS = "∑∏∫∂∇√∈∉∀∃λμπσφψωΩ∞≤≥≠×÷±∩∪⊂⊃⊆⊇→←↔⇒⇔∝∼≈≡⊕⊗∧∨¬°′″⊥∥∠△⊙"
HARD_SYMS = "∑∏∫ϵΛ⟨⟩∂∇√"                   # prose 里几乎不出现，单个即计分
SOFT_SYMS = "∈≥≤∞λ→"                        # 文科可能夹带，成对才计分
CJK_RE = re.compile(r"[一-鿿]")
ANCHOR_RE = re.compile(r"\[P(\d{4})\]\s*")
PAGEMARK_RE = re.compile(r"<!--\s*page\s+(\d+)\s*-->")


# ---------- 文本切分 ----------

def strip_markup(text):
    """剥锚点前缀和页标记行，供行级规则用"""
    lines = []
    for ln in text.split("\n"):
        if PAGEMARK_RE.search(ln):
            continue
        lines.append(ANCHOR_RE.sub("", ln, count=1))
    return "\n".join(lines)


def split_pages(md_text):
    """按 <!-- page N --> 切页 -> [(page_no|None, text)]，首个标记前为页 None（章标题等）"""
    parts = PAGEMARK_RE.split(md_text)
    out = []
    if parts[0].strip():
        out.append((None, parts[0]))
    for i in range(1, len(parts), 2):
        out.append((int(parts[i]), parts[i + 1]))
    return out


def split_segments(md_text):
    """按空行切段 -> [{anchor, text, page}]。段首 [Pxxxx] 剥出，页标记行间入当前页号"""
    cur_page = None
    segs = []
    for block in re.split(r"\n\s*\n", md_text):
        b = block.strip()
        if not b:
            continue
        m = PAGEMARK_RE.search(b)
        if m:
            cur_page = int(m.group(1))
            b = PAGEMARK_RE.sub("", b).strip()
            if not b:
                continue
        am = ANCHOR_RE.match(b)
        if am:
            segs.append({"anchor": f"P{am.group(1)}", "text": ANCHOR_RE.sub("", b, count=1),
                         "page": cur_page})
        else:
            segs.append({"anchor": None, "text": b, "page": cur_page})
    return segs


def _remove_code(text):
    """去 ``` 围栏块和行内 `...`（其内的 $ 不参与数学判定）"""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    return re.sub(r"`[^`]*`", " ", text)


def extract_math(text):
    """提取公式 -> [(tex, display:bool)]。$$ 优先于 $；\\$ 转义跳过"""
    t = _remove_code(text)
    out = []
    def _disp(m):
        out.append((m.group(1), True))
        return " "
    t = re.sub(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", _disp, t, flags=re.S)
    def _inl(m):
        out.append((m.group(1), False))
        return " "
    t = re.sub(r"(?<!\\)\$([^$\n]+?)(?<!\\)\$", _inl, t)
    return out


def strip_math(text):
    """挖掉数学 span（供裸符号扫描/相似度对比）"""
    t = _remove_code(text)
    t = re.sub(r"(?<!\\)\$\$.+?(?<!\\)\$\$", " ", t, flags=re.S)
    t = re.sub(r"(?<!\\)\$[^$\n]+?(?<!\\)\$", " ", t)
    return t


# ---------- 签名规则 ----------

FRAG_CHARS = re.compile(r"[A-Za-z0-9=<>≤≥≠/!().,±+\-*^_\s]+")

def _is_fragment(s):
    """孤立上下标碎片行：短、无中文、无 $、全公式残片字符，且含运算符或纯残片"""
    if not (1 <= len(s) <= 15) or CJK_RE.search(s) or "$" in s:
        return False
    if not FRAG_CHARS.fullmatch(s):
        return False
    return bool(re.search(r"[=<>≤≥!/]", s))


def scan_text(text):
    """签名扫描 -> {"score": int, "hits": [{rule, line, excerpt}]}。text 可带锚点/页标记"""
    hits = []
    body = strip_math(strip_markup(text))
    lines = body.split("\n")

    # S1 \sum 丢失：P∞/X∞ 粘连
    for i, ln in enumerate(lines, 1):
        if re.search(r"[PX]∞", ln):
            hits.append({"rule": "S1", "score": 3, "line": i,
                         "excerpt": ln.strip()[:60]})

    # S2/S6 碎片行与连排
    frag_idx = [i for i, ln in enumerate(lines) if _is_fragment(ln.strip())]
    for i in frag_idx:
        hits.append({"rule": "S2", "score": 2, "line": i + 1,
                     "excerpt": lines[i].strip()[:40]})
    run = 1
    for a, b in zip(frag_idx, frag_idx[1:]):
        run = run + 1 if b == a + 1 else 1
        if run == 2:
            hits.append({"rule": "S6", "score": 2, "line": b + 1,
                         "excerpt": "碎片连排"})

    # S3 裸 Unicode 数学符号（数学 span 已挖掉）
    n_hard = sum(1 for c in body if c in HARD_SYMS)
    n_soft = sum(1 for c in body if c in SOFT_SYMS)
    s3 = min(n_hard * 2 + n_soft // 2, 6)
    if s3:
        sample = next((ln.strip()[:60] for ln in lines
                       if any(c in ln for c in HARD_SYMS + SOFT_SYMS)), "")
        hits.append({"rule": "S3", "score": s3, "line": 0,
                     "excerpt": f"裸符号 硬{n_hard} 软{n_soft}: {sample}"})

    # S4 $ 不成对：成对 $$ 和 $ 都剥掉后仍有残余 $
    t = _remove_code(strip_markup(text))
    t = re.sub(r"(?<!\\)\$\$.+?(?<!\\)\$\$", " ", t, flags=re.S)
    t = re.sub(r"(?<!\\)\$[^$\n]+?(?<!\\)\$", " ", t)
    leftover = len(re.findall(r"(?<!\\)\$", t))
    if leftover:
        hits.append({"rule": "S4", "score": 3, "line": 0,
                     "excerpt": f"{leftover} 个残余 $"})

    # S5 \( \[ 定界符（渲染器只认 $/$$）
    n5 = len(re.findall(r"\\[([]", text))
    if n5:
        hits.append({"rule": "S5", "score": min(n5, 2), "line": 0,
                     "excerpt": f"{n5} 处 \\( 或 \\["})

    return {"score": sum(h["score"] for h in hits), "hits": hits}


def page_verdict(text, threshold=3):
    """ingest 闸门入口：一页 L0 抽出文本 -> {escalate, score, hits}"""
    r = scan_text(text)
    return {"escalate": r["score"] >= threshold, **r}


# ---------- KaTeX 试渲染（可选，需 playwright） ----------

def find_chrome():
    base = ROOT / "tools" / "browser-crawler" / "browsers"
    for d in sorted(base.glob("chromium-*")):
        exe = d / "chrome-win64" / "chrome.exe"
        if exe.exists():
            return exe
    return None


def katex_render_batch(formulas):
    """[(tex, display)] -> [err|None]。一次 evaluate 批量 renderToString 抓 ParseError"""
    from playwright.sync_api import sync_playwright
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError(f"未找到 chromium（{ROOT}/tools/browser-crawler/browsers/）")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=str(chrome), headless=True)
        page = browser.new_page()
        page.goto("about:blank")
        page.add_script_tag(path=str(KATEX_JS))
        errs = page.evaluate(
            """(list) => list.map(([tex, disp]) => {
                try { katex.renderToString(tex, {displayMode: disp, throwOnError: true}); return null }
                catch (e) { return String(e).slice(0, 200) }
            })""",
            [[t, d] for t, d in formulas])
        browser.close()
    return errs


# ---------- 扫书出报告 ----------

def scan_book(book_dir, use_katex=False, threshold=3):
    """扫一本书 -> 报告 dict，并写 公式校验报告.json/.md 到书目录"""
    book_dir = pathlib.Path(book_dir)
    pages_agg, anchors_agg, formulas_all = [], [], []
    for f in sorted((book_dir / "正文").glob("*.md")):
        md = f.read_text(encoding="utf-8")
        for pno, ptext in split_pages(md):
            if pno is None:
                continue
            v = scan_text(ptext)
            if v["score"] >= threshold:
                pages_agg.append({"page": pno, "file": f"正文/{f.name}",
                                  "score": v["score"], "hits": v["hits"]})
        for seg in split_segments(md):
            if not seg["anchor"]:
                continue
            v = scan_text(seg["text"])
            if v["score"] >= threshold:
                anchors_agg.append({"anchor": seg["anchor"], "page": seg["page"],
                                    "file": f"正文/{f.name}", "score": v["score"],
                                    "rules": sorted({h["rule"] for h in v["hits"]})})
            for tex, disp in extract_math(seg["text"]):
                formulas_all.append({"anchor": seg["anchor"], "tex": tex, "display": disp})

    katex_checked, katex_errs = 0, []
    if use_katex and formulas_all:
        try:
            errs = katex_render_batch([(f["tex"], f["display"]) for f in formulas_all])
            katex_checked = len(errs)
            for f, e in zip(formulas_all, errs):
                if e:
                    katex_errs.append({"anchor": f["anchor"], "tex": f["tex"][:80], "err": e})
        except Exception as ex:
            print(f"!! KaTeX 校验不可用，降级为签名-only：{ex}")

    report = {
        "book": book_dir.name, "generated": __import__("datetime").date.today().isoformat(),
        "threshold": threshold, "katex": use_katex,
        "totals": {"pages_flagged": len(pages_agg), "anchors_flagged": len(anchors_agg),
                   "formulas": len(formulas_all), "katex_checked": katex_checked,
                   "katex_errors": len(katex_errs)},
        "pages": sorted(pages_agg, key=lambda p: -p["score"]),
        "anchors": anchors_agg, "katex_errors": katex_errs,
    }
    (book_dir / "公式校验报告.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (book_dir / "公式校验报告.md").write_text(_report_md(report), encoding="utf-8")
    return report


def _report_md(rep):
    t = rep["totals"]
    lines = [f"# 《{rep['book']}》公式校验报告（{rep['generated']}）\n",
             f"- 命中页 **{t['pages_flagged']}**，命中锚点段 **{t['anchors_flagged']}**"
             f"（阈值 {rep['threshold']} 分）",
             f"- 公式 {t['formulas']} 条，KaTeX 试渲染 {t['katex_checked']} 条 / "
             f"错误 {t['katex_errors']} 条\n"]
    if rep["pages"]:
        lines.append("## 命中页（按分数降序）\n")
        lines.append("| 页 | 文件 | 分 | 规则 | 摘录 |")
        lines.append("|---|---|---|---|---|")
        for p in rep["pages"][:30]:
            rules = ",".join(sorted({h["rule"] for h in p["hits"]}))
            ex = p["hits"][0]["excerpt"] if p["hits"] else ""
            lines.append(f"| {p['page']} | {p['file']} | {p['score']} | {rules} | {ex} |")
    if rep["katex_errors"]:
        lines.append("\n## KaTeX 错误 Top10\n")
        for k in rep["katex_errors"][:10]:
            lines.append(f"- [{k['anchor']}] `{k['tex'][:60]}` — {k['err'][:80]}")
    return "\n".join(lines) + "\n"


# ---------- verify 闸门（graph-code verify_cmd） ----------

def _cjk_counter(text):
    body = strip_math(strip_markup(text))
    return {c: body.count(c) for c in set(CJK_RE.findall(body))}


def _similarity(a, b):
    """中文字符多重集相似度（交集/并集）。修复只许动公式，中文应几乎逐字不变"""
    ca, cb = _cjk_counter(a), _cjk_counter(b)
    keys = set(ca) | set(cb)
    if not keys:
        return 1.0
    inter = sum(min(ca.get(k, 0), cb.get(k, 0)) for k in keys)
    union = sum(max(ca.get(k, 0), cb.get(k, 0)) for k in keys)
    return inter / union


def verify(orig_path, fixed_path, sim=0.95, use_katex=False):
    """修复验收：全过 print PASS 退 0，否则 print FAIL 原因退 1。供 graph-code verify_cmd"""
    orig = pathlib.Path(orig_path).read_text(encoding="utf-8")
    fixed_p = pathlib.Path(fixed_path)
    if not fixed_p.exists():
        sys.exit(f"FAIL: 修后文件不存在 {fixed_path}")
    fixed = fixed_p.read_text(encoding="utf-8")

    # 1. 锚点集合（含次数）一致
    a_o = sorted(re.findall(r"\[P\d{4}\]", orig))
    a_f = sorted(re.findall(r"\[P\d{4}\]", fixed))
    if a_o != a_f:
        miss = sorted(set(a_o) - set(a_f))[:5]
        extra = sorted(set(a_f) - set(a_o))[:5]
        sys.exit(f"FAIL: 锚点不一致 缺{miss} 多{extra} ({len(a_o)} vs {len(a_f)})")

    # 2. 页标记一致
    p_o = sorted(re.findall(r"<!--\s*page\s+\d+\s*-->", orig))
    p_f = sorted(re.findall(r"<!--\s*page\s+\d+\s*-->", fixed))
    if p_o != p_f:
        sys.exit(f"FAIL: 页标记不一致 ({len(p_o)} vs {len(p_f)})")

    # 3. 修后签名重扫须 0 分
    v = scan_text(fixed)
    if v["score"] > 0:
        h = v["hits"][0]
        sys.exit(f"FAIL: 修后仍命中签名 {v['score']} 分，如 {h['rule']}: {h['excerpt'][:60]}")

    # 4. 中文相似度（防编造/防改写正文）
    s = _similarity(orig, fixed)
    if s < sim:
        sys.exit(f"FAIL: 中文相似度 {s:.3f} < {sim}（疑似改写正文）")

    # 5. 可选 KaTeX 复渲染
    if use_katex:
        formulas = extract_math(fixed)
        if formulas:
            errs = katex_render_batch(formulas)
            bad = [(t, e) for (t, _), e in zip(formulas, errs) if e]
            if bad:
                sys.exit(f"FAIL: KaTeX 错误 {len(bad)} 条，如 `{bad[0][0][:60]}` — {bad[0][1][:80]}")

    print(f"PASS（锚点 {len(a_o)}、页标记 {len(p_o)}、中文相似度 {s:.3f}）")


# ---------- CLI ----------

def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd = args[0]
    if cmd == "book":
        name = args[1]
        use_katex = "--katex" in args
        thr = _opt(args, "--threshold", 3, int)
        bd = pathlib.Path(name)
        if not bd.is_dir():
            bd = ROOT / "denia" / "私有" / "共读" / name
        rep = scan_book(bd, use_katex=use_katex, threshold=thr)
        t = rep["totals"]
        print(f"《{rep['book']}》命中页 {t['pages_flagged']} / 锚点段 {t['anchors_flagged']}"
              f" / 公式 {t['formulas']}（KaTeX {t['katex_checked']} 查 {t['katex_errors']} 错）"
              f"\n报告: {bd}/公式校验报告.md")
    elif cmd == "page":
        md = pathlib.Path(args[1]).read_text(encoding="utf-8")
        thr = _opt(args, "--threshold", 3, int)
        v = page_verdict(md, thr)
        print(json.dumps(v, ensure_ascii=False, indent=1))
    elif cmd == "verify":
        verify(args[1], args[2], sim=_opt(args, "--sim", 0.95, float),
               use_katex="--katex" in args)
    else:
        sys.exit(__doc__)


def _opt(args, name, default, cast):
    return cast(args[args.index(name) + 1]) if name in args else default


if __name__ == "__main__":
    main()

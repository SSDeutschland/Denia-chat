# 共读公式修复发配器 · 校验报告 -> graph-code 运行目录
#
# 读 denia/私有/共读/<书名>/公式校验报告.json，生成 E:\Deepcode\runs\repair-<书名>-<日期>/：
#   orig/<章文件>   修复前备份（verify 对照基准 + 兜底）
#   graph.json      一章一节点（同文件纪律下最简划分），verify_cmd 走 formula_check.py verify
#
# 用法：
#   PDF测试/venv/Scripts/python tools/共读预处理/make_repair_graph.py <书名> [--katex] [--max-workers 4]
# 然后（给用户看图确认后）：
#   python E:/Deepcode/scripts/dispatcher.py E:/Deepcode/runs/repair-<书名>-<日期>/
import json, pathlib, shutil, sys, datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]                       # Denia 根
DEEPCODE_RUNS = pathlib.Path(r"E:\Deepcode\runs")

SYMPTOMS = """书稿由 PDF 文本层抽取产生，字体编码损坏导致公式系统性坏掉，典型症状：
- ∑ 显示为 P（如 "P∞" 实为 \\sum_{...}^{\\infty}），∏ 显示为 Π，∫ 显示为 Z
- 公式的上下标/分子分母被拆成独立短行（k=0、k!、Aktk 各成一锚点段）
- 公式无 $ 定界，裸 Unicode 数学符号（∈≥≤∞λϵΛ）混在正文里"""

RULES = """【硬规则】（违反会被自动打回重写）：
1. 锚点 `[Pxxxx]` 原样保留：每个锚点段维持原位，不增不删不改编号
2. 页标记 `<!-- page N -->` 原样保留
3. 只修公式和明显的碎片拆行：未损坏的中文/正文逐字保留，不重写、不润色、不总结
4. 被拆碎的上下标段，内容合并进所属公式段；被掏空的锚点段留锚点后内容清空
5. 行内公式用 $...$，独立成行的公式用 $$...$$，$$ 必须在同一段内闭合
6. 根据上下文推断原公式；推不出来的部分用 [?] 标出，绝不编造
7. 直接编辑目标文件本身（原地修复），不要新建任何文件"""


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    name = args[0]
    use_katex = "--katex" in args
    max_workers = _opt(args, "--max-workers", 4)
    book_dir = ROOT / "denia" / "私有" / "共读" / name
    report_p = book_dir / "公式校验报告.json"
    if not report_p.exists():
        sys.exit(f"!! 先跑校验：formula_check.py book {name}")
    report = json.loads(report_p.read_text(encoding="utf-8"))
    if not report["anchors"]:
        sys.exit(f"《{name}》无命中锚点，无需修复")

    # 命中锚点按章文件分组，一章一节点
    by_file = {}
    for a in report["anchors"]:
        by_file.setdefault(a["file"], []).append(a)

    run_dir = DEEPCODE_RUNS / f"repair-{name}-{datetime.date.today():%Y%m%d}"
    if run_dir.exists():
        sys.exit(f"!! 运行目录已存在：{run_dir}（避免覆盖 orig 备份，请换日期后缀或清理）")
    (run_dir / "orig").mkdir(parents=True)

    nodes = []
    for i, (rel, anchors) in enumerate(sorted(by_file.items()), 1):
        chapter = book_dir / rel
        bak = run_dir / "orig" / pathlib.Path(rel).name
        shutil.copy2(chapter, bak)
        verify = (f'python "{HERE / "formula_check.py"}" verify '
                  f'"{bak}" "{chapter}"' + (" --katex" if use_katex else ""))
        alist = "、".join(a["anchor"] for a in anchors)
        brief = f"""你是数学书稿修复 worker。一本中文数学书的 markdown 书稿需要修复损坏的公式。

{SYMPTOMS}

【目标文件】{chapter}（原地编辑）
【已定位的可疑锚点段】{alist}
（以校验扫描为准，附近未列出的段若也有同样症状一并修；其余内容一律不动）

{RULES}

完成后写 handoff：修了哪些锚点、哪里用了 [?] 及原因。"""
        nodes.append({"id": f"{i:03d}", "role": "repair",
                      "title": f"修复 {pathlib.Path(rel).name}（{len(anchors)} 锚点）",
                      "brief": brief, "files": [str(chapter)],
                      "depends_on": [], "verify_cmd": verify})

    graph = {"name": f"repair-{name}", "max_workers": max_workers,
             "max_retries": 2, "nodes": nodes}
    (run_dir / "graph.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"运行目录: {run_dir}")
    print(f"节点 {len(nodes)} 个（一章一节点），备份 orig/ {len(by_file)} 章"
          f"{'（verify 含 KaTeX）' if use_katex else ''}")
    for n in nodes:
        print(f"  {n['id']} {n['title']}")
    print(f"\n确认后执行: python E:/Deepcode/scripts/dispatcher.py \"{run_dir}\"")


def _opt(args, name, default):
    return int(args[args.index(name) + 1]) if name in args else default


if __name__ == "__main__":
    main()

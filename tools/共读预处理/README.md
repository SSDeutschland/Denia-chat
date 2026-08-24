# 共读预处理管线（PDF → 共读书 权威工作流）

把书（排版 PDF / 扫描 PDF / 手机拍照）转成达妮娅共读书的目录结构。本文件是共读模块
**产生阅读文件的唯一权威工作流文档**（`denia-read` skill 引用此处）。

流水线总览：

```
PDF/图片 ──ingest.py(路由 v3)──> denia/私有/共读/<书名>/ ──formula_check 自动校验──> 公式校验报告
                                        ▲                                │
                                        └──── graph-code 修复(deepcode worker) ◄── 命中时
```

## 1. 入库：ingest.py（路由 v3）

```bash
# 在 Denia 根目录；需要 PyMuPDF（用 PDF测试/venv 的 python）
PDF测试/venv/Scripts/python tools/共读预处理/ingest.py <PDF路径|图片目录> --book 抽象代数

# 常用参数
--limit 10              # 只跑前 10 页（试跑）
--l4 glm-5v-turbo       # 终审模型（presets.json 里任意视觉后端，如 qwen3-vl-plus 省钱）
--formula-blocks 2      # glm-ocr 公式块 ≥ N 的页 escalate L4
--l0-density 0.02       # 文本层数学符号密度阈值，超过走 L4
--dpi 200
--no-gate               # 关掉 L0 公式损坏闸门（默认开）
--gate-threshold 3      # 闸门签名分阈值
--no-check              # 关掉入库后全书公式校验（默认开）
```

### 路由 v3

```
输入页
  ├─ PDF 有文本层 → L0 直出（免费瞬时）
  │    ├─ 数学符号密度 ≥ 阈值 → 渲染走 L4
  │    └─ ★公式闸门（2026-08-04 新增）：L0 文本命中损坏签名 → escalate L4 重转录
  ├─ 无文本层 / 拍照 → glm-ocr 快扫（~2s/页，≈免费）
  │    ├─ formula 块 < 阈值 → 用 glm-ocr 产出
  │    └─ formula 块 ≥ 阈值 → escalate L4（默认 glm-5v-turbo）整页重转录
  └─ 纯文本书 → 全程 L0/glm-ocr，不会动 L4
```

后端注册表 = `GUI/presets.json` 的 `vision_presets`（transcriber = base_url+model+token 三元组，
换后端零代码）。两类适配：OpenAI 兼容视觉 chat（GLM/Qwen 通用）+ glm-ocr 专用 layout_parsing。

### 输出

```
denia/私有/共读/<书名>/
  正文/NN-章标题.md    # [Pxxxx] 段落锚点 + <!-- page N --> 页标记，锚点全书连续编号
  目录.md              # 章 -> 文件 -> 起始页
  进度.json            # 路由/耗时/token/成本逐页记录 + current 阅读位置（共读会话维护）
  公式校验报告.json/.md # 入库自动校验产物（见 §2）
  笔记.md              # 双方追加式
  共读工作缓存.md       # RAM 模板（达妮娅的读书工作记忆）
  共读记忆.md          # SSD 模板（迷你归档落点）
```

## 2. 公式校验：formula_check.py

### 为什么需要它

L0 直抽在某些排版 PDF 上遭遇**字体编码损坏**：∑ 显示为 P、∏→Π、∫→Z，display 公式
按物理行拆碎、上下标各成一锚点段、无 `$` 定界。中文正文稀释了数学符号密度，
这些页低于 `--l0-density` 阈值漏网（2026-08-04 用户实测发现，判例：优化方法 P0491
`A = PJP −1。则x(t) = eAtx(0) = P∞`）。L4 视觉转录无此病。

### 签名规则（页分数 ≥ 3 即命中）

| # | 规则 | 分 |
|---|---|---|
| S1 | `\sum` 丢失：`P∞`/`X∞` 粘连 | 3 |
| S2 | 孤立上下标碎片行（`k=0`/`k!`/`!1/p`，≤15 字符无中文无 `$`） | 2/行 |
| S3 | 裸 Unicode 数学符号在 `$...$` 外（硬符号∑∏∫ϵΛ单个计，软符号∈≥≤成对计） | 累计封顶6 |
| S4 | `$` 不成对 | 3 |
| S5 | `\(` `\[` 定界符（GUI 渲染器只认 `$`/`$$`） | 1-2 |
| S6 | 碎片连排 ≥2 行 | +2 |

### 三种用法

```bash
# (a) 扫书出报告（纯签名，PDF测试/venv 即可）
PDF测试/venv/Scripts/python tools/共读预处理/formula_check.py book <书名>

# (b) 加 KaTeX 试渲染（对每条 $...$/$$...$$ renderToString 抓 ParseError，
#     需 playwright：tools/browser-crawler/venv）
tools/browser-crawler/venv/Scripts/python tools/共读预处理/formula_check.py book <书名> --katex

# (c) verify 闸门：graph-code 修复的 verify_cmd（见 §3）
formula_check.py verify <原md> <修后md> [--katex] [--sim 0.95]
```

ingest 内部还有两处自动调用：L0 闸门（`page_verdict`）+ 入库后全书 `scan_book`。

verify 五道闸：①锚点多重集一致 ②页标记一致 ③修后签名重扫 0 分
④中文相似度 ≥0.95（剥数学 span 后对比，防模型改写正文/编造）⑤可选 KaTeX 复渲染。

## 3. 修复：graph-code 发配（强模型指挥弱模型）

命中页的修复**不重跑视觉模型**，由语言模型按上下文推断修复（文本层损坏对 LLM 难度不大）。
复用 `E:\Deepcode` 的 graph-code 基建：强模型（Claude）设计 DAG + 终审，
弱模型（deepcode CLI，deepseek-v4-flash 足够）并发原地修文件，
`verify_cmd` 零 token 闸门自动打回重写。

```bash
# 1. 出校验报告（§2a）
# 2. 生成运行目录（备份 orig/ + graph.json，一章一节点）
PDF测试/venv/Scripts/python tools/共读预处理/make_repair_graph.py <书名> [--katex]
# 3. 给用户看图确认后执行（★必须用 E:\Python，pywinpty 只装在那）
E:\Python\python.exe E:\Deepcode\scripts\dispatcher.py E:\Deepcode\runs\repair-<书名>-<日期>\
# 4. escalated 节点 → 强模型读 logs/ 人工收尾（graph-code 标准流程）
```

- worker 纪律写死在节点 brief：锚点/页标记原样保留、正文逐字不动、碎片合并进所属公式段、
  掏空段留锚点清空、推不出用 `[?]` 不编造
- 修前原章备份在 `runs/<run>/orig/`（verify 对照基准 + 兜底回滚）
- 弱模型档位：`~/.deepcode/settings.json` 的 MODEL（pro/flash 切换；2026-08-04 冒烟 flash 一遍过）
- **实测**（repair-修复冒烟-20260804）：P∞ 链 10 段合并还原
  `\sum_{k=0}^{\infty}\frac{A^k t^k}{k!}`，worker 还区分了三处 P 的语义（∑ vs 矩阵 P）

## 4. 实测成本

- 入库（2026-08-01，45 页数学排版 PDF，55% 页 dense→L4）：$0.38 ≈ ¥2.8；
  外推 400 页同类书 ≈¥25；扫描/拍照书 ≈¥15-20
- 闸门误伤代价 = 一页 L4（≈¥0.06），阈值宁松勿紧
- 校验全免费（本地）；KaTeX 试渲染全书 <1 分钟
- 修复按章发节点，deepseek-v4-flash 级成本（冒烟单节点 488s 一遍过）

## 5. 注意

- glm-ocr 的 label 实测是 `formula` 不是 `display_formula`（代码已兼容）
- 5v-turbo 是推理模型，会用数学知识"补正"原书（(-1)^l 判例）——精度红利，但锚点引用以原书为准
- presets.json 含 API key，gitignored；日志只打 token 数不打 key
- venv 分工：ingest/签名校验用 `PDF测试/venv`；KaTeX/任何要 playwright 的用 `tools/browser-crawler/venv`；
  graph-code dispatcher 用 `E:\Python`（pywinpty）
- 摄取分层（L0-L4）实测依据见 `PDF测试/exp/报告.md`

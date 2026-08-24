# 配置教程：API 钥匙（主模型 / 识图 / 生图 / 上网）

> 本文写给"完全没配过 API 的人"，也写给帮你配置的 AI——照做即可。
> 所有配置最终都落在 `GUI/presets.json`；但**你永远不需要手编这个文件**，
> GUI 右上角 ⚙ 设置面板里有表单。本文只是告诉你每个格子填什么。

## 0. 必备：主对话模型（没有它她不会说话）

她的大脑是外接大模型，按量付费。推荐二选一：

### 方案 A：DeepSeek（推荐，便宜）

1. 打开 https://platform.deepseek.com ，注册 → 充值（5 元够聊很久）
2. 「API keys」→ 创建，复制 `sk-xxxxxxxx` 形式的 key
3. 打开 GUI（启动达妮娅GUI.bat）→ 右上角 ⚙ → 模型预设 → 添加：
   - 名称：`DeepSeek`
   - base_url：`https://api.deepseek.com/anthropic`
   - token：粘贴你的 key
   - model：`deepseek-v4-flash`（日常快聊）或 `deepseek-v4-pro`（更聪明更贵）
   - vision：关
4. 选中它，回到聊天页，发一句"你好"——她回了就成了。

### 方案 B：GLM 智谱（支持识图，她能直接看你发的图）

1. https://open.bigmodel.cn 注册 → API keys → 复制 key
2. ⚙ 添加预设：
   - base_url：`https://open.bigmodel.cn/api/anthropic`
   - model：`glm-5.2`
   - vision：**开**
3. 同上分页签还能领每日免费额度。

> 别家（Kimi/Claude/GPT 中转站等）同理：只要是「Anthropic 兼容端点」都能填。
> 想同时挂好几家随时切换 → 进阶看 `docs/配置-LiteLLM桥.md`。

## 1. 识图转接（主模型是盲模型时的"代看"）

如果你主模型用 DeepSeek（看不见图），可以给她配一个"眼睛"：
你发图时，先由识图模型看一眼、用文字描述给她。

⚙ 设置 → 👁 识图转接：
- enabled：开
- 识图预设：base_url `https://open.bigmodel.cn/api/paas/v4`，model `glm-5v-turbo`，token 填 GLM key

她还可以用 `ask_vision` 工具主动向"眼睛"追问细节（每批图最多 3 次）。

## 2. 生图（她"拍照"发给你）

她在对话里写 `[生图:画面]` 标记，后端调用火山方舟 Seedream 生成，几毛钱一张量级。

1. https://www.volcengine.com/product/ark 注册 → 开通方舟 → API Key 管理 → 创建
2. ⚙ 设置 → 🎨 生图：
   - enabled：开
   - base_url：`https://ark.cn-beijing.volces.com/api/v3`
   - token：你的方舟 key
   - model：`doubao-seedream-5.0-lite`（或你开通的 Seedream 型号）
   - 每日限额：默认 20，防烧钱
3. 参考图已自带（`denia/共享/设定/参考图/固定层/`），保证画出来像她本人。

## 3. 上网 / 想法池（她闲时自己刷热榜）

她的想法池靠内置浏览器爬虫。首次启用跑一次安装（约下载 150MB Chromium）：

双击仓库根目录的 `安装浏览器爬虫.bat`（需已装 Python），或直接运行 `tools/browser-crawler/setup.py`。

装完即自动生效。不想要她上网：GUI ⚙ 里勾选「跳过想法池爬取」。

## 4. 验证清单

- [ ] 主模型：发消息她有回复
- [ ] 识图：发一张图，她能说出图里内容
- [ ] 生图：对她说"拍张照给我看看"，几分钟后收到图
- [ ] 上网：问她"最近有什么新闻"，她能答出时效内容

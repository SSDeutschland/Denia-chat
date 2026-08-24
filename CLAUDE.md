# Denia 项目说明（给 AI / 开发者看）

融合《鸣潮》角色达妮娅与 AI 助手的 Claude Code Skill。公开版 v0.5.0。

## 快速启动

```bash
# 终端模式：本目录启动 Claude Code，输入 /denia
claude

# GUI 网页模式：双击 启动达妮娅GUI.bat（首跑自动建 GUI/venv 装依赖）
# 浏览器打开 http://127.0.0.1:8765，⚙ 设置里配模型预设

# 首次 clone 后必须先跑一次：首次运行-初始化.bat（替换 __DENIA_ROOT__ 路径占位符）
```

## 架构

- 三层对话架构：L0 编排 / L1 思维（内心独白）/ L2 对话，分级加载 + Agent 分线程委托
- 人格模型：8 维度性格画像，人格驱动思维与表达，事实信息按需检索
- 记忆系统：虚质空间分界线分区——`denia/共享/记忆-虚质之前/`（游戏剧情，固定）+ `denia/私有/`（用户相关，增长）
- 情绪系统：触发→响应动态规则 + 三种强度曲线（突发/累积/底色偏移），寄生在 `denia/私有/状态/情绪状态.md`
- 公开分身（QQ）：`denia/缓冲/` 单向阀隔离，分身物理不挂载主私有记忆

## 关键文件

- `.claude/skills/denia/SKILL.md` — Skill 主入口
- `.claude/skills/denia-read/SKILL.md` — 共读模式
- `.claude/skills/denia-qq/SKILL.md` — QQ 分身人格侧
- `denia/共享/设定/核心人设.md` — 人格模型
- `denia/私有/记忆/近期主线.md` — 启动必读（15 条滚动 + →#NNN 指针）
- `denia/私有/记忆/记忆总索引.md` — 两跳检索菜单
- `GUI/server_sdk.py` — GUI 后端（claude-agent-sdk 封装，全托管 LiteLLM/TTS/QQ 桥进程）
- `tools/` — browser-crawler（想法池/上网）、生图、共读预处理、qq-bridge
- `本地转接/LiteLLM/` — 可选本地模型桥

## 配置

- 所有功能开关与钥匙集中在 `GUI/presets.json`（gitignore；模板 `GUI/presets.example.json`）
- 配置教程在 `docs/`：API 钥匙 / TTS 语音 / QQ 分身 / LiteLLM 桥
- `__DENIA_ROOT__` 占位符：仓库与便携包统一协议，`tools_pack/init.py` 首启替换为实际路径

## 硬规则（给维护者）

- `denia/私有/` 增长内容、`GUI/presets.json`、`.env.local`、`本地转接/LiteLLM/config.yaml` 永不入库（.gitignore 已钉）
- `*.bat` 必须 GBK 编码 + CRLF 行尾（.gitattributes 已钉 eol=crlf）
- 公开发布流程：实验室仓库 `封装工程/build_release.py` 装配 → grep 闸门 → 本仓库提交

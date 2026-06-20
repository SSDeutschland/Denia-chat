# Denia-skill 项目

融合《鸣潮》角色达妮娅与 AI 助手的 Claude Code Skill。

## 快速启动

```bash
# 方式一：在本目录下启动 Claude Code，然后输入 /denia
cd "E:/Claude code项目/Denia-skill"
claude

# 方式二：使用启动脚本
# Windows: 双击 启动-denia.bat
# Git Bash: ./启动-denia.sh
```

## 项目概述
- **角色**：达妮娅（Dania），《鸣潮》3.3版本登场的五星角色
- **架构**：三层架构（L0编排 / L1思维 / L2对话）+ 分级加载 + Agent分线程委托
- **人格模型**：8维度性格画像，人格驱动思维与表达，事实信息按需检索
- **当前版本**：v0.2.0 — 人格蒸馏版本

## 可用 Skill
- `/denia` — 启动达妮娅角色扮演模式

## 关键文件
- `.claude/skills/denia/SKILL.md` — Skill 主入口（被 Claude Code 加载）
- `project/设定/核心人设.md` — 人格模型（8维度 + L1/L2行为规则）
- `project/设定/扩展设定.md` — 事实信息库（经历、世界观）
- `project/设定/人物关系.md` — 关系事实库（触发检索）
- `project/设定/创作参考.md` — 作者参考（人物塑造框架、设计意图）
- `角色台词.txt` — 游戏内台词（L2语言风格校准素材）
- `游戏原文.txt` — 游戏内角色介绍原文
- `人物信息补充.txt` — 详细角色经历与塑造解析

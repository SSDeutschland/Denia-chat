# Git 速查便签

> 用于 Denia-skill 项目的版本管理

---

## 日常三连（每次改完代码后执行）

```powershell
git add .                              # 把改动加入待存档
git commit -m "一句话描述你改了什么"      # 创建存档点
git status                             # 确认状态
```

---

## 试错分支（最常用）

```powershell
git checkout -b 分支名          # 创建并切换到新分支（平行世界）
# ... 随便改文件 ...
git add . && git commit -m "..."  # 在分支上存档
# 满意 → 合并回主线
git checkout master              # 回到主线
git merge 分支名                 # 合并
git branch -d 分支名             # 删分支
# 不满意 → 直接扔
git checkout master              # 回到主线
git branch -D 分支名             # 强制删分支
```

---

## 查看历史和差异

```powershell
git log --oneline                # 紧凑版历史（每个commit一行）
git log                          # 完整版历史（按 q 退出）
git status                       # 当前有哪些文件改动了
git diff                         # 看具体改了什么内容
git diff 文件名                   # 只看某个文件
```

---

## 回退单个文件

```powershell
git checkout 存档编号 -- 文件路径
# 例：git checkout 7516ca8 -- project/设定/核心人设.md
```

---

## 记不住就查

```powershell
git --help         # 总帮助
git 命令 --help     # 具体某个命令的帮助（如 git commit --help，按 q 退出）
```

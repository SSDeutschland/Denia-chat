# LiteLLM 本地桥（端口 4000）

novadiff GPT 池 → Anthropic `/v1/messages` 协议的本地转接，与 CCR（3456）并列。
相对 CCR 的增量：**会话内 set_model 切模型可用**、自带逐调用成本、多后端归一 hub、
**图片块翻译可用**（GPT 视觉直通，可免 GLM 识图中继）。

## 复现安装（依赖全在项目目录内）

```bash
python -m venv venv
venv/Scripts/python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    "litellm[proxy]==1.95.0" "fastapi==0.136.3"
```

两个坑（2026-08-06 实测）：

1. **fastapi 必须钉 0.136.3**：litellm 1.95.0 声明 `fastapi>=0.136.3,<1.0`，但
   最新的 0.141.x 已删掉内部函数 `get_flat_dependant`，proxy 启动即
   `ImportError`（真实错误被 litellm 吞成 `No module named 'proxy_server'`）。
2. **config.yaml 不能含中文**：litellm 读配置不带 encoding，Windows 按 GBK 解码
   直接 `UnicodeDecodeError`。配置保持纯 ASCII（本目录路径含中文不影响）。

另：PyPI 直连极慢，用清华镜像；避开 litellm 投毒版本 1.82.7/1.82.8。

3. **Claude 档名泄漏（2026-08-06 实测）**：CC 子 agent（Browser Operator 等）和部分
   内部调用会绕过 env 映射，直接发字面 `claude-sonnet-5` / `model=None`，LiteLLM
   400 `ProxyModelNotFoundError`。已在 config.yaml 加档名兜底 alias：
   `claude-sonnet-5`/`claude-opus-5` → gpt-5.5，`claude-haiku-4-5` → gpt-5.4-mini
   （后台任务自动走便宜档，相当于白捡分层路由）。`model=None` 的偶发 400 无法靠
   alias 兜，复发再查。

## 启动

```bash
启动-LiteLLM.bat          # 或: venv/Scripts/litellm.exe --config config.yaml --port 4000 --host 127.0.0.1
```

GUI 预设「LiteLLM本地桥」= `http://127.0.0.1:4000` + token `sk-denia-local`。
config.yaml 含 novadiff key，已被 `.gitignore` 隔离，勿入库。

## 验证脚本（GUI venv 的 python 跑）

- `../test_sdk_litellm.py` — SDK 全链路 + 会话内 set_model 切模型
- `../test_gui_litellm_preset.py` — GUI server_sdk 真实路径（_preset_sdk_env）

## 已知小差异

- tool_use 流式块结构标准，但 stop_reason 报 `end_turn` 而非 `tool_use`（CC SDK 实测不介意）。
- usage 里 cache 字段为 0（novadiff 侧自动缓存，litellm 不上报）。
- cost 按官方价表算，novadiff 实际是中转价（GPT 池 ~0.05x），看绝对值要乘系数。

## tool_result 图片丢失与 custom_callbacks.py 修复（2026-08-06 深夜）

**病因**：CC 的 Read 工具读图后以 tool_result 块返回图片，LiteLLM 的
anthropic→openai adapter 把单图 tool_result 翻成 tool 消息里的一坨
**base64 data-URL 纯文本字符串**——模型看不到图，还白烧 token。Denia 实测
"读取成功但相纸没显影"，最小复现证实；用户消息里的图（直接发图）一直正常。

**修复**：`custom_callbacks.py` 的 `async_pre_call_hook` 在翻译前（Anthropic 层）
把 tool_result 里的图块搬到紧随其后的 user 消息（图在 user 位是验证过可用的），
原位留文字占位。config.yaml `litellm_settings.callbacks` 挂载，cwd 须为本目录。

**验证**：最小复现从"没看到图像数据"变"黑白相间的猫坐在户外土地上"；
端到端 SDK+Read 读 opaque 文件名照片，描述出虎斑猫/绿眼睛/车底/背景的黑白猫
（真实视觉实锤）。注意 GPT 调 Read 仍爱带 limit/offset/pages 参数（pages:"" 会报错，
它通常下一发改 pages:"1" 成功）——提示词可缓解但根治不了，属模型习惯。

## 纯文本模型图片/工具结构毒化与二次修复（2026-08-09）

**病因**（达妮娅生图后 Read 回看照片实锤）：图片块进入会话历史后，桥上纯文本模型
**之后每一轮**都被上游 400——glm-5.2 报 `1210 messages.content.type` 不收图片；
deepseek 报 `No tool output found for tool call`（续接会话从 transcript 重建历史时，
并行 tool_use 是分开的 assistant 条目，deepseek 要求 tool_calls 后必须紧邻 tool 输出）。
历史由 runtime 全量重放，坏一块=整局毒死，cost=0 秒回。

**修复**（同在 custom_callbacks.py 的 pre_call_hook）：别名在 TEXT_ONLY_MODELS
（deepseek-v4-flash/pro、glm-5.2）时不做 hoist，改为 ①全部图片块→占位文字
（含 tool_result 内层）②同角色连续消息合并（并行 tool_use 归并到一条 assistant，
Anthropic 协议天然合法）。新增纯文本别名记得往集合里加。

**验证**：`test_sanitize.py` 单元三项 + 活链三项（中毒结构打 deepseek/glm 均 200
且答对工具结果内容；gpt-5.5 视觉回归仍认出 1x1 红图）。中毒的旧存档修复后可直接
续接使用（每次请求现剥，历史本身不用洗）。

## deepseek-v4-flash 调工具 400：切原生 anthropic 端点根治（2026-08-16）

**病因**（Browser Operator 子 agent 三次浏览中途摔，`No tool output found for tool
call call_00_...`）：config.yaml 里 `deepseek-v4-flash` 配成
`openai/deepseek-v4-flash` + `/v1`，litellm 把它路由到 `/v1/responses` 的
anthropic→Responses 适配器。assistant 消息**同时含 text 和 tool_use** 时，适配器把
text 拆成独立 message item、落在 function_call 与 function_call_output 之间；
deepseek responses 要求 output 紧跟 call → 400。而 `deepseek-v4-pro` 配的是
`anthropic/deepseek-v4-pro` + `/anthropic` 原生路径，text+tool_use 天然合法，所以
pro 正常、flash 卡。子 agent（browser-operator）默认跟随主模型，主会话当时是 flash，
于是反复触发。

**修复（根治，config.yaml）**：flash 改走与 pro 同款原生 anthropic 路径——

```yaml
- model_name: "deepseek-v4-flash"
  litellm_params:
    model: "anthropic/deepseek-v4-flash"
    api_base: "https://api.deepseek.com/anthropic"
```

实测同一份"text+tool_use 同条"结构：`/v1` responses 路径 400，`/anthropic` 原生
路径 200 且正常返回 tool_use。

**为什么不做 fallback**：litellm `_should_retry()`（utils.py:6339）对 400 返回
False——400 是客户端错误，**不会触发 fallback 重试**。给 flash 配 fallback 对这个
错无效，只会让失败直接透传。真正的问题是路由路径，换路径根治。

**尝试过的 hook 方案已撤回**：曾在 custom_callbacks 里加 `_move_tool_call_
narration_to_next_user` 把 text 挪进下一条 user，但 deepseek 原生 anthropic 端点也
要求 tool_result 紧贴 tool_use 且在消息开头，挪到 text 前面反而 400（实测）。
切到原生路径后不需要任何 narration 变换，已整体撤销。custom_callbacks 的剥图 +
同角色合并（2026-08-09）保持不变。

**验证**：`test_sanitize.py` 全绿；活链两测：原结构打 `/anthropic` 200 + tool_use，
hook 变换后结构打 `/anthropic` 复现"tool_result 紧邻"400（证明确实不能搬 text）。

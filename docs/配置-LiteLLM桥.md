# 配置教程：LiteLLM 本地桥（可选进阶）

> 一个本地代理（127.0.0.1:4000），把多家模型统一成一个入口：
> GUI 里只配一个"LiteLLM 本地桥"预设，顶栏点模型名即可热切换后端模型。
> 适合：想同时挂 DeepSeek/GLM/GPT 多家随时切、想省钱（按对话复杂度选模型）。

## 部署

1. 进入 `本地转接/LiteLLM/`
2. 复制 `config.yaml.example` 为 `config.yaml`，把里面的 key 换成你的
   （模板带了 DeepSeek/GLM 的可用配置和 OpenAI 兼容中转站写法，照注释填）
3. 首次运行先建环境：
   ```
   python -m venv venv
   venv\Scripts\pip install "litellm[proxy]==1.95.0" fastapi==0.136.3
   ```
   （fastapi 版本必须钉 0.136.3，新版删了 LiteLLM 依赖的函数）
4. 双击 `启动-LiteLLM.bat`，看到 4000 端口监听即成功。
   （GUI 也能全托管它：⚙ 设置里开了"自动管理 LiteLLM"就不用手动启动）

## 接入 GUI

⚙ 模型预设 → 添加：
- 名称：`LiteLLM`
- base_url：`http://127.0.0.1:4000`
- token：`sk-denia-local`（= config.yaml 里的 master_key）
- model：config.yaml 里 `model_list` 定义的任一别名，如 `deepseek-v4-flash`

顶栏副标题点模型名 → 下拉热切换 config.yaml 里定义的所有别名。

## 注意事项

- config.yaml 含真实 key，已被 gitignore，不要提交、不要发给别人
- 别在 config.yaml 里写中文（yaml 解码问题），注释除外
- 加新模型 = 在 model_list 里照样子加一段，保存后重启 LiteLLM

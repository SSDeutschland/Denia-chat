# 配置教程：TTS 语音（让她开口说话）

> 写给帮你配置的 AI：本项目的 TTS 已全部实现好（GUI 后端 `server_sdk.py` 内含
> TTS 后端注册表与全托管进程管理），你要做的只是**准备声线环境和改预设**。
> 三条路线，按用户条件选一。

## 路线 A：本地 GPT-SoVITS（效果最好，需要 NVIDIA 显卡或耐心等 CPU）

1. 下载 GPT-SoVITS 整合包（v2ProPlus）：https://github.com/RVC-Boss/GPT-SoVITS
   解压到任意目录，记下路径（下称 `<SoVITS目录>`）
2. 下载达妮娅声线权重（ModelScope，作者 villia）：
   https://modelscope.cn/models/villia/dania
   把权重文件放进 `<SoVITS目录>/GPT_weights_v2ProPlus/` 和 `SoVITS_weights_v2ProPlus/`
3. 准备一段 3-10 秒的参考音频（游戏内达妮娅语音，wav），记下路径和它对应的台词文本
4. GUI ⚙ 设置 → 🔊 TTS：
   - enabled：开，backend：`sovits`
   - sovits_dir：`<SoVITS目录>`（含 api_v2.py 的那层）
   - python：`<SoVITS目录>\runtime\python.exe`（整合包自带）
   - ref_audio：参考音频相对 sovits_dir 的路径
   - prompt_text：参考音频对应的台词
5. 验证：聊天气泡上出现半透明喇叭按钮，点它出声即成功。

> CPU 也能跑但慢（RTF≈1.4，10 秒语音约 14 秒合成）。GUI 是异步合成，不卡聊天。

## 路线 B：百炼云 CosyVoice（零本地部署，按量付费）

1. 阿里云百炼 https://bailian.console.aliyun.com 开通 → 拿 API key（dashscope）
2. 「语音合成 CosyVoice」→ 声音复刻：上传一段达妮娅游戏语音，得到音色 ID
   （形如 `cosyvoice-v3.5-plus-xxxx-xxxx`）
3. ⚙ 设置 → 🔊 TTS：backend 选 `bailian`，voice_id 填音色 ID
   - key 回落顺序：tts 配置 → 识图预设里的 dashscope key
4. 验证同路线 A。

## 路线 C：本地 CosyVoice（介于两者之间）

参照路线 A，但部署 CosyVoice（https://github.com/FunAudioLLM/CosyVoice），
backend 选 `cosyvoice`，填 cosy_dir / python / ref_audio / prompt_text。

## 口语化预处理（可选）

她的文字回复是书面语，直接念会怪。开启后由小模型先改写成口语再合成：
⚙ TTS 预处理：enabled 开，填任一 OpenAI 兼容端点（base_url/model/token）。

## 排错

- 喇叭点了没声：看 "Denia Backend" 窗口日志；SoVITS 首次启动慢（冷启 1-2 分钟）属正常
- 端口被占：tts_sovits.port 默认 9880，被占就改一个并同步改 SoVITS 启动端口
- 合成出来是机器人腔：参考音频质量不够，换一段更清晰、情感更明显的

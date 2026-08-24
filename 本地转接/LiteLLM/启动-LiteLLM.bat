@echo off
rem 启动 LiteLLM 本地桥（端口 4000，novadiff GPT 池）
cd /d "%~dp0"
venv\Scripts\litellm.exe --config config.yaml --port 4000 --host 127.0.0.1

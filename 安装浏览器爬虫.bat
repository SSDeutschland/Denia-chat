@echo off
chcp 936 >nul
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
    echo [ERR] 没找到 python。请先安装 Python 3.10+（勾选 Add to PATH）。
    pause
    exit /b 1
)
echo [*] 安装浏览器爬虫（首次约下载 150MB Chromium，请保持网络通畅）...
python tools\browser-crawler\setup.py
pause

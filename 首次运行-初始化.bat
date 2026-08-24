@echo off
chcp 936 >nul
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
    echo [ERR] 没找到 python。请先安装 Python 3.10+（安装时勾选 Add to PATH），再重新双击本文件。
    pause
    exit /b 1
)
python tools_pack\init.py
pause

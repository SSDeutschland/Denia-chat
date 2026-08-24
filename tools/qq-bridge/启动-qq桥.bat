@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=..\..\GUI\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [启动-qq桥] 找不到 GUI venv：%PY%
  echo 先跑一次 启动达妮娅GUI.bat 建 venv，再来启动桥。
  pause
  exit /b 1
)
"%PY%" bridge.py
pause

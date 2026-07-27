@echo off
title Denia GUI Launcher

rem GUI mode launcher (sits next to CC-mode launcher qi-dong-denia.bat).
rem Everything GUI lives in GUI\ ; this script only enters it and bootstraps.
cd /d "%~dp0GUI"

set "PY=venv\Scripts\python.exe"
set "URL=http://127.0.0.1:8765"

rem ---- First run: bootstrap venv + dependencies (portable, no shipped venv) ----
if not exist "%PY%" (
    echo [*] First run: creating venv and installing dependencies ...
    where python
    if errorlevel 1 (
        echo [ERR] python not found on PATH. Install Python 3.10+ first, then rerun.
        pause
        exit /b 1
    )
    python -m venv venv
    if errorlevel 1 (
        echo [ERR] venv creation failed.
        pause
        exit /b 1
    )
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [WARN] pip install failed, retrying with Tsinghua mirror ...
        "%PY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        if errorlevel 1 (
            echo [ERR] pip install failed twice. Check network / proxy.
            pause
            exit /b 1
        )
    )
    echo [*] Dependencies ready.
)

rem ---- Load provider credentials from .env.local (KEY=VALUE per line) ----
rem This file is git-ignored. Without it the SDK cannot reach the provider
rem and chat gets no response.
if exist ".env.local" (
    echo [*] Loading provider config from .env.local
    for /f "usebackq tokens=1,* delims==" %%A in (".env.local") do set "%%A=%%B"
) else (
    echo [WARN] .env.local not found. If chat gets no response, create it with:
    echo        ANTHROPIC_BASE_URL=...    and    ANTHROPIC_AUTH_TOKEN=...
)

echo [*] Starting Denia backend (server_sdk.py) ...
start "Denia Backend" "%PY%" server_sdk.py

echo [*] Waiting for backend to come up (about 4s) ...
timeout /t 4 /nobreak

echo [*] Opening frontend: %URL%
start "" "%URL%"

echo.
echo [OK] Launched. Frontend is in your browser; backend runs in the "Denia Backend" window.
echo      Close that window to stop the service.
echo.
timeout /t 3 /nobreak
exit /b 0

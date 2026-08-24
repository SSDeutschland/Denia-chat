@echo off
chcp 936 >nul
title Denia QQ 分身 — 一键全链路
cd /d "%~dp0"

rem ============================================================
rem  【配置区】使用前先把下面四项改成你自己的（改完保存即可）
rem ============================================================
set "NAPCAT_DIR=<NapCat安装目录，如 E:\NapCat\napcat>"
set "QQ_EXE=<QQ.exe完整路径>"
set "BOT_QQ=<分身QQ号>"
set "WEBUI_URL=http://127.0.0.1:6099/webui?token=<NapCat配置的token>"
rem ============================================================

echo ============================================
echo   达妮娅 × QQ 分身 — 一键全链路
echo ============================================
echo.

rem ---------- 管理员权限（NapCat 注入 QQ 需要管理员） ----------
net session >nul 2>&1
if errorlevel 1 (
    echo [0/4] 需要管理员权限，正在请求 UAC ...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb runAs"
    exit /b
)

rem ---------- 1. server_sdk（8765 HTTP / 8766 WS） ----------
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [1/4] server_sdk 已在跑（8765 监听中），复用。
    goto backend_ok
)
echo [1/4] 拉起 server_sdk ...
call "启动达妮娅GUI.bat"
cd /d "%~dp0"
set /a w=0
:wait_backend
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul
if not errorlevel 1 goto backend_ok
set /a w+=1
if %w% gtr 30 (
    echo   [ERR] server_sdk 30 秒未起来，请查看 Denia Backend 窗口。
    goto done
)
timeout /t 1 /nobreak >nul
goto wait_backend
:backend_ok
echo        server_sdk 已就绪

rem ---------- 2. QQ 检测：QQ.exe 在跑但 6099 未监听 = 没注入 NapCat ----------
tasklist /fi "IMAGENAME eq QQ.exe" | findstr /i "QQ.exe" >nul
if errorlevel 1 goto start_napcat
netstat -ano | findstr ":6099" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [2/4] NapCat 与 QQ 都在跑，直接复用。
    goto wait_link
)
echo [2/4] 检测到未注入 NapCat 的 QQ 进程（单实例会挡住注入）。
choice /c YN /n /m "      结束 QQ 进程并重新注入？[Y/n] "
if errorlevel 2 goto manual
taskkill /IM QQ.exe /F >nul 2>&1
timeout /t 2 /nobreak >nul

rem ---------- 3. 启动 NapCat（拉起 QQ 并自动登录分身号） ----------
:start_napcat
echo [3/4] 启动 NapCat（自动登录分身 %BOT_QQ%）...
set "NAPCAT_PATCH_PACKAGE=%NAPCAT_DIR%\qqnt.json"
set "NAPCAT_LOAD_PATH=%NAPCAT_DIR%\loadNapCat.js"
set "NAPCAT_INJECT_PATH=%NAPCAT_DIR%\NapCatWinBootHook.dll"
set "NAPCAT_LAUNCHER_PATH=%NAPCAT_DIR%\NapCatWinBootMain.exe"
set "NAPCAT_MAIN_PATH=%NAPCAT_DIR%\napcat.mjs"
start "NapCat 控制台" cmd /k "chcp 65001>nul & cd /d %NAPCAT_DIR% & %NAPCAT_LAUNCHER_PATH% %QQ_EXE% %NAPCAT_INJECT_PATH% -q %BOT_QQ%"

rem ---------- 4. 等 NapCat 反向 WS 连上桥（8790 ESTABLISHED，最长 60 秒） ----------
:wait_link
echo [4/4] 等待 NapCat 连接桥（最长 60 秒）...
set /a n=0
:wait_napcat
netstat -ano | findstr ":8790" | findstr "ESTABLISHED" >nul
if not errorlevel 1 goto success
set /a n+=1
if %n% gtr 60 goto manual
timeout /t 1 /nobreak >nul
goto wait_napcat

:success
echo.
echo   [OK] 全链路就绪：server_sdk + QQ 桥 + NapCat 已连通。
echo        NapCat 控制台：%WEBUI_URL%
echo        GUI 前端：http://127.0.0.1:8765
echo.
start "" "%WEBUI_URL%"
goto done

:manual
echo.
echo   [WARN] 链路未连通。打开控制台手动登录/排查：
echo          %WEBUI_URL%
echo          并确认 GUI 设置里 QQ 分身 enabled 已打开。
start "" "%WEBUI_URL%"
goto done

:done
echo.
echo ============================================
echo   引导流程结束（NapCat 控制台窗口请勿关闭）
echo ============================================
pause

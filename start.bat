@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Terraria-Agent-V1
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "VENV=%~dp0.venv"
set "PY=%VENV%\Scripts\python.exe"

echo ============================================================
echo   Terraria-Agent-V1  One-Click Launcher
echo ============================================================
echo.

netstat -ano | findstr ":5000" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo [INFO] Terraria-Agent is already running.
    echo [INFO] 已在运行: http://127.0.0.1:5000（请直接切到已有面板）
    echo.
    echo This window can be closed. The agent is already running.
    pause
    exit /b 0
)

if exist "%PY%" goto check_deps

echo [1/3] Creating Python virtual environment...
where python >nul 2>nul
if not errorlevel 1 (
    python --version >nul 2>nul
    if not errorlevel 1 (
        set "BOOT=python"
        goto make_venv
    )
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 --version >nul 2>nul
    if not errorlevel 1 (
        set "BOOT=py -3"
        goto make_venv
    )
)

echo [ERROR] Python 3.10+ was not found.
echo Install it from https://www.python.org/downloads/
echo Remember to enable "Add Python to PATH" during installation.
echo.
pause
exit /b 1

:make_venv
echo Using: %BOOT%
%BOOT% -m venv "%VENV%"
if errorlevel 1 (
    echo [ERROR] Failed to create the virtual environment.
    pause
    exit /b 1
)

:check_deps
"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer is required.
    pause
    exit /b 1
)

if exist "%VENV%\.deps_ok" (
    "%PY%" -c "import flask, waitress, requests, pyperclip, mss, pygetwindow, psutil, numpy, cv2, pydirectinput" >nul 2>nul
    if not errorlevel 1 goto run
    del "%VENV%\.deps_ok" >nul 2>nul
)

rem If a pre-built venv already has the core packages, skip network entirely.
"%PY%" -c "import flask, waitress, requests, pyperclip, mss, pygetwindow, psutil, numpy, cv2, pydirectinput" >nul 2>nul
if not errorlevel 1 (
    > "%VENV%\.deps_ok" echo ok
    goto run
)

echo [2/3] Installing dependencies (first run only)...
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency installation failed.
    echo Check your network, then delete the .venv folder and run start.bat again.
    pause
    exit /b 1
)
> "%VENV%\.deps_ok" echo ok

:run
echo [3/3] Starting Terraria-Agent-V1...
echo Web panel: http://127.0.0.1:5000 (browser auto-open disabled)
echo Keep this window open while the agent is running.
echo.
"%PY%" main.py
set "EXITCODE=%ERRORLEVEL%"
echo.
echo [INFO] Program exited with code %EXITCODE%.
echo See logs folder: agent.log
pause
exit /b %EXITCODE%

@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Muse Local Video Workspace
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%lib\.venv\Scripts\python.exe"
set "SCRIPT=%ROOT%lib\web_app\server.py"

if /i "%~1"=="--check" (
    if not exist "%PYTHON%" (
        echo [ERROR] Application environment is missing. Run 00_setup_or_update.bat first.
        exit /b 2
    )
    "%PYTHON%" "%SCRIPT%" --check
    exit /b %ERRORLEVEL%
)

if not exist "%PYTHON%" (
    echo [ERROR] Application environment is missing.
    echo [INFO] Run 00_setup_or_update.bat first.
    pause
    exit /b 2
)

echo [INFO] Starting Muse at http://127.0.0.1:8765/
echo [INFO] Keep this window open while using the interface.
"%PYTHON%" "%SCRIPT%"
if errorlevel 1 (
    echo [ERROR] Muse stopped unexpectedly.
    pause
    exit /b %ERRORLEVEL%
)

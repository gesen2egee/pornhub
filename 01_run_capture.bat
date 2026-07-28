@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Video 5x5 Grid Capture and GPU Tagger
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
set "SCRIPT=%ROOT%lib\capture_frames.py"

if not exist "%PYTHON%" (
    echo [ERROR] Environment is missing. Run 00_setup_or_update.bat first.
    pause
    exit /b 2
)

if /i "%~1"=="--check" (
    "%PYTHON%" -c "import yt_dlp, PIL, numpy, curl_cffi, mutagen, requests, huggingface_hub, onnxruntime"
    if errorlevel 1 exit /b !ERRORLEVEL!
    echo [OK] Capture environment and paths are ready.
    exit /b 0
)

set "URL="
set /p "URL=Please paste Video, Keyword or Page URL (Enter for default): "
if "!URL!"=="" set "URL=https://www.eporner.com/country-top/tw/"

set "PAGES=1"
set /p "PAGES=Enter number of pages (Enter for 1): "
if "!PAGES!"=="" set "PAGES=1"

"%PYTHON%" "%SCRIPT%" "!URL!" -p !PAGES! -q 480p
if errorlevel 1 (
    set "CAPTURE_EXIT=!ERRORLEVEL!"
    echo [ERROR] Capture failed with exit code !CAPTURE_EXIT!.
    pause
    exit /b !CAPTURE_EXIT!
)

echo [DONE] 5x5 preview images are in output\01_preview_images.
pause

@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Video 5x5 Grid Capture and GPU Tagger Filter
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

set "RATING=general"
set /p "RATING=Required RATING (Enter for general): "
if "!RATING!"=="" set "RATING=general"

set "RATING_CONF=50"
set /p "RATING_CONF=Minimum RATING confidence %% (Enter for 50): "
if "!RATING_CONF!"=="" set "RATING_CONF=50"

set "REQUIRED_TAG=smile"
set /p "REQUIRED_TAG=Required TAG (Enter for smile): "
if "!REQUIRED_TAG!"=="" set "REQUIRED_TAG=smile"

set "TAG_CONF=50"
set /p "TAG_CONF=Minimum TAG confidence %% (Enter for 50): "
if "!TAG_CONF!"=="" set "TAG_CONF=50"

"%PYTHON%" "%SCRIPT%" "!URL!" -p !PAGES! -q 480p --rating "!RATING!" --rating-min-confidence !RATING_CONF!e-2 --required-tag "!REQUIRED_TAG!" --tag-min-confidence !TAG_CONF!e-2
if errorlevel 1 (
    set "CAPTURE_EXIT=!ERRORLEVEL!"
    echo [ERROR] Capture failed with exit code !CAPTURE_EXIT!.
    pause
    exit /b !CAPTURE_EXIT!
)

echo [DONE] Passed 5x5 preview images are in output\01_preview_images.
pause

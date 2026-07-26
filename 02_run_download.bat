@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Video Download and Subtitle Pipeline
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
set "MOSS_PYTHON=%PYTHON%"
set "SCRIPT=%ROOT%lib\run_download.py"
if /i "%~1"=="--retry-subtitles" set "NO_PAUSE=1"
if /i "%~1"=="--repair-over-1080" set "NO_PAUSE=1"

if not exist "%PYTHON%" (
    echo [ERROR] Application environment is missing. Run 00_setup_or_update.bat first.
    if not defined NO_PAUSE pause
    exit /b 2
)
if /i "%~1"=="--check" (
    "%PYTHON%" -c "import yt_dlp, PIL, numpy, curl_cffi, mutagen, requests"
    if errorlevel 1 exit /b !ERRORLEVEL!
    "%MOSS_PYTHON%" -c "import mutagen, PIL, requests"
    if errorlevel 1 exit /b !ERRORLEVEL!
    echo [OK] The shared download and subtitle environment is ready.
    exit /b 0
)

echo [INFO] Downloads and subtitles run in parallel.
echo [INFO] 03_videos uses sidecar SRT; 02_preview_videos uses hard subtitles.

"%PYTHON%" "%SCRIPT%" %*
if errorlevel 1 (
    set "DOWNLOAD_EXIT=!ERRORLEVEL!"
    echo [ERROR] Pipeline failed with exit code !DOWNLOAD_EXIT!.
    if not defined NO_PAUSE pause
    exit /b !DOWNLOAD_EXIT!
)

echo [DONE] Download and subtitle pipeline completed.
if not defined NO_PAUSE pause

@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Video 3x3 Grid Collage Generator (720P)
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [INFO] Creating the Python 3.12 capture environment...
    py -3.12 -m venv "%ROOT%.venv"
    if errorlevel 1 (
        echo [ERROR] Python 3.12 is required.
        pause
        exit /b 2
    )
)

"%PYTHON%" -c "import yt_dlp, PIL, numpy, curl_cffi, mutagen, requests" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Installing capture dependencies...
    "%PYTHON%" -m pip install -r "%ROOT%requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Failed to install capture dependencies.
        pause
        exit /b 2
    )
)

if /i "%~1"=="--check" (
    echo [OK] Capture environment and syntax check passed.
    exit /b 0
)

echo ==================================================
echo     Video 3x3 Grid Collage Generator (720P)
echo ==================================================
echo.

set "URL="
set /p "URL=Please paste Video, Keyword or Page URL (Press Enter to default Homepage): "

if "!URL!"=="" set "URL=https://www.eporner.com/country-top/tw/"

set "PAGES=1"
set /p "PAGES=Enter number of pages to scrape (Press Enter to default: 1): "

if "!PAGES!"=="" set "PAGES=1"

echo.
echo [*] Target Input: "!URL!"
echo [*] Pages count: !PAGES! page(s)
echo [*] Starting 3x3 Grid Collage Generation (720P)...
echo.

"%PYTHON%" "%ROOT%capture_frames.py" "!URL!" -p !PAGES! -q 720p -o previews
if errorlevel 1 (
    set "CAPTURE_EXIT=!ERRORLEVEL!"
    echo.
    echo ==================================================
    echo [ERROR] 3x3 Collage failed with exit code !CAPTURE_EXIT!.
    echo ==================================================
    pause
    exit /b !CAPTURE_EXIT!
)

echo.
echo ==================================================
echo [DONE] 3x3 Collage Completed! Check "previews" folder.
echo ==================================================
pause

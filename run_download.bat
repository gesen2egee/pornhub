@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Video Download and Subtitle Pipeline
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "MOSS_PYTHON=%ROOT%moss\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [INFO] Creating the Python 3.12 download environment...
    py -3.12 -m venv "%ROOT%.venv"
    if errorlevel 1 (
        echo [ERROR] Python 3.12 is required.
        pause
        exit /b 2
    )
)

"%PYTHON%" -c "import yt_dlp, PIL, numpy, curl_cffi, mutagen, requests" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Installing download dependencies...
    "%PYTHON%" -m pip install -r "%ROOT%requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Failed to install download dependencies.
        pause
        exit /b 2
    )
)

if not exist "%MOSS_PYTHON%" (
    echo [ERROR] MOSS environment not found:
    echo %MOSS_PYTHON%
    echo Run install_moss.bat first.
    pause
    exit /b 2
)

"%MOSS_PYTHON%" -c "import mutagen, PIL, requests" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] The MOSS environment is missing metadata dependencies.
    echo Run install_moss.bat again.
    pause
    exit /b 2
)

if /i "%~1"=="--check" (
    echo [OK] Batch environment and syntax check passed.
    exit /b 0
)

echo ==================================================
echo       Video Download and Subtitle Pipeline
echo ==================================================
echo.
echo [INFO] Download and subtitle workers run in parallel.
echo [INFO] Each completed download is queued for the full subtitle pipeline.
echo [INFO] Finished videos move from temp into their final folders.
echo [INFO] videos grids move to downloaded; low_videos grids stay in place.
echo.

"%PYTHON%" "%ROOT%run_download.py"
if errorlevel 1 (
    set "DOWNLOAD_EXIT=!ERRORLEVEL!"
    echo.
    echo ==================================================
    echo [ERROR] Pipeline failed with exit code !DOWNLOAD_EXIT!.
    echo ==================================================
    pause
    exit /b !DOWNLOAD_EXIT!
)

echo.
echo ==================================================
echo [DONE] Download and subtitle pipeline completed.
echo ==================================================
pause

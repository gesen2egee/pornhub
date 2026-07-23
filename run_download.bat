@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Pornhub Full Video Downloader (run_download)
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [*] 首次執行，正在建立下載工具 Python 環境...
    py -3.12 -m venv "%ROOT%.venv"
    if errorlevel 1 (
        echo [ERROR] 無法建立 Python 3.12 環境，請先安裝 Python 3.12。
        pause
        exit /b 2
    )
)

"%PYTHON%" -c "import yt_dlp, PIL, numpy, curl_cffi" >nul 2>nul
if errorlevel 1 (
    echo [*] 正在安裝下載工具依賴...
    "%PYTHON%" -m pip install -r "%ROOT%requirements.txt"
    if errorlevel 1 (
        echo [ERROR] 下載工具依賴安裝失敗。
        pause
        exit /b 2
    )
)

echo ==================================================
echo      Pornhub Full Video Downloader (run_download)
echo ==================================================
echo.
echo [*] Scanning "videos" folder for moved 3x3 Grid Collages (.jpg)...
echo [*] Downloading highest quality full videos to "videos" folder...
echo [*] Auto-deleting .jpg collages upon successful download!
echo.

"%PYTHON%" "%ROOT%run_download.py"
if errorlevel 1 (
    set "DOWNLOAD_EXIT=!ERRORLEVEL!"
    echo.
    echo ==================================================
    echo [ERROR] Download task failed with exit code !DOWNLOAD_EXIT!.
    echo ==================================================
    pause
    exit /b !DOWNLOAD_EXIT!
)

echo.
echo ==================================================
echo [DONE] Download task completed!
echo ==================================================
pause

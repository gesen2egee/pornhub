@echo off
setlocal EnableExtensions EnableDelayedExpansion
title 影片下載與完整字幕整合流程
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "MOSS_PYTHON=%ROOT%moss\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [*] 首次執行，正在建立下載工具 Python 環境...
    py -3.12 -m venv "%ROOT%.venv"
    if errorlevel 1 (
        echo [ERROR] 無法建立 Python 3.12 環境，請先安裝 Python 3.12。
        pause
        exit /b 2
    )
)

"%PYTHON%" -c "import yt_dlp, PIL, numpy, curl_cffi, mutagen, requests" >nul 2>nul
if errorlevel 1 (
    echo [*] 正在安裝下載工具依賴...
    "%PYTHON%" -m pip install -r "%ROOT%requirements.txt"
    if errorlevel 1 (
        echo [ERROR] 下載工具依賴安裝失敗。
        pause
        exit /b 2
    )
)

if not exist "%MOSS_PYTHON%" (
    echo [ERROR] 找不到 MOSS 字幕環境：%MOSS_PYTHON%
    echo 請先執行 install_moss.bat。
    pause
    exit /b 2
)

"%MOSS_PYTHON%" -c "import mutagen, PIL, requests" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] MOSS 字幕環境缺少新版 Meta 依賴。
    echo 請重新執行 install_moss.bat。
    pause
    exit /b 2
)

echo ==================================================
echo        影片下載 + 完整字幕整合管線
echo ==================================================
echo.
echo [*] 下載與字幕使用獨立程序並行處理。
echo [*] 每支影片下載後立即排入音訊判斷、增強、MOSS、翻譯、硬字幕與 Meta。
echo [*] 九宮格只會在該支影片完整成功後移至 downloads。
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
echo [DONE] 下載與字幕整合流程完成！
echo ==================================================
pause

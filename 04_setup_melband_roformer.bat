@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Mel-Band-Roformer Vocal Setup
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "SCRIPT=%ROOT%lib\mel_band_roformer_vocal.py"

if not exist "%PYTHON%" (
    echo [ERROR] 找不到專案 VENV，請先執行 00_setup_or_update.bat。
    exit /b 2
)

echo [1/2] 安裝 Mel-Band-Roformer 最小推理依賴...
"%PYTHON%" -m pip install "beartype==0.14.1" "rotary-embedding-torch==0.3.5"
if errorlevel 1 exit /b %ERRORLEVEL%

echo [2/2] 下載固定版本推理程式與 MIT 權重（約 871 MiB）...
"%PYTHON%" "%SCRIPT%" --setup
if errorlevel 1 exit /b %ERRORLEVEL%

echo.
echo [DONE] Mel-Band-Roformer 人聲分離已就緒。
exit /b 0

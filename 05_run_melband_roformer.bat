@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Mel-Band-Roformer Vocal Separation
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "SCRIPT=%ROOT%lib\mel_band_roformer_vocal.py"

if not exist "%PYTHON%" (
    echo [ERROR] 找不到專案 VENV，請先執行 00_setup_or_update.bat。
    exit /b 2
)

if "%~1"=="" (
    echo 用法：05_run_melband_roformer.bat ^<輸入音檔^> [輸出資料夾]
    echo 例如：05_run_melband_roformer.bat "input.wav" "output\roformer"
    exit /b 2
)

set "OUTPUT=%~2"
if "%OUTPUT%"=="" set "OUTPUT=%ROOT%output\00_temp\melband_roformer"

"%PYTHON%" "%SCRIPT%" --input "%~f1" --output-dir "%OUTPUT%"
exit /b %ERRORLEVEL%

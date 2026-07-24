@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%confucius4_tts\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [錯誤] 找不到 Confucius4-TTS 環境，請先執行 install_confucius4_tts.bat。
    exit /b 2
)

"%PYTHON%" "%ROOT%run_confucius4_tts.py" %*
exit /b %ERRORLEVEL%

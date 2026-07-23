@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%moss\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [錯誤] 找不到 MOSS VENV：%PYTHON%
    echo 請先執行 install_moss.bat。
    exit /b 2
)

echo [開始] MOSS -^> OpenRouter -^> 低畫質硬字幕／一般影片軟字幕
"%PYTHON%" "%ROOT%run_subtitle.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
echo [結束] ExitCode=%EXIT_CODE%
exit /b %EXIT_CODE%

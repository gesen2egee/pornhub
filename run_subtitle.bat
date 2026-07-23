@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ROOT=%~dp0"
if not defined ASR_BACKEND set "ASR_BACKEND=whisper"

if /I "%ASR_BACKEND%"=="whisper" (
    set "PYTHON=%ROOT%whisper\.venv\Scripts\python.exe"
) else if /I "%ASR_BACKEND%"=="moss" (
    set "PYTHON=%ROOT%moss\.venv\Scripts\python.exe"
) else (
    echo [錯誤] ASR_BACKEND 只允許 whisper 或 moss。
    exit /b 2
)

if not exist "%PYTHON%" (
    if /I "%ASR_BACKEND%"=="moss" (
        echo [錯誤] 找不到 MOSS VENV：%PYTHON%
        echo 請先執行 install_moss.bat。
    ) else (
        echo [錯誤] 找不到 Whisper VENV：%PYTHON%
    )
    exit /b 2
)

echo [開始] ASR_BACKEND=%ASR_BACKEND% -^> OpenRouter -^> 同目錄同名.srt -^> 覆蓋原始影片
"%PYTHON%" "%ROOT%run_subtitle.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
echo [結束] ExitCode=%EXIT_CODE%
exit /b %EXIT_CODE%

@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%whisper\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [錯誤] 找不到 Whisper VENV：%PYTHON%
    pause
    exit /b 2
)

echo [開始] faster-whisper -^> OpenRouter -^> 同名.srt -^> ffmpeg 軟字幕 _sub.mp4
"%PYTHON%" "%ROOT%run_subtitle.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
echo [結束] ExitCode=%EXIT_CODE%
exit /b %EXIT_CODE%

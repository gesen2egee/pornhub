@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%qwen-asr\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [錯誤] 找不到 Qwen3-ASR VENV：%PYTHON%
    exit /b 2
)

if not defined OPENROUTER_API_KEY if not defined OPENROUTER_KEY (
    echo [錯誤] 請先設定 OPENROUTER_API_KEY 環境變數。
    echo 例如：set OPENROUTER_API_KEY=sk-or-v1-...
    exit /b 2
)

echo [開始] low_videos -^> Qwen3-ASR -^> OpenRouter Grok 4.5 -^> videos\同名.srt
"%PYTHON%" "%ROOT%run_subtitle.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
echo [結束] ExitCode=%EXIT_CODE%
exit /b %EXIT_CODE%

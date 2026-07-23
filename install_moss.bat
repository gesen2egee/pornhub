@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%moss\.venv\Scripts\python.exe"

where nvidia-smi >nul 2>nul
if errorlevel 1 (
    echo [錯誤] 找不到 nvidia-smi，請先安裝 NVIDIA Driver。
    exit /b 2
)

py -3.12 -c "import sys; assert sys.version_info[:2] == (3, 12)"
if errorlevel 1 (
    echo [錯誤] 找不到 Python 3.12，請先安裝後再執行。
    exit /b 2
)

if not exist "%PYTHON%" (
    py -3.12 -m venv "%ROOT%moss\.venv"
    if errorlevel 1 exit /b %ERRORLEVEL%
)

"%PYTHON%" -m pip install --upgrade pip
if errorlevel 1 exit /b %ERRORLEVEL%

"%PYTHON%" -m pip install --index-url https://download.pytorch.org/whl/cu128 torch torchaudio
if errorlevel 1 exit /b %ERRORLEVEL%

"%PYTHON%" -m pip install "git+https://github.com/OpenMOSS/MOSS-Transcribe-Diarize.git@9990574e6ac62390a21bcce25a914d66ac92c25e" modelscope requests
if errorlevel 1 exit /b %ERRORLEVEL%

"%PYTHON%" "%ROOT%moss_setup.py"
if errorlevel 1 exit /b %ERRORLEVEL%

echo [完成] MOSS Windows CUDA 環境與 ModelScope 模型已就緒。
exit /b 0

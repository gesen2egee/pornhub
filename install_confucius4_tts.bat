@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "ROOT=%~dp0"
set "RUNTIME=%ROOT%confucius4_tts"
set "SOURCE=%RUNTIME%\source"
set "PYTHON=%RUNTIME%\.venv\Scripts\python.exe"
set "REPOSITORY=https://github.com/netease-youdao/Confucius4-TTS.git"
set "COMMIT=186983518e9e8ab9af69cabdda3436a76d6ccdfb"

where nvidia-smi >nul 2>nul
if errorlevel 1 (
    echo [錯誤] 找不到 nvidia-smi，請先安裝 NVIDIA Driver。
    exit /b 2
)

where git >nul 2>nul
if errorlevel 1 (
    echo [錯誤] 找不到 Git，請先安裝 Git for Windows。
    exit /b 2
)

py -3.10 -c "import sys; assert sys.version_info[:2] == (3, 10)"
if errorlevel 1 (
    echo [錯誤] 找不到 Python 3.10，請先安裝後再執行。
    exit /b 2
)

if not exist "%PYTHON%" (
    py -3.10 -m venv "%RUNTIME%\.venv"
    if errorlevel 1 exit /b %ERRORLEVEL%
)

if exist "%SOURCE%" if not exist "%SOURCE%\.git" (
    echo [錯誤] %SOURCE% 已存在但不是 Git repository，請重新命名後再執行。
    exit /b 2
)

if not exist "%SOURCE%\.git" (
    git clone --no-checkout "%REPOSITORY%" "%SOURCE%"
    if errorlevel 1 exit /b %ERRORLEVEL%
)

git -C "%SOURCE%" fetch --depth 1 origin "%COMMIT%"
if errorlevel 1 exit /b %ERRORLEVEL%

git -C "%SOURCE%" checkout --detach "%COMMIT%"
if errorlevel 1 exit /b %ERRORLEVEL%

"%PYTHON%" -m pip install --upgrade pip
if errorlevel 1 exit /b %ERRORLEVEL%

"%PYTHON%" -m pip install --index-url https://download.pytorch.org/whl/cu126 torch==2.7.0 torchaudio==2.7.0
if errorlevel 1 exit /b %ERRORLEVEL%

"%PYTHON%" -m pip install -r "%SOURCE%\requirements.txt"
if errorlevel 1 exit /b %ERRORLEVEL%

"%PYTHON%" -m pip install --no-deps --editable "%SOURCE%"
if errorlevel 1 exit /b %ERRORLEVEL%

"%PYTHON%" "%ROOT%confucius4_tts_setup.py"
if errorlevel 1 exit /b %ERRORLEVEL%

echo [完成] Confucius4-TTS Windows CUDA 環境已就緒。
echo [提示] 第一次推理會從 Hugging Face 自動下載數 GB 模型。
exit /b 0

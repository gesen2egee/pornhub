@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Project Setup or Update
cd /d "%~dp0"

set "ROOT=%~dp0"
set "LIB=%ROOT%lib"
set "PYTHON=%LIB%\.venv\Scripts\python.exe"
set "MOSS_ROOT=%LIB%\moss"
set "MOSS_PYTHON=%MOSS_ROOT%\.venv\Scripts\python.exe"
set "ASMR_DIR=%MOSS_ROOT%\asmr-enhancer"
set "ASMR_COMMIT=ade1a82b4f8b97abf088280d22156448cc0a888f"
set "MOSS_COMMIT=9990574e6ac62390a21bcce25a914d66ac92c25e"

if /i "%~1"=="--check" goto check

where py >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python Launcher was not found.
    exit /b 2
)

py -3.12 -c "import sys; assert sys.version_info[:2] == (3, 12)"
if errorlevel 1 (
    echo [ERROR] Python 3.12 is required.
    exit /b 2
)

where git >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Git for Windows was not found.
    exit /b 2
)

where nvidia-smi >nul 2>nul
if errorlevel 1 (
    echo [ERROR] NVIDIA Driver and CUDA-capable GPU are required for MOSS.
    exit /b 2
)

for %%D in (
    "%ROOT%output\00_temp"
    "%ROOT%output\01_preview_images"
    "%ROOT%output\02_preview_videos"
    "%ROOT%output\03_videos"
    "%ROOT%output\04_downloaded"
) do if not exist "%%~D" mkdir "%%~D"

if not exist "%PYTHON%" (
    echo [1/6] Creating the Python 3.12 application environment...
    py -3.12 -m venv "%LIB%\.venv"
    if errorlevel 1 exit /b 1
)

echo [2/6] Updating application dependencies...
"%PYTHON%" -m pip install --upgrade pip
if errorlevel 1 exit /b %ERRORLEVEL%
"%PYTHON%" -m pip install -r "%ROOT%requirements.txt"
if errorlevel 1 exit /b %ERRORLEVEL%

if not exist "%MOSS_PYTHON%" (
    echo [3/6] Creating the Python 3.12 MOSS environment...
    py -3.12 -m venv "%MOSS_ROOT%\.venv"
    if errorlevel 1 exit /b 1
)

echo [4/6] Updating MOSS and CUDA dependencies...
"%MOSS_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 exit /b %ERRORLEVEL%
"%MOSS_PYTHON%" -m pip install --index-url https://download.pytorch.org/whl/cu128 torch torchaudio
if errorlevel 1 exit /b %ERRORLEVEL%
"%MOSS_PYTHON%" -m pip install "git+https://github.com/OpenMOSS/MOSS-Transcribe-Diarize.git@%MOSS_COMMIT%" modelscope requests mutagen Pillow
if errorlevel 1 exit /b %ERRORLEVEL%
"%MOSS_PYTHON%" -m pip install librosa pyloudnorm scipy soundfile tqdm
if errorlevel 1 exit /b %ERRORLEVEL%

echo [5/6] Updating ASMR Enhancer...
if not exist "%ASMR_DIR%\.git" (
    git clone https://github.com/xmlans/asmr-enhancer.git "%ASMR_DIR%"
    if errorlevel 1 exit /b 1
)
git -C "%ASMR_DIR%" fetch --depth 1 origin %ASMR_COMMIT%
if errorlevel 1 exit /b %ERRORLEVEL%
git -C "%ASMR_DIR%" checkout --detach %ASMR_COMMIT%
if errorlevel 1 exit /b %ERRORLEVEL%

echo [6/6] Verifying CUDA and downloading required models...
"%MOSS_PYTHON%" "%LIB%\moss_setup.py"
if errorlevel 1 exit /b %ERRORLEVEL%

echo.
echo [DONE] Setup and update completed successfully.
exit /b 0

:check
if not exist "%PYTHON%" (
    echo [ERROR] Application environment is missing. Run 00_setup_or_update.bat first.
    exit /b 2
)
if not exist "%MOSS_PYTHON%" (
    echo [ERROR] MOSS environment is missing. Run 00_setup_or_update.bat first.
    exit /b 2
)
"%PYTHON%" -c "import yt_dlp, PIL, numpy, curl_cffi, mutagen, requests"
if errorlevel 1 exit /b %ERRORLEVEL%
"%MOSS_PYTHON%" -c "import mutagen, PIL, requests"
if errorlevel 1 exit /b %ERRORLEVEL%
echo [OK] Application and MOSS environments are ready.
exit /b 0

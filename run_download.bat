@echo off
title Pornhub Full Video Downloader (run_download)

echo ==================================================
echo      Pornhub Full Video Downloader (run_download)
echo ==================================================
echo.
echo [*] Scanning "videos" folder for moved 3x3 Grid Collages (.jpg)...
echo [*] Downloading highest quality full videos to "videos" folder...
echo [*] Auto-deleting .jpg collages upon successful download!
echo.

python "%~dp0run_download.py"

echo.
echo ==================================================
echo [DONE] Download task completed!
echo ==================================================
pause

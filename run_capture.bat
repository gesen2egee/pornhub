@echo off
setlocal enabledelayedexpansion
title Video 3x3 Grid Collage Generator (720P)

echo ==================================================
echo     Video 3x3 Grid Collage Generator (720P)
echo ==================================================
echo.

set "URL="
set /p "URL=Please paste Video, Keyword or Page URL (Press Enter to default Homepage): "

if "!URL!"=="" set "URL=https://cn.pornhub.com/"

set "PAGES=1"
set /p "PAGES=Enter number of pages to scrape (Press Enter to default: 1): "

if "!PAGES!"=="" set "PAGES=1"

echo.
echo [*] Target Input: "!URL!"
echo [*] Pages count: !PAGES! page(s)
echo [*] Starting 3x3 Grid Collage Generation (720P)...
echo.

python "%~dp0capture_frames.py" "!URL!" -p !PAGES! -q 720p -o previews

echo.
echo ==================================================
echo [DONE] 3x3 Collage Completed! Check "previews" folder.
echo ==================================================
pause

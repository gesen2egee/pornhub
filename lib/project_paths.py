"""集中管理專案程式、輸出與執行環境路徑。"""

from __future__ import annotations

import os
from pathlib import Path


LIB_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = LIB_DIR.parent
_configured_output = Path(os.getenv("PORN_OUTPUT_DIR", "output"))
OUTPUT_ROOT = (
    _configured_output
    if _configured_output.is_absolute()
    else PROJECT_ROOT / _configured_output
).resolve()

TEMP_DIR = OUTPUT_ROOT / "00_temp"
PREVIEW_IMAGES_DIR = OUTPUT_ROOT / "01_preview_images"
PREVIEW_VIDEOS_DIR = OUTPUT_ROOT / "02_preview_videos"
VIDEOS_DIR = OUTPUT_ROOT / "03_videos"
DOWNLOADED_DIR = OUTPUT_ROOT / "04_downloaded"

TASKS_DIR = PROJECT_ROOT / "tasks"
DOWNLOAD_VENV_DIR = LIB_DIR / ".venv"
MOSS_DIR = LIB_DIR / "moss"
MOSS_VENV_DIR = MOSS_DIR / ".venv"
CONFUCIUS_DIR = LIB_DIR / "confucius4_tts"


def ensure_output_directories() -> None:
    """建立固定輸出目錄，不清除既有內容。"""
    for directory in (
        TEMP_DIR,
        PREVIEW_IMAGES_DIR,
        PREVIEW_VIDEOS_DIR,
        VIDEOS_DIR,
        DOWNLOADED_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)

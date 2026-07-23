"""下載流程使用的長駐字幕工作者。"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import run_subtitle
from asr_backends import create_backend
from audio_enhance_stage import auto_enhance_enabled, prepare_audio_media
from translate_srt_openrouter import DEFAULT_MODEL

try:
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
except Exception:
    pass


def archive_grid(grid: Path, archive_dir: Path) -> Path | None:
    """整支影片完成後才歸檔九宮格，並避免同名檔互相覆蓋。"""
    if not grid.exists():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_dir / grid.name
    if destination.exists():
        destination = archive_dir / (
            f"{grid.stem}-{grid.parent.name}{grid.suffix}"
        )
    counter = 2
    while destination.exists():
        destination = archive_dir / (
            f"{grid.stem}-{grid.parent.name}-{counter}{grid.suffix}"
        )
        counter += 1
    shutil.move(str(grid), str(destination))
    print(f"  [歸檔] 完整流程成功，九宮格已移至：{destination}", flush=True)
    return destination


def finalize_video(video: Path, final_video: Path) -> Path:
    """字幕與 Meta 完整後，才把暫存影片移入正式資料夾。"""
    if video == final_video:
        return final_video
    final_video.parent.mkdir(parents=True, exist_ok=True)
    if final_video.exists():
        raise RuntimeError(f"正式影片路徑已存在，拒絕覆寫：{final_video}")
    shutil.move(str(video), str(final_video))
    print(f"  [完成] 影片已移至正式路徑：{final_video}", flush=True)
    return final_video


def finish_grid(
    grid: Path,
    archive_dir: Path,
    should_archive: bool,
) -> None:
    if should_archive:
        archive_grid(grid, archive_dir)
    elif grid.exists():
        print(f"  [保留] low video 九宮格保留原位：{grid}", flush=True)


class SubtitleRuntime:
    """重用同一個 MOSS backend，逐支處理下載完成的影片。"""

    def __init__(self) -> None:
        self.backend = None
        self.api_key = (
            os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
        )
        self.model_name = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        self.use_audio_enhance = auto_enhance_enabled()
        self.configured_max_tokens = os.getenv("MOSS_MAX_NEW_TOKENS")

    def _load_backend(self):
        if self.backend is None:
            self.backend = create_backend().load()
            print("[字幕管線] MOSS 已載入，後續影片共用同一模型", flush=True)
        return self.backend

    def process(self, job: dict[str, Any]) -> None:
        video = Path(job["video"]).resolve()
        final_video = Path(job["final_video"]).resolve()
        grid = Path(job["grid"]).resolve()
        archive_dir = Path(job["archive_dir"]).resolve()
        should_archive_grid = bool(job.get("archive_grid"))
        if job.get("is_low_quality") and self.configured_max_tokens is None:
            os.environ["MOSS_MAX_NEW_TOKENS"] = "1024"
        elif self.configured_max_tokens is None:
            os.environ.pop("MOSS_MAX_NEW_TOKENS", None)
        else:
            os.environ["MOSS_MAX_NEW_TOKENS"] = self.configured_max_tokens
        print(f"\n[字幕管線] 接手：{video.name}", flush=True)

        if run_subtitle._subtitle_complete(video):
            print("[字幕管線] 已有舊 SRT 或影片字幕 Meta，直接略過字幕", flush=True)
            finalize_video(video, final_video)
            finish_grid(grid, archive_dir, should_archive_grid)
            return
        if not self.api_key:
            raise RuntimeError("找不到 OPENROUTER_API_KEY 環境變數。")

        prepared = {}
        media = None
        try:
            if self.use_audio_enhance:
                print("[字幕管線] 自動判斷音訊是否需要增強", flush=True)
                prepared = prepare_audio_media([video])
                media = prepared.get(video)
            run_subtitle.process_video(
                video,
                self._load_backend(),
                self.api_key,
                self.model_name,
                False,
                media_input=media.media_input if media else video,
                audio_enhanced=media.enhanced if media else False,
            )
        finally:
            for item in prepared.values():
                item.cleanup()

        subtitle_meta = run_subtitle._read_video_meta(video)
        if not (
            subtitle_meta.get("original_srt_present")
            and subtitle_meta.get("translated_srt_present")
        ):
            raise RuntimeError("字幕流程結束但影片內沒有完整雙字幕 Meta。")
        finalize_video(video, final_video)
        finish_grid(grid, archive_dir, should_archive_grid)


def main() -> int:
    failures = 0
    runtime: SubtitleRuntime | None = None
    print("[字幕管線] 背景工作者已啟動，等待下載完成影片", flush=True)
    for raw_line in sys.stdin:
        job: dict[str, Any] = {}
        if not raw_line.strip():
            continue
        try:
            job = json.loads(raw_line.lstrip("\ufeff"))
            if runtime is None:
                runtime = SubtitleRuntime()
            runtime.process(job)
        except Exception as exc:
            failures += 1
            name = job.get("video", "未知影片")
            print(f"[字幕管線失敗] {name}：{exc}", file=sys.stderr, flush=True)
    print(f"[字幕管線] 全部佇列完成；失敗 {failures} 支", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

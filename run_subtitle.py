"""使用 faster-whisper 的原生 segment/VAD 產生並翻譯字幕。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
VIDEOS = ROOT / "videos"
WHISPER_ROOT = ROOT / "whisper"
WHISPER_CACHE = WHISPER_ROOT / "model-cache"

sys.path.insert(0, str(ROOT))
from translate_srt_openrouter import (  # noqa: E402
    DEFAULT_MODEL,
    format_srt,
    translate_cues,
)


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm"}


def _find_videos() -> list[Path]:
    candidates: list[Path] = []
    configured_dir = os.getenv("LOW_VIDEO_DIR")
    if configured_dir:
        candidates.append(Path(configured_dir))
    candidates.extend([ROOT / "low_videos", ROOT / "low_video", VIDEOS])
    existing_dirs = [path for path in candidates if path.exists()]
    if not existing_dirs:
        raise FileNotFoundError(
            f"找不到輸入資料夾，已檢查：{', '.join(map(str, candidates))}"
        )

    sources: list[Path] = []
    seen_stems: set[str] = set()
    for directory in existing_dirs:
        for video in sorted(directory.iterdir()):
            if not video.is_file() or video.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            stem_key = video.stem.casefold()
            if stem_key in seen_stems:
                continue
            seen_stems.add(stem_key)
            sources.append(video)
    return sources


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_part, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d},{millis:03d}"


def _load_whisper_model():
    from faster_whisper import WhisperModel

    model_name = os.getenv("WHISPER_MODEL", "large-v3")
    device = os.getenv("WHISPER_DEVICE", "cuda")
    compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "float16")
    print(
        f"載入 Whisper：{model_name}，device={device}，compute_type={compute_type}",
        flush=True,
    )
    return WhisperModel(
        model_name,
        device=device,
        compute_type=compute_type,
        download_root=str(WHISPER_CACHE),
    )


def _transcribe_segments(model, video: Path) -> tuple[list[dict[str, Any]], str]:
    language = os.getenv("WHISPER_LANGUAGE") or None
    options: dict[str, Any] = {
        "beam_size": 5,
        "vad_filter": True,
    }
    if language:
        options["language"] = language
    segments, info = model.transcribe(str(video), **options)
    cues: list[dict[str, Any]] = []
    for index, segment in enumerate(segments, start=1):
        text = segment.text.strip()
        if not text:
            continue
        cues.append(
            {
                "id": index,
                "time": f"{_srt_time(segment.start)} --> {_srt_time(segment.end)}",
                "text": text,
            }
        )
    return cues, getattr(info, "language", "unknown")


def process_video(video: Path, model, api_key: str, model_name: str) -> Path:
    output_srt = VIDEOS / f"{video.stem}.srt"
    print(f"\n處理：{video.name}", flush=True)
    print("  1/2 faster-whisper VAD 與自然 segment 辨識", flush=True)
    cues, language = _transcribe_segments(model, video)
    if not cues:
        raise RuntimeError("Whisper 沒有產生有效字幕段落。")
    print(f"  語言：{language}；字幕段落：{len(cues)}", flush=True)
    print("  2/2 OpenRouter 翻譯", flush=True)
    translated = translate_cues(cues, api_key, model_name)
    temporary_output = output_srt.with_name(output_srt.name + ".tmp")
    temporary_output.write_text(format_srt(translated), encoding="utf-8-sig")
    temporary_output.replace(output_srt)
    print(f"完成：{output_srt}", flush=True)
    return output_srt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="覆寫已存在的同名 SRT")
    parser.add_argument("--limit", type=int, default=0, help="只處理前 N 部影片，0 表示全部")
    parser.add_argument("--dry-run", action="store_true", help="只列出待處理影片，不呼叫模型/API")
    args = parser.parse_args()

    videos = _find_videos()
    if not videos:
        print("low_videos、low_video、videos 都沒有可處理的影片。")
        return 0 if args.dry_run else 1
    pending = [
        video
        for video in videos
        if args.force or not (VIDEOS / f"{video.stem}.srt").exists()
    ]
    skipped = len(videos) - len(pending)
    if args.limit > 0:
        pending = pending[: args.limit]
    print(
        f"來源影片：{len(videos)} 部；略過既有字幕：{skipped} 部；待處理：{len(pending)} 部",
        flush=True,
    )
    if args.dry_run:
        for video in pending:
            print(f"{video} -> {VIDEOS / (video.stem + '.srt')}")
        return 0
    if not pending:
        print("沒有需要處理的影片。")
        return 0

    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
    if not api_key:
        print("錯誤：找不到 OPENROUTER_API_KEY 環境變數。", file=sys.stderr)
        return 2

    model = _load_whisper_model()
    model_name = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
    failures = 0
    for video in pending:
        try:
            process_video(video, model, api_key, model_name)
        except Exception as exc:
            failures += 1
            print(f"失敗：{video.name}：{exc}", file=sys.stderr, flush=True)
    print(f"批次完成：成功 {len(pending) - failures} 部，失敗 {failures} 部。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

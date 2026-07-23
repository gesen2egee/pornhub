"""使用可切換的 ASR backend 產生、翻譯並內嵌字幕。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VIDEOS = ROOT / "videos"

sys.path.insert(0, str(ROOT))
from asr_backends import create_backend, resolve_backend  # noqa: E402
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


def _has_soft_subtitle(video: Path) -> bool:
    ffprobe = os.getenv("FFPROBE_EXE", "ffprobe")
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "s:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "csv=p=0",
        video.name,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            cwd=str(video.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _subtitle_path(video: Path) -> Path:
    return video.with_suffix(".srt")


def _embed_soft_subtitle(
    video: Path, subtitle: Path, output_video: Path, force: bool
) -> Path:
    ffmpeg = os.getenv("FFMPEG_EXE", "ffmpeg")
    temporary_output = output_video.with_name(
        f".{output_video.stem}.tmp{output_video.suffix}"
    )
    is_mp4_container = output_video.suffix.lower() in {".mp4", ".mov"}
    subtitle_codec = "mov_text" if is_mp4_container else "srt"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        video.name,
        "-f",
        "srt",
        "-i",
        subtitle.name,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map",
        "1:0",
        "-map_metadata",
        "0",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-c:s",
        subtitle_codec,
        "-disposition:s:0",
        "default",
        "-metadata:s:s:0",
        "language=zho",
        "-metadata:s:s:0",
        "title=繁體中文字幕",
        temporary_output.name,
    ]
    if is_mp4_container:
        command[-1:-1] = ["-movflags", "+faststart"]
    print("  3/3 ffmpeg 軟字幕內嵌（不重新編碼）", flush=True)
    try:
        result = subprocess.run(
            command,
            check=False,
            cwd=str(video.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "找不到 ffmpeg。請先把 ffmpeg 加入 PATH，或設定 FFMPEG_EXE。"
        ) from exc

    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"ffmpeg 封裝失敗：{details[-1000:]}")
    temporary_output.replace(output_video)
    print(f"完成並覆蓋原始影片：{output_video}", flush=True)
    return output_video


def process_video(
    video: Path,
    backend,
    api_key: str | None,
    model_name: str,
    force: bool,
) -> Path:
    output_srt = _subtitle_path(video)
    output_video = video
    print(f"\n處理：{video.name}", flush=True)
    if output_srt.exists() and not force:
        print(
            f"  1/3 同名 SRT 已存在，略過 ASR 與翻譯：{output_srt}",
            flush=True,
        )
    else:
        if backend is None or not api_key:
            raise RuntimeError("缺少 ASR backend 或 OpenRouter API key。")
        print(f"  1/3 {backend.display_name} 辨識", flush=True)
        cues, language = backend.transcribe(video)
        if not cues:
            raise RuntimeError("ASR 沒有產生有效字幕段落。")
        print(f"  語言：{language}；字幕段落：{len(cues)}", flush=True)
        print("  2/3 OpenRouter 翻譯", flush=True)
        translated = translate_cues(cues, api_key, model_name)
        temporary_output = output_srt.with_name(output_srt.name + ".tmp")
        temporary_output.write_text(format_srt(translated), encoding="utf-8-sig")
        temporary_output.replace(output_srt)
        print(f"完成：{output_srt}", flush=True)

    return _embed_soft_subtitle(video, output_srt, output_video, force)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="重新產生 SRT，並重新封裝後覆蓋原始影片",
    )
    parser.add_argument("--limit", type=int, default=0, help="只處理前 N 部影片，0 表示全部")
    parser.add_argument("--dry-run", action="store_true", help="只列出待處理影片，不呼叫模型/API")
    args = parser.parse_args()

    try:
        backend_name = resolve_backend()
    except ValueError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2

    videos = _find_videos()
    if not videos:
        print("low_videos、low_video、videos 都沒有可處理的影片。")
        return 0 if args.dry_run else 1
    pending = [
        video
        for video in videos
        if (
            args.force
            or not _subtitle_path(video).exists()
            or not _has_soft_subtitle(video)
        )
    ]
    skipped = len(videos) - len(pending)
    if args.limit > 0:
        pending = pending[: args.limit]
    print(
        f"來源影片：{len(videos)} 部；略過已有 SRT 與軟字幕影片：{skipped} 部；待處理：{len(pending)} 部",
        flush=True,
    )
    if args.dry_run:
        for video in pending:
            print(
                f"{video} -> {_subtitle_path(video)} + "
                f"覆蓋原始影片"
            )
        return 0
    if not pending:
        print("沒有需要處理的影片。")
        return 0

    needs_asr = args.force or any(
        not _subtitle_path(video).exists() for video in pending
    )
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
    if needs_asr and not api_key:
        print("錯誤：找不到 OPENROUTER_API_KEY 環境變數。", file=sys.stderr)
        return 2

    backend = create_backend().load() if needs_asr else None
    if needs_asr:
        print(f"使用 ASR backend：{backend_name}", flush=True)
    model_name = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
    failures = 0
    for video in pending:
        try:
            process_video(video, backend, api_key, model_name, args.force)
        except Exception as exc:
            failures += 1
            print(f"失敗：{video.name}：{exc}", file=sys.stderr, flush=True)
    print(f"批次完成：成功 {len(pending) - failures} 部，失敗 {failures} 部。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

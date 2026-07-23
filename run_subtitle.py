"""使用 MOSS 產生、翻譯並內嵌字幕。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VIDEOS = ROOT / "videos"

sys.path.insert(0, str(ROOT))
from asr_backends import create_backend  # noqa: E402
from audio_enhance_stage import (  # noqa: E402
    ENHANCE_MARKER,
    auto_enhance_enabled,
    prepare_audio_media,
)
from translate_srt_openrouter import (  # noqa: E402
    DEFAULT_MODEL,
    format_srt,
    strip_speaker_labels,
    translate_cues,
)


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm"}


def _low_video_directories() -> list[Path]:
    directories: list[Path] = []
    configured_dir = os.getenv("LOW_VIDEO_DIR")
    if configured_dir:
        directories.append(Path(configured_dir))
    directories.extend([ROOT / "low_videos", ROOT / "low_video"])
    return directories


def _find_videos(low_only: bool = False) -> list[Path]:
    candidates: list[Path] = []
    candidates.extend(_low_video_directories())
    if not low_only:
        candidates.append(VIDEOS)
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


def _uses_hard_subtitle(video: Path) -> bool:
    video_parent = video.parent.resolve()
    return any(
        directory.exists() and directory.resolve() == video_parent
        for directory in _low_video_directories()
    )


def _has_current_hard_subtitle(video: Path) -> bool:
    subtitle = _subtitle_path(video)
    return (
        subtitle.exists()
        and video.exists()
        and video.stat().st_mtime_ns >= subtitle.stat().st_mtime_ns
    )


def _ffmpeg_filter_value(value: str) -> str:
    return value.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _burn_hard_subtitle(
    video: Path,
    subtitle: Path,
    output_video: Path,
    force: bool,
    mark_audio_enhanced: bool = False,
) -> Path:
    ffmpeg = os.getenv("FFMPEG_EXE", "ffmpeg")
    temporary_output = output_video.with_name(
        f".{output_video.stem}.hardsub.tmp{output_video.suffix}"
    )
    subtitle_name = _ffmpeg_filter_value(subtitle.name)
    subtitle_filter = (
        f"subtitles=filename='{subtitle_name}':"
        "force_style='FontName=Microsoft JhengHei,FontSize=18,"
        "Outline=2,Shadow=1,MarginV=28,Alignment=2'"
    )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        video.name,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-vf",
        subtitle_filter,
        "-c:v",
        "libx264",
        "-preset",
        os.getenv("HARDSUB_PRESET", "veryfast"),
        "-crf",
        os.getenv("HARDSUB_CRF", "20"),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        temporary_output.name,
    ]
    if mark_audio_enhanced:
        command[-1:-1] = ["-metadata", f"comment={ENHANCE_MARKER}"]
    print("  3/3 ffmpeg 繁中硬字幕燒錄", flush=True)
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
        raise RuntimeError(f"ffmpeg 硬字幕燒錄失敗：{details[-1000:]}")
    temporary_output.replace(output_video)
    print(f"完成硬字幕並覆蓋原始影片：{output_video}", flush=True)
    return output_video


def _embed_soft_subtitle(
    video: Path,
    subtitle: Path,
    output_video: Path,
    force: bool,
    mark_audio_enhanced: bool = False,
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
    if mark_audio_enhanced:
        command[-1:-1] = ["-metadata", f"comment={ENHANCE_MARKER}"]
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
    media_input: Path | None = None,
    audio_enhanced: bool = False,
) -> Path:
    output_srt = _subtitle_path(video)
    output_video = video
    media_input = video if media_input is None else media_input
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
        cues, language = backend.transcribe(media_input)
        if not cues:
            raise RuntimeError("ASR 沒有產生有效字幕段落。")
        print(f"  語言：{language}；字幕段落：{len(cues)}", flush=True)
        print("  2/3 OpenRouter 翻譯", flush=True)
        translated = strip_speaker_labels(
            translate_cues(cues, api_key, model_name)
        )
        temporary_output = output_srt.with_name(output_srt.name + ".tmp")
        temporary_output.write_text(format_srt(translated), encoding="utf-8-sig")
        temporary_output.replace(output_srt)
        print(f"完成：{output_srt}", flush=True)

    if _uses_hard_subtitle(video):
        return _burn_hard_subtitle(
            media_input,
            output_srt,
            output_video,
            force,
            mark_audio_enhanced=audio_enhanced,
        )
    return _embed_soft_subtitle(
        media_input,
        output_srt,
        output_video,
        force,
        mark_audio_enhanced=audio_enhanced,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="重新產生 SRT，並重新製作字幕影片",
    )
    parser.add_argument("--limit", type=int, default=0, help="只處理前 N 部影片，0 表示全部")
    parser.add_argument(
        "--low-only",
        action="store_true",
        help="只處理 low_videos／LOW_VIDEO_DIR，不處理一般 videos",
    )
    parser.add_argument("--dry-run", action="store_true", help="只列出待處理影片，不呼叫模型/API")
    args = parser.parse_args()

    if args.low_only:
        os.environ.setdefault("MOSS_MAX_NEW_TOKENS", "1024")

    videos = _find_videos(low_only=args.low_only)
    if not videos:
        print("low_videos、low_video、videos 都沒有可處理的影片。")
        return 0 if args.dry_run else 1
    pending = [
        video
        for video in videos
        if (
            args.force
            or not _subtitle_path(video).exists()
            or (
                not _has_current_hard_subtitle(video)
                if _uses_hard_subtitle(video)
                else not _has_soft_subtitle(video)
            )
        )
    ]
    skipped = len(videos) - len(pending)
    if args.limit > 0:
        pending = pending[: args.limit]
    print(
        f"來源影片：{len(videos)} 部；略過已完成字幕影片：{skipped} 部；待處理：{len(pending)} 部",
        flush=True,
    )
    if args.dry_run:
        for video in pending:
            subtitle_mode = "硬字幕" if _uses_hard_subtitle(video) else "軟字幕"
            print(
                f"{video} -> {_subtitle_path(video)} + "
                f"{subtitle_mode}覆蓋原始影片"
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

    try:
        use_audio_enhance = auto_enhance_enabled()
    except ValueError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
    prepared_media = {}
    if use_audio_enhance:
        print(
            "字幕前音訊流程：中段三點分析 → pass／enhance "
            "（uncertain 自動 enhance）",
            flush=True,
        )
        try:
            prepared_media = prepare_audio_media(pending)
        except Exception as exc:
            print(f"錯誤：字幕前音訊處理失敗：{exc}", file=sys.stderr)
            return 2

    failures = 0
    try:
        backend = create_backend().load() if needs_asr else None
        if needs_asr:
            print("使用 ASR：MOSS-Transcribe-Diarize", flush=True)
        model_name = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        for video in pending:
            media = prepared_media.get(video)
            try:
                process_video(
                    video,
                    backend,
                    api_key,
                    model_name,
                    args.force,
                    media_input=media.media_input if media else video,
                    audio_enhanced=media.enhanced if media else False,
                )
            except Exception as exc:
                failures += 1
                print(f"失敗：{video.name}：{exc}", file=sys.stderr, flush=True)
            finally:
                if media:
                    media.cleanup()
    finally:
        for media in prepared_media.values():
            media.cleanup()
    print(f"批次完成：成功 {len(pending) - failures} 部，失敗 {failures} 部。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

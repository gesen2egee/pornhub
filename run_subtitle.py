"""使用 MOSS 產生、翻譯並內嵌字幕。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VIDEOS = ROOT / "videos"
SUBTITLE_TEMP = ROOT / "tasks" / "subtitle-temp"

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
    translate_cues,
)
import video_meta  # noqa: E402


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


def _read_video_meta(video: Path) -> dict:
    try:
        return video_meta.read_mp4_meta(video)
    except Exception:
        return {}


def _subtitle_path(video: Path) -> Path:
    return video.with_suffix(".srt")


def _has_embedded_subtitle_meta(video: Path) -> bool:
    """兩個字幕區段都存在才算完成；內容可為空。"""
    meta = _read_video_meta(video)
    return bool(
        meta.get("original_srt_present")
        and meta.get("translated_srt_present")
    )


def _subtitle_complete(video: Path) -> bool:
    """未來以影片 Meta 為主；舊同名 SRT 直接視為已完成。"""
    return _subtitle_path(video).exists() or _has_embedded_subtitle_meta(video)


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
    subtitle_name = _ffmpeg_filter_value(str(subtitle.resolve()))
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


def process_video(
    video: Path,
    backend,
    api_key: str | None,
    model_name: str,
    force: bool,
    media_input: Path | None = None,
    audio_enhanced: bool = False,
) -> Path:
    legacy_srt = _subtitle_path(video)
    output_video = video
    media_input = video if media_input is None else media_input
    existing_meta = _read_video_meta(video)
    original_srt: str | None = existing_meta.get("original_srt")
    translated_srt: str | None = None
    burn_srt: Path | None = None
    remove_burn_srt = False
    print(f"\n處理：{video.name}", flush=True)
    if existing_meta.get("translated_srt_present") and not force:
        print("  影片內已有字幕 Meta，直接略過", flush=True)
        return video
    if legacy_srt.exists() and not force:
        print(
            f"  1/3 使用舊同名 SRT：{legacy_srt}",
            flush=True,
        )
        translated_srt = legacy_srt.read_text(encoding="utf-8-sig")
        burn_srt = legacy_srt
    else:
        if backend is None or not api_key:
            raise RuntimeError("缺少 ASR backend 或 OpenRouter API key。")
        print(f"  1/3 {backend.display_name} 辨識", flush=True)
        cues, language = backend.transcribe(media_input)
        print(f"  語言：{language}；字幕段落：{len(cues)}", flush=True)
        original_srt = format_srt(cues)
        if cues:
            print("  2/3 OpenRouter 翻譯", flush=True)
            translated = translate_cues(cues, api_key, model_name)
            translated_srt = format_srt(translated)
            SUBTITLE_TEMP.mkdir(parents=True, exist_ok=True)
            burn_srt = (
                SUBTITLE_TEMP
                / f"{video.stem[:48]}-{abs(hash(video.resolve())):x}.srt"
            )
            burn_srt.write_text(translated_srt, encoding="utf-8-sig")
            remove_burn_srt = True
        else:
            translated_srt = ""
            print("  2/3 無字幕，將空字幕狀態寫入影片 Meta", flush=True)

    try:
        if burn_srt is not None and translated_srt and translated_srt.strip():
            result = _burn_hard_subtitle(
                media_input,
                burn_srt,
                output_video,
                force,
                mark_audio_enhanced=audio_enhanced,
            )
        else:
            result = output_video
        try:
            base_comment = existing_meta.get("raw_comment") or ""
            if audio_enhanced and ENHANCE_MARKER not in base_comment:
                base_comment = f"{ENHANCE_MARKER}\n{base_comment}".rstrip()
            video_meta.merge_write_mp4_meta(
                result,
                web_meta=existing_meta.get("web_meta"),
                original_srt=original_srt,
                translated_srt=translated_srt,
                base_comment=base_comment,
            )
            print("  [META] 已寫入 MOSS 原文與繁中字幕", flush=True)
        except Exception as exc:
            print(f"  [!] 寫入字幕 metadata 失敗：{exc}", flush=True)
    finally:
        if remove_burn_srt and burn_srt is not None:
            burn_srt.unlink(missing_ok=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="重新產生字幕 Meta，並重新製作硬字幕影片",
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
        if args.force or not _subtitle_complete(video)
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
            print(f"{video} -> 硬字幕覆蓋原始影片 + 影片內字幕 Meta")
        return 0
    if not pending:
        print("沒有需要處理的影片。")
        return 0

    needs_asr = bool(pending)
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

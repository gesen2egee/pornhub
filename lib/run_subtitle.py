"""使用 MOSS 產生、翻譯並輸出硬字幕或相容 SRT。"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
from pathlib import Path

from project_paths import (
    LIB_DIR,
    PREVIEW_VIDEOS_DIR,
    TASKS_DIR,
    VIDEOS_DIR,
)

ROOT = LIB_DIR
VIDEOS = VIDEOS_DIR
SUBTITLE_TEMP = TASKS_DIR / "subtitle-temp"

sys.path.insert(0, str(ROOT))
from asr_backends import create_backend, srt_time  # noqa: E402
from audio_enhance_stage import (  # noqa: E402
    ENHANCE_MARKER,
    auto_enhance_enabled,
    prepare_audio_media,
)
from translate_srt_openrouter import (  # noqa: E402
    DEFAULT_MODEL,
    SPEAKER_LABEL_PATTERN,
    format_srt,
    translate_cues,
)
import video_meta  # noqa: E402


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm"}
ASR_CHUNK_SECONDS = 7.5 * 60
MIN_ASR_CHUNK_SECONDS = 3 * 60


def _low_video_directories() -> list[Path]:
    directories: list[Path] = []
    configured_dir = os.getenv("LOW_VIDEO_DIR")
    if configured_dir:
        directories.append(Path(configured_dir))
    directories.append(PREVIEW_VIDEOS_DIR)
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
    """只有終態影片 Meta 才算完成；舊 SRT 必須先硬編碼遷移。"""
    meta = _read_video_meta(video)
    status = meta.get("subtitle_status") or {}
    if status.get("outcome") == "failed":
        return False
    return bool(
        meta.get("original_srt_present")
        and meta.get("translated_srt_present")
    )


def _uses_hard_subtitle(video: Path) -> bool:
    """LOW 目錄使用硬字幕；一般 videos 使用外掛 SRT。"""
    parent = video.parent.resolve()
    return any(parent == path.resolve() for path in _low_video_directories())


def _probe_media_duration(media: Path) -> float | None:
    ffprobe = os.getenv("FFPROBE_EXE", "ffprobe")
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(media),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        duration = float(result.stdout.strip())
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        return None
    return duration if duration > 0 else None


def _parse_srt_time(value: str) -> float:
    hours, minutes, remainder = value.strip().split(":")
    seconds, milliseconds = remainder.replace(".", ",").split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(milliseconds) / 1000
    )


def _offset_cues(
    cues: list[dict],
    offset_seconds: float,
    first_id: int,
) -> list[dict]:
    """把分段內相對時間換算成全片時間並重新連續編號。"""
    merged: list[dict] = []
    for index, cue in enumerate(cues, start=first_id):
        start_text, end_text = str(cue["time"]).split("-->", 1)
        start = _parse_srt_time(start_text) + offset_seconds
        end = _parse_srt_time(end_text) + offset_seconds
        item = dict(cue)
        item["id"] = index
        item["time"] = f"{srt_time(start)} --> {srt_time(end)}"
        merged.append(item)
    return merged


def _chunk_audio_path(media: Path, index: int) -> Path:
    SUBTITLE_TEMP.mkdir(parents=True, exist_ok=True)
    return SUBTITLE_TEMP / (
        f"{media.stem[:40]}-{abs(hash(media.resolve())):x}"
        f".asr-part-{index:03d}.wav"
    )


def _extract_audio_chunk(
    media: Path,
    output: Path,
    start: float,
    duration: float,
) -> None:
    ffmpeg = os.getenv("FFMPEG_EXE", "ffmpeg")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-i",
        str(media),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("找不到 ffmpeg，無法建立 ASR 分段。") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg 建立 7.5 分鐘 ASR 分段超時。") from exc
    if result.returncode != 0 or not output.exists():
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"FFmpeg 建立 ASR 分段失敗：{details[-1000:]}")


def _transcribe_with_chunks(media: Path, backend) -> tuple[list[dict], str]:
    """每 7.5 分鐘執行 MOSS；CUDA OOM 時自動二分後合併時間軸。"""
    duration = _probe_media_duration(media)
    if duration is None:
        try:
            return backend.transcribe(media)
        finally:
            release = getattr(backend, "release_transient_memory", None)
            if callable(release):
                release()

    def is_cuda_oom(exc: Exception) -> bool:
        message = str(exc).casefold()
        return "cuda out of memory" in message or (
            "cuda" in message and "allocate" in message
        )

    if duration <= ASR_CHUNK_SECONDS:
        try:
            try:
                return backend.transcribe(media)
            finally:
                release = getattr(
                    backend,
                    "release_transient_memory",
                    None,
                )
                if callable(release):
                    release()
        except Exception as exc:
            if (
                not is_cuda_oom(exc)
                or duration <= MIN_ASR_CHUNK_SECONDS
            ):
                raise
            print(
                f"  整段 ASR 發生 CUDA OOM，改用 "
                f"{duration / 120:.1f} 分鐘子段重試",
                flush=True,
            )
            safe_chunk_seconds = duration / 2
    else:
        safe_chunk_seconds = ASR_CHUNK_SECONDS

    part_count = math.ceil(duration / ASR_CHUNK_SECONDS)
    print(
        f"  MOSS 分段 ASR：片長 {duration / 60:.1f} 分鐘，"
        f"共 {part_count} 段，每段最多 7.5 分鐘",
        flush=True,
    )
    merged: list[dict] = []
    languages: list[str] = []
    chunk_sequence = 0

    def transcribe_range(start: float, part_duration: float) -> None:
        nonlocal chunk_sequence, safe_chunk_seconds
        if part_duration > safe_chunk_seconds + 0.001:
            first_duration = min(safe_chunk_seconds, part_duration)
            transcribe_range(start, first_duration)
            transcribe_range(
                start + first_duration,
                part_duration - first_duration,
            )
            return

        chunk_sequence += 1
        chunk = _chunk_audio_path(media, chunk_sequence)
        try:
            print(
                f"  ASR 子段：{srt_time(start)}–"
                f"{srt_time(start + part_duration)}",
                flush=True,
            )
            _extract_audio_chunk(
                media,
                chunk,
                start,
                part_duration,
            )
            try:
                cues, language = backend.transcribe(chunk)
            finally:
                release = getattr(
                    backend,
                    "release_transient_memory",
                    None,
                )
                if callable(release):
                    release()
        except Exception as exc:
            if (
                not is_cuda_oom(exc)
                or part_duration <= MIN_ASR_CHUNK_SECONDS
            ):
                raise
            safe_chunk_seconds = min(
                safe_chunk_seconds,
                part_duration / 2,
            )
            print(
                f"  [OOM 自適應] {part_duration / 60:.1f} 分鐘仍過大，"
                f"改用 {safe_chunk_seconds / 60:.1f} 分鐘子段",
                flush=True,
            )
            transcribe_range(start, part_duration)
            return
        finally:
            chunk.unlink(missing_ok=True)

        merged.extend(_offset_cues(cues, start, len(merged) + 1))
        if language and language not in languages:
            languages.append(language)

    for index in range(part_count):
        start = index * ASR_CHUNK_SECONDS
        part_duration = min(ASR_CHUNK_SECONDS, duration - start)
        print(
            f"  ASR 分段 {index + 1}/{part_count}："
            f"{srt_time(start)}–{srt_time(start + part_duration)}",
            flush=True,
        )
        transcribe_range(start, part_duration)
    print(
        f"  ASR 分段合併完成：{len(merged)} 段字幕，"
        "接著統一交給 LLM 校正與翻譯",
        flush=True,
    )
    return merged, ",".join(languages) or "multilingual"


def _ffmpeg_filter_value(value: str) -> str:
    return value.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _strip_speaker_labels_from_srt(content: str) -> str:
    """供畫面燒錄與播放器 SRT 使用；Meta 仍保存完整說話者標籤。"""
    return "".join(
        SPEAKER_LABEL_PATTERN.sub("", line)
        for line in content.splitlines(keepends=True)
    )


def _compatible_srt(content: str) -> str:
    """輸出 Windows／電視播放器普遍相容的 UTF-8 BOM + CRLF SRT 內容。"""
    normalized = _strip_speaker_labels_from_srt(content)
    return normalized.replace("\r\n", "\n").replace("\r", "\n").replace(
        "\n", "\r\n"
    )


def _write_compatible_srt(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_compatible_srt(content), encoding="utf-8-sig", newline="")
    print(f"  [SRT] 已輸出播放器相容字幕：{path}", flush=True)
    return path


def _write_burn_subtitle(video: Path, translated_srt: str) -> Path:
    SUBTITLE_TEMP.mkdir(parents=True, exist_ok=True)
    burn_srt = (
        SUBTITLE_TEMP
        / f"{video.stem[:48]}-{abs(hash(video.resolve())):x}.srt"
    )
    burn_srt.write_text(
        _strip_speaker_labels_from_srt(translated_srt),
        encoding="utf-8-sig",
    )
    return burn_srt


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
    hard_subtitle: bool = True,
    export_srt: bool = False,
) -> Path:
    legacy_srt = _subtitle_path(video)
    output_video = video
    media_input = video if media_input is None else media_input
    existing_meta = _read_video_meta(video)
    original_srt: str | None = existing_meta.get("original_srt")
    translated_srt: str | None = None
    subtitle_outcome = "translated"
    burn_srt: Path | None = None
    remove_burn_srt = False
    metadata_written = False
    print(f"\n處理：{video.name}", flush=True)
    existing_status = existing_meta.get("subtitle_status") or {}
    if (
        existing_meta.get("original_srt_present")
        and existing_meta.get("translated_srt_present")
        and existing_status.get("outcome") != "failed"
        and not force
    ):
        if export_srt and not legacy_srt.exists():
            existing_translation = existing_meta.get("translated_srt") or ""
            if existing_translation.strip():
                _write_compatible_srt(legacy_srt, existing_translation)
        print("  影片內已有字幕 Meta，直接略過", flush=True)
        return video
    if legacy_srt.exists() and not force:
        print(
            f"  1/3 使用舊同名 SRT：{legacy_srt}",
            flush=True,
        )
        translated_srt = legacy_srt.read_text(encoding="utf-8-sig")
        original_srt = ""
        subtitle_outcome = "legacy_srt"
    else:
        if backend is None or not api_key:
            raise RuntimeError("缺少 ASR backend 或 OpenRouter API key。")
        print(f"  1/3 {backend.display_name} 辨識", flush=True)
        cues, language = _transcribe_with_chunks(media_input, backend)
        print(f"  語言：{language}；字幕段落：{len(cues)}", flush=True)
        original_srt = format_srt(cues)
        if cues:
            print("  2/3 OpenRouter 翻譯", flush=True)
            try:
                translated = translate_cues(cues, api_key, model_name)
                translated_srt = format_srt(translated)
            except Exception as exc:
                translated_srt = ""
                subtitle_outcome = "translation_failed"
                print(
                    "  [!] 翻譯失敗，保留未硬編碼影片；"
                    f"翻譯 Meta 寫空並視為完成：{exc}",
                    flush=True,
                )
        else:
            translated_srt = ""
            subtitle_outcome = "empty"
            print("  2/3 無字幕，將空字幕狀態寫入影片 Meta", flush=True)

    if hard_subtitle and translated_srt and translated_srt.strip():
        burn_srt = _write_burn_subtitle(video, translated_srt)
        remove_burn_srt = True

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
            if media_input != output_video:
                os.replace(media_input, output_video)
                media_input = output_video
                print("  [音訊] 已套用增強音軌，影片畫面未重新編碼", flush=True)
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
                subtitle_status=video_meta.build_subtitle_status(
                    subtitle_outcome,
                    audio_enhanced=audio_enhanced,
                ),
                base_comment=base_comment,
            )
            metadata_written = True
            print("  [META] 已寫入 MOSS 原文與繁中字幕", flush=True)
        except Exception as exc:
            print(f"  [!] 寫入字幕 metadata 失敗：{exc}", flush=True)
        if export_srt:
            if translated_srt and translated_srt.strip():
                _write_compatible_srt(legacy_srt, translated_srt)
            else:
                legacy_srt.unlink(missing_ok=True)
    finally:
        if remove_burn_srt and burn_srt is not None:
            burn_srt.unlink(missing_ok=True)
    if legacy_srt.exists() and metadata_written and not export_srt:
        legacy_srt.unlink()
        print(f"  [遷移] 舊 SRT 已寫入影片 Meta 並移除：{legacy_srt}", flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="重新產生字幕 Meta，並依目錄重製硬字幕或外掛 SRT",
    )
    parser.add_argument("--limit", type=int, default=0, help="只處理前 N 部影片，0 表示全部")
    parser.add_argument(
        "--low-only",
        action="store_true",
        help="只處理 output/02_preview_videos，不處理正式影片",
    )
    parser.add_argument("--dry-run", action="store_true", help="只列出待處理影片，不呼叫模型/API")
    args = parser.parse_args()

    if args.low_only:
        os.environ.setdefault("MOSS_MAX_NEW_TOKENS", "1024")

    videos = _find_videos(low_only=args.low_only)
    if not videos:
        print("output/02_preview_videos 與 output/03_videos 都沒有可處理的影片。")
        return 0 if args.dry_run else 1
    pending = [
        video
        for video in videos
        if (
            args.force
            or not _subtitle_complete(video)
            or (
                not _uses_hard_subtitle(video)
                and bool(
                    (_read_video_meta(video).get("translated_srt") or "").strip()
                )
                and not _subtitle_path(video).exists()
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
            output = (
                "繁中硬字幕 + 影片內字幕 Meta"
                if _uses_hard_subtitle(video)
                else "不重編碼影片 + 同名相容 SRT + 影片內字幕 Meta"
            )
            print(f"{video} -> {output}")
        return 0
    if not pending:
        print("沒有需要處理的影片。")
        return 0

    needs_asr = any(
        not _subtitle_path(video).exists()
        for video in pending
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
            hard_subtitle = _uses_hard_subtitle(video)
            try:
                process_video(
                    video,
                    backend,
                    api_key,
                    model_name,
                    args.force,
                    media_input=media.media_input if media else video,
                    audio_enhanced=media.enhanced if media else False,
                    hard_subtitle=hard_subtitle,
                    export_srt=not hard_subtitle,
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

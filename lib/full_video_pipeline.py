# -*- coding: utf-8 -*-
"""正式影片（03_videos）循序管線：低畫質代理 → ASR/翻譯 → 高畫質（可分段）→ 分段 enhance。

預覽影片（02_preview_videos）不走此模組，維持原下載＋背景字幕流程。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterator

import segment_cutter
from project_paths import (
    DOWNLOADED_DIR,
    LIB_DIR,
    MOSS_VENV_DIR,
    TEMP_DIR,
    VIDEOS_DIR,
    ensure_output_directories,
)

# 每個功能皆由 process_full_video_from_grid 的獨立參數控制。

ROOT = LIB_DIR
DEFAULT_MOSS_PYTHON = MOSS_VENV_DIR / "Scripts" / "python.exe"
PROXY_FORMAT = (
    "worstvideo[height<=240]+worstaudio/"
    "worst[height<=240]/"
    "worstvideo+worstaudio/worst"
)
# 正式片預設鎖 720P（可用 HIGH_VIDEO_HEIGHT / HIGH_VIDEO_FORMAT 覆寫）
HIGH_FORMAT = (
    "bestvideo[height<=720]+bestaudio/"
    "best[height<=720]/"
    "bestvideo*+bestaudio/best"
)
HIGH_FORMAT_SORT = ["res:720"]
# yt-dlp --concurrent-fragments（分片並行下載）
HIGH_CONCURRENT_FRAGMENTS = 8
DIALOGUE_TRIM_THRESHOLD = 30.0
SEGMENT_GAP = 1.5
DOWNLOAD_SOCKET_TIMEOUT = 30
DOWNLOAD_RETRIES = 3

# 由外部 benchmark 注入 PipelineMetrics；未注入則不計時
_ACTIVE_METRICS: Any | None = None


def set_pipeline_metrics(metrics: Any | None) -> None:
    global _ACTIVE_METRICS
    _ACTIVE_METRICS = metrics


@contextmanager
def _stage(name: str, **extra: Any) -> Iterator[None]:
    if _ACTIVE_METRICS is None:
        with nullcontext():
            yield
        return
    with _ACTIVE_METRICS.stage(name, **extra):
        yield


def _log(msg: str) -> None:
    print(msg, flush=True)


def get_video_url_from_image(jpg_path: str | Path) -> str | None:
    from PIL import Image
    import urllib.parse

    try:
        with Image.open(jpg_path) as img:
            url = img.getexif().get(0x010e)
        if not isinstance(url, str):
            return None
        url = url.strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return url
    except Exception:
        pass
    return None


def get_video_url_from_source(source_path: str | Path) -> str | None:
    """從九宮格 EXIF 或影片 Metadata 取得來源 URL。"""
    path = Path(source_path).resolve()
    if path.suffix.casefold() in {".jpg", ".jpeg", ".png"}:
        return get_video_url_from_image(path)
    if path.suffix.casefold() not in {
        ".mp4", ".mkv", ".mov", ".webm", ".m4v",
    }:
        return None
    try:
        import video_meta

        meta = video_meta.read_mp4_meta(path)
    except Exception:
        return None
    web = meta.get("web_meta") or {}
    for candidate in (
        web.get("webpage_url"),
        web.get("url"),
        meta.get("webpage_url"),
    ):
        if not isinstance(candidate, str):
            continue
        candidate = candidate.strip()
        if candidate.startswith(("http://", "https://")):
            return candidate
    return None


def probe_duration(path: Path) -> float | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        pass
    return None


def has_video_stream(path: Path) -> bool | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return result.returncode == 0 and "video" in result.stdout.split()


def is_within_1080p(path: Path) -> bool | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout or "{}")
        stream = (payload.get("streams") or [])[0]
        width = int(stream["width"])
        height = int(stream["height"])
    except Exception:
        return None
    return (width <= 1920 and height <= 1080) or (
        width <= 1080 and height <= 1920
    )


def _base_ydl_opts(
    out_path: Path,
    purpose: str,
    video_url: str,
) -> dict[str, Any]:
    import sites

    adapter = sites.get_adapter_for_url(video_url)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    opts: dict[str, Any] = {
        "paths": {
            "home": str(out_path.parent),
            "temp": str(TEMP_DIR),
        },
        "outtmpl": {"default": out_path.name},
        "quiet": False,
        "no_warnings": True,
        "socket_timeout": DOWNLOAD_SOCKET_TIMEOUT,
        "retries": DOWNLOAD_RETRIES,
        "fragment_retries": DOWNLOAD_RETRIES,
        "extractor_retries": DOWNLOAD_RETRIES,
        "file_access_retries": DOWNLOAD_RETRIES,
        "overwrites": True,
    }
    opts.update(adapter.ydl_opts(purpose))
    return opts


def download_proxy_low(video_url: str, out_path: Path) -> Path:
    """下載最低畫質／≤240P 全片代理檔，供 ASR 使用。"""
    import yt_dlp

    if out_path.exists():
        out_path.unlink()
    opts = _base_ydl_opts(out_path, "download_low", video_url)
    opts["format"] = PROXY_FORMAT
    _log(f"  [1/5] 下載低畫質代理 → {out_path.name}")
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([video_url])
    if not out_path.exists() or has_video_stream(out_path) is not True:
        raise RuntimeError(f"低畫質代理下載失敗：{out_path}")
    dur = probe_duration(out_path)
    _log(f"  [OK] 代理完成，時長 {dur:.1f}s" if dur else "  [OK] 代理完成")
    return out_path


def _high_format_opts() -> tuple[str, list[str], int]:
    """回傳 (format, format_sort, concurrent_fragments)。"""
    fmt = os.getenv("HIGH_VIDEO_FORMAT", HIGH_FORMAT).strip() or HIGH_FORMAT
    try:
        height = int(os.getenv("HIGH_VIDEO_HEIGHT", "720"))
    except ValueError:
        height = 720
    if os.getenv("HIGH_VIDEO_FORMAT") is None:
        fmt = (
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/"
            f"bestvideo*+bestaudio/best"
        )
    sort = [f"res:{height}"]
    try:
        concurrent = int(
            os.getenv(
                "YTDLP_CONCURRENT_FRAGMENTS",
                str(HIGH_CONCURRENT_FRAGMENTS),
            )
        )
    except ValueError:
        concurrent = HIGH_CONCURRENT_FRAGMENTS
    return fmt, sort, max(1, concurrent)


def download_high_full(video_url: str, out_path: Path) -> Path:
    import yt_dlp

    if out_path.exists():
        out_path.unlink()
    fmt, fsort, concurrent = _high_format_opts()
    opts = _base_ydl_opts(out_path, "download_full", video_url)
    opts["format"] = fmt
    opts["format_sort"] = fsort
    opts["concurrent_fragment_downloads"] = concurrent
    opts["writethumbnail"] = True
    opts["postprocessors"] = [
        {"key": "EmbedThumbnail", "already_have_thumbnail": False}
    ]
    _log(
        f"  [4/5] 下載全片高畫質 → {out_path.name}"
        f"（format_sort={fsort}, concurrent_fragments={concurrent}）"
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([video_url])
    if not out_path.exists() or has_video_stream(out_path) is not True:
        raise RuntimeError(f"高畫質全片下載失敗：{out_path}")
    if is_within_1080p(out_path) is False:
        out_path.unlink(missing_ok=True)
        raise RuntimeError("高畫質影片超過等效 1080P，已捨棄")
    return out_path


def download_high_range(
    video_url: str,
    out_path: Path,
    start: float,
    end: float,
) -> Path:
    """下載單一時間區間的高畫質片段。"""
    import yt_dlp

    if out_path.exists():
        out_path.unlink()
    start = max(0.0, float(start))
    end = max(start + 0.05, float(end))

    def _ranges(info_dict, ydl):
        yield {"start_time": start, "end_time": end}

    fmt, fsort, concurrent = _high_format_opts()
    opts = _base_ydl_opts(out_path, "download_full", video_url)
    opts["format"] = fmt
    opts["format_sort"] = fsort
    opts["concurrent_fragment_downloads"] = concurrent
    opts["download_ranges"] = _ranges
    opts["force_keyframes_at_cuts"] = True
    _log(
        f"    [dl] {out_path.name} {start:.2f}-{end:.2f}s "
        f"res≤{fsort[0]} cf={concurrent}"
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([video_url])
    if not out_path.exists() or has_video_stream(out_path) is not True:
        raise RuntimeError(
            f"高畫質分段下載失敗：{out_path.name} ({start:.2f}-{end:.2f}s)"
        )
    return out_path


def concat_videos(parts: list[Path], out_path: Path) -> Path:
    if not parts:
        raise RuntimeError("沒有可拼接的片段")
    if len(parts) == 1:
        if parts[0].resolve() != out_path.resolve():
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(parts[0]), str(out_path))
        return out_path

    list_file = out_path.with_suffix(".concat.txt")
    lines = []
    for part in parts:
        escaped = str(part.resolve()).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    list_file.unlink(missing_ok=True)
    if result.returncode != 0 or not out_path.exists():
        # copy 失敗時重編碼拼接
        filter_parts = []
        maps = []
        cmd = ["ffmpeg", "-y"]
        for part in parts:
            cmd.extend(["-i", str(part)])
        n = len(parts)
        for i in range(n):
            filter_parts.append(
                f"[{i}:v]setpts=PTS-STARTPTS[v{i}];"
                f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]"
            )
            maps.append(f"[v{i}][a{i}]")
        filter_complex = (
            ";".join(filter_parts)
            + f";{''.join(maps)}concat=n={n}:v=1:a=1[outv][outa]"
        )
        cmd.extend(
            [
                "-filter_complex",
                filter_complex,
                "-map",
                "[outv]",
                "-map",
                "[outa]",
                "-c:v",
                "libx264",
                "-crf",
                "18",
                "-preset",
                "fast",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(out_path),
            ]
        )
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=7200
        )
        if result.returncode != 0 or not out_path.exists():
            raise RuntimeError(
                f"片段拼接失敗：{(result.stderr or '')[-800:]}"
            )
    return out_path


def cues_to_entries(cues: list[dict[str, Any]]) -> list[dict]:
    entries = []
    for cue in cues:
        t_str = cue["time"]
        s_str, e_str = t_str.split(" --> ")
        entries.append(
            {
                "start": segment_cutter.parse_srt_time(s_str),
                "end": segment_cutter.parse_srt_time(e_str),
                "text": cue["text"],
            }
        )
    return entries


def entries_to_cues(entries: list[dict]) -> list[dict[str, Any]]:
    cues = []
    for idx, entry in enumerate(entries, 1):
        cues.append(
            {
                "id": idx,
                "time": (
                    f"{segment_cutter.format_srt_time(entry['start'])} --> "
                    f"{segment_cutter.format_srt_time(entry['end'])}"
                ),
                "text": entry["text"],
            }
        )
    return cues


def srt_text_to_entries(srt_text: str) -> list[dict]:
    temp = TEMP_DIR / "_parse_tmp.srt"
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_text(srt_text, encoding="utf-8")
    try:
        return segment_cutter.load_srt_entries(temp)
    finally:
        temp.unlink(missing_ok=True)


def complete_cached_translation(
    asr: dict[str, Any],
    cache_path: Path | None = None,
) -> dict[str, Any]:
    """翻譯既有 ASR；開關為 ON 卻缺 key/時間軸時明確失敗。"""
    if (asr.get("translated_srt") or "").strip():
        return asr
    if not (asr.get("original_srt") or asr.get("cues")):
        return asr
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
    if not api_key:
        raise RuntimeError("翻譯已開啟，但找不到 OPENROUTER_API_KEY")
    from translate_srt_openrouter import DEFAULT_MODEL, format_srt, translate_cues

    cues = asr.get("cues") or []
    if not cues and asr.get("original_srt"):
        cues = entries_to_cues(srt_text_to_entries(asr["original_srt"]))
    if not cues:
        raise RuntimeError("翻譯已開啟，但 ASR 快取沒有可翻譯的 cues")
    model_name = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
    try:
        translated = translate_cues(cues, api_key, model_name)
    except Exception as exc:
        raise RuntimeError(f"翻譯已開啟，但 OpenRouter 翻譯失敗：{exc}") from exc
    asr["translated_srt"] = format_srt(translated)
    asr["outcome"] = "translated"
    if cache_path is not None:
        cache_path.write_text(
            json.dumps(asr, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return asr


def run_asr_translate_local(proxy_path: Path) -> dict[str, Any]:
    """在目前 Python 環境執行 ASR + 翻譯（原始時間軸，不 retime）。"""
    import run_subtitle
    from asr_backends import create_backend
    from translate_srt_openrouter import (
        DEFAULT_MODEL,
        format_srt,
        translate_cues,
    )

    translation_enabled = os.getenv(
        "ENABLE_TRANSLATION", "1"
    ).strip().casefold() not in {"0", "false", "no", "off"}
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
    if translation_enabled and not api_key:
        raise RuntimeError("找不到 OPENROUTER_API_KEY")
    model_name = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
    os.environ.setdefault("MOSS_MAX_NEW_TOKENS", "4096")

    backend = create_backend().load()
    _log(f"  [2/5] {backend.display_name} 辨識（代理音訊）")
    cues, language = run_subtitle._transcribe_with_chunks(proxy_path, backend)
    _log(f"  語言：{language}；字幕段落：{len(cues)}")
    original_srt = format_srt(cues) if cues else ""
    translated_srt = ""
    outcome = "empty"
    if cues and translation_enabled:
        _log("  [3/5] OpenRouter 翻譯")
        try:
            translated = translate_cues(cues, api_key, model_name)
            translated_srt = format_srt(translated)
            outcome = "translated"
        except Exception as exc:
            _log(f"  [!] 翻譯失敗，保留原文：{exc}")
            outcome = "translation_failed"
    elif cues:
        _log("  [3/5] OpenRouter 翻譯已由開關停用")
        outcome = "transcribed"
    return {
        "language": language,
        "original_srt": original_srt,
        "translated_srt": translated_srt,
        "outcome": outcome,
        "cues": cues,
    }


def run_asr_translate(proxy_path: Path, work_dir: Path) -> dict[str, Any]:
    """依 ASR_BACKEND 選擇本機直接跑或 MOSS 子程序。"""
    from asr_backends import selected_asr_backend_name

    backend_name = selected_asr_backend_name()
    current = Path(sys.executable).resolve()

    # 雲端 STT／Whisper：在目前 venv 直接跑（不必進 moss 子程序）
    if backend_name in {"voxtral", "grok-stt", "whisper"}:
        label = {
            "voxtral": "OpenRouter Voxtral",
            "grok-stt": "OpenRouter Grok STT",
            "whisper": "faster-whisper",
        }.get(backend_name, backend_name)
        _log(f"  [2/5] 使用 {label} 轉寫（本程序）…")
        return run_asr_translate_local(proxy_path)

    # MOSS：需在 moss venv
    if "moss" in str(current).casefold() and current.exists():
        return run_asr_translate_local(proxy_path)

    moss_python = Path(os.getenv("MOSS_PYTHON", str(DEFAULT_MOSS_PYTHON)))
    if not moss_python.is_file():
        raise RuntimeError(
            f"找不到 MOSS 環境：{moss_python}。請先執行 00_setup_or_update.bat。"
        )

    result_path = work_dir / "asr_result.json"
    result_path.unlink(missing_ok=True)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env.setdefault("ASR_BACKEND", "moss")
    cmd = [
        str(moss_python),
        str(Path(__file__).resolve()),
        "--asr-only",
        str(proxy_path),
        "--result",
        str(result_path),
    ]
    _log("  [2/5] 啟動 MOSS 子程序做 ASR + 翻譯…")
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, check=False)
    if proc.returncode != 0 or not result_path.is_file():
        raise RuntimeError(
            f"MOSS ASR 子程序失敗，ExitCode={proc.returncode}"
        )
    return json.loads(result_path.read_text(encoding="utf-8"))


def enhance_parts(
    parts: list[Path],
    decisions: list[dict],
) -> tuple[list[Path], bool]:
    """依分段決策增強音訊；回傳（最終片段列表, 是否有任一增強）。"""
    from audio_enhance_stage import (
        DEFAULT_STAGE_PYTHON,
        _load_enhancer,
    )

    any_enhanced = False
    out_parts: list[Path] = []
    need_indices = {
        int(d["segment_index"])
        for d in decisions
        if d.get("should_enhance")
    }
    if not need_indices:
        return parts, False

    # 在 moss venv 內載入 enhancer 較穩
    moss_python = Path(os.getenv("AUDIO_STAGE_PYTHON", str(DEFAULT_STAGE_PYTHON)))
    current = Path(sys.executable).resolve()
    use_local = "moss" in str(current).casefold()

    enhancer = None
    settings = None
    if use_local:
        enhancer = _load_enhancer()
        settings = enhancer.Settings(
            device=os.getenv("ASMR_ENHANCER_DEVICE", "auto")
        )

    for idx, part in enumerate(parts):
        if idx not in need_indices:
            out_parts.append(part)
            continue
        enhanced_path = part.with_name(f"{part.stem}.enhanced{part.suffix}")
        _log(f"  [Enhance] 分段 {idx + 1}/{len(parts)}：{part.name}")
        try:
            if use_local and enhancer is not None:
                enhancer.process_file(str(part), str(enhanced_path), settings)
            else:
                # 子程序：只增強單一檔
                script = f"""
import os, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from audio_enhance_stage import _load_enhancer
enh = _load_enhancer()
settings = enh.Settings(device=os.getenv("ASMR_ENHANCER_DEVICE", "auto"))
enh.process_file({str(part)!r}, {str(enhanced_path)!r}, settings)
"""
                proc = subprocess.run(
                    [str(moss_python), "-c", script],
                    cwd=str(ROOT),
                    check=False,
                )
                if proc.returncode != 0 or not enhanced_path.exists():
                    raise RuntimeError(f"enhance exit={proc.returncode}")
            if enhanced_path.exists():
                part.unlink(missing_ok=True)
                out_parts.append(enhanced_path)
                any_enhanced = True
            else:
                out_parts.append(part)
        except Exception as exc:
            _log(f"  [!] 分段 {idx + 1} 增強失敗，保留原音：{exc}")
            enhanced_path.unlink(missing_ok=True)
            out_parts.append(part)
    return out_parts, any_enhanced


def enhance_full_video(video: Path) -> tuple[Path, bool]:
    """整片走既有 prepare_audio_media。"""
    from audio_enhance_stage import auto_enhance_enabled, prepare_audio_media

    if not auto_enhance_enabled():
        return video, False
    prepared = prepare_audio_media([video])
    media = prepared.get(video)
    if not media:
        return video, False
    if media.enhanced and media.media_input != video:
        final = video.with_name(f"{video.stem}.enhanced{video.suffix}")
        if media.media_input.resolve() != final.resolve():
            shutil.move(str(media.media_input), str(final))
        video.unlink(missing_ok=True)
        shutil.move(str(final), str(video))
        return video, True
    return video, bool(media.enhanced)


def write_compatible_srt(path: Path, content: str) -> None:
    """寫入播放器相容 SRT（UTF-8 BOM + CRLF）。

    必須 newline=\"\"，否則 Windows 會把字串裡的 \\r\\n 再轉一次成 \\r\\r\\n，
    播放器／編輯器會看到大量空行。
    """
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    # 移除 [Sxx]；壓掉多餘空行（最多 cue 之間一個空行）
    cleaned: list[str] = []
    blank_run = 0
    for line in text.split("\n"):
        line = re.sub(r"\[S\d+\]\s*", "", line).rstrip()
        if not line.strip():
            blank_run += 1
            if blank_run <= 1 and cleaned:
                cleaned.append("")
            continue
        blank_run = 0
        cleaned.append(line)
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    body = "\r\n".join(cleaned)
    if body:
        body += "\r\n"
    path.write_text("\ufeff" + body, encoding="utf-8", newline="")


def archive_grid(grid: Path, archive_dir: Path) -> Path | None:
    if not grid.exists():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_dir / grid.name
    if destination.exists():
        destination = archive_dir / f"{grid.stem}-{grid.parent.name}{grid.suffix}"
    counter = 2
    while destination.exists():
        destination = archive_dir / (
            f"{grid.stem}-{grid.parent.name}-{counter}{grid.suffix}"
        )
        counter += 1
    shutil.move(str(grid), str(destination))
    _log(f"  [歸檔] 九宮格 → {destination.name}")
    return destination


def process_full_video_from_grid(
    jpg_path: Path,
    final_dir: Path | None = None,
    archive_dir: Path | None = None,
    keep_proxy: bool = False,
    *,
    max_height: int | None = None,
    enable_enhance: bool | None = None,
    enable_asr: bool | None = None,
    export_subtitles: bool | None = None,
    enable_dialogue_trim: bool | None = None,
    enable_translation: bool | None = None,
    enable_metadata: bool | None = None,
    dialogue_trim_threshold: float = DIALOGUE_TRIM_THRESHOLD,
    segment_gap: float = SEGMENT_GAP,
    force: bool = False,
    work_bucket: str = "03_videos",
    archive_grid_on_done: bool = True,
) -> Path:
    """
    單支來源循序流程：從九宮格或影片取得 URL，低畫質代理 → ASR/翻譯 →
    高畫質 →（可）enhance → 發布。

    max_height：高畫質上限（預設讀 HIGH_VIDEO_HEIGHT，否則 720）
    enable_enhance：是否允許音訊增強（預設讀 AUDIO_AUTO_ENHANCE）
    enable_dialogue_trim：是否依停頓門檻移除長停頓並分段下載
    """
    ensure_output_directories()
    jpg_path = Path(jpg_path).resolve()
    source_suffix = jpg_path.suffix.casefold()
    source_is_grid = source_suffix in {".jpg", ".jpeg", ".png"}
    pipeline_stage = "chosen" if work_bucket == "05_chosen" else "video"
    final_dir = Path(final_dir or VIDEOS_DIR).resolve()
    archive_dir = Path(archive_dir or DOWNLOADED_DIR).resolve()
    final_dir.mkdir(parents=True, exist_ok=True)

    # 解析模式參數（環境變數可當預設）
    if max_height is None:
        try:
            max_height = int(os.getenv("HIGH_VIDEO_HEIGHT", "720"))
        except ValueError:
            max_height = 720
    if enable_enhance is None:
        from audio_enhance_stage import auto_enhance_enabled

        enable_enhance = auto_enhance_enabled()
    if enable_asr is None:
        enable_asr = os.getenv(
            "ENABLE_ASR", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
    if export_subtitles is None:
        export_subtitles = os.getenv(
            "EXPORT_SUBTITLES", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
    if enable_dialogue_trim is None:
        enable_dialogue_trim = os.getenv(
            "ENABLE_DIALOGUE_TRIM", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
    if enable_translation is None:
        enable_translation = os.getenv(
            "ENABLE_TRANSLATION", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
    if enable_metadata is None:
        enable_metadata = os.getenv(
            "ENABLE_METADATA", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
    # 下載高度寫入 env，供 _high_format_opts 使用
    os.environ["HIGH_VIDEO_HEIGHT"] = str(max_height)
    os.environ["ENABLE_TRANSLATION"] = "1" if enable_translation else "0"
    os.environ["AUDIO_AUTO_ENHANCE"] = "1" if enable_enhance else "0"

    raw_stem = jpg_path.stem
    video_stem = re.sub(r"^\d{4}-", "", raw_stem)
    final_video = final_dir / f"{video_stem}.mp4"
    final_srt = final_dir / f"{video_stem}.srt"

    if (
        not force
        and final_video.exists()
        and has_video_stream(final_video) is True
    ):
        import video_meta

        meta = video_meta.read_mp4_meta(final_video)
        status = meta.get("subtitle_status") or {}
        source_meta = meta.get("web_meta") or {}
        same_stage_video = (
            source_suffix in {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
            and source_meta.get("pipeline_stage") == pipeline_stage
        )
        if (
            (source_is_grid or same_stage_video)
            and (
                (
                    not enable_asr
                    and not enable_enhance
                    and not enable_dialogue_trim
                )
                or (
                    meta.get("original_srt_present")
                    and meta.get("translated_srt_present")
                    and status.get("outcome") != "failed"
                    and (not export_subtitles or final_srt.exists())
                    and (not enable_enhance or status.get("audio_enhanced"))
                )
            )
        ):
            _log(f"[SKIP] 正式成品已存在：{final_video.name}")
            if archive_grid_on_done and source_is_grid and jpg_path.exists():
                archive_grid(jpg_path, archive_dir)
            return final_video

    video_url = get_video_url_from_source(jpg_path)
    if not video_url:
        raise RuntimeError(f"來源沒有可用 URL：{jpg_path.name}")

    work_dir = TEMP_DIR / "pipeline" / work_bucket / f"_work_{video_stem}"
    work_dir.mkdir(parents=True, exist_ok=True)

    proxy_path = work_dir / f"{video_stem}.proxy.mp4"
    high_path = work_dir / f"{video_stem}.high.mp4"
    asr_cache = work_dir / "asr_result.json"
    reuse_asr = os.getenv("REUSE_ASR_RESULT", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }
    # 清理舊分段／成品暫存，但保留可用的 proxy 以利續跑
    for stale in work_dir.glob(f"{video_stem}.seg*.mp4"):
        stale.unlink(missing_ok=True)
    for stale in work_dir.glob(f"{video_stem}.high*.mp4"):
        stale.unlink(missing_ok=True)
    for stale in work_dir.glob("*.enhanced.mp4"):
        stale.unlink(missing_ok=True)

    _log("=" * 60)
    _log(f"正式片循序管線：{video_stem}")
    _log(f"URL：{video_url}")
    _log("=" * 60)

    # 1) 只有真的需要重新 ASR 或做增強分析時才準備代理。
    needs_proxy = (
        (enable_asr and not (reuse_asr and asr_cache.is_file()))
        or enable_enhance
    )
    with _stage("01_proxy_download"):
        if not needs_proxy:
            _log("  [1/5] 代理下載已略過（ASR 快取可用或相關功能已關閉）")
            duration = 0.0
        elif proxy_path.exists() and has_video_stream(proxy_path) is True:
            _log(f"  [1/5] 重用既有代理：{proxy_path.name}")
            duration = probe_duration(proxy_path) or 0.0
        else:
            download_proxy_low(video_url, proxy_path)
            duration = probe_duration(proxy_path) or 0.0

    # 2–3) ASR + 翻譯（可重用 work_dir/asr_result.json 續跑）
    with _stage("02_asr_and_translate"):
        if reuse_asr and asr_cache.is_file():
            try:
                asr = json.loads(asr_cache.read_text(encoding="utf-8"))
                if not enable_translation:
                    asr["translated_srt"] = ""
                    asr["outcome"] = "transcribed"
                _log(
                    f"  [2/5] 重用既有字幕快取：{asr_cache.name}"
                    f"（cues={len(asr.get('cues') or [])}，"
                    f"outcome={asr.get('outcome')}）"
                )
                # 先前翻譯失敗時，有 API key 則只重跑翻譯
                if (
                    enable_translation
                    and
                    not (asr.get("translated_srt") or "").strip()
                    and (asr.get("original_srt") or asr.get("cues"))
                ):
                    _log("  [3/5] 補跑 OpenRouter 翻譯（沿用既有 ASR）")
                    with _stage("02b_translate_only"):
                        asr = complete_cached_translation(asr, asr_cache)
            except Exception as exc:
                if "翻譯已開啟" in str(exc):
                    raise
                if not enable_asr:
                    raise RuntimeError(
                        f"ASR 已關閉，但字幕快取無法讀取：{exc}"
                    ) from exc
                _log(f"  [!] 讀取字幕快取失敗，改重新 ASR：{exc}")
                if not proxy_path.exists() or has_video_stream(proxy_path) is not True:
                    download_proxy_low(video_url, proxy_path)
                asr = run_asr_translate(proxy_path, work_dir)
        elif not enable_asr:
            if enable_translation or enable_dialogue_trim:
                raise RuntimeError(
                    "ASR 已關閉且沒有可重用字幕快取；翻譯與對白剪片"
                    "無資料來源，請個別關閉或開啟 ASR。"
                )
            _log("  [2/5] ASR 已由開關停用")
            asr = {
                "language": None,
                "original_srt": "",
                "translated_srt": "",
                "outcome": "disabled",
                "cues": [],
            }
        else:
            asr = run_asr_translate(proxy_path, work_dir)
            try:
                asr_cache.write_text(
                    json.dumps(
                        {
                            "language": asr.get("language"),
                            "original_srt": asr.get("original_srt"),
                            "translated_srt": asr.get("translated_srt"),
                            "outcome": asr.get("outcome"),
                            "cues": asr.get("cues") or [],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except Exception:
                pass

    original_srt = asr.get("original_srt") or ""
    translated_srt = asr.get("translated_srt") or ""
    outcome = asr.get("outcome") or "empty"

    # 優先用譯文估對話；否則用原文
    srt_for_segments = translated_srt.strip() or original_srt
    entries = srt_text_to_entries(srt_for_segments) if srt_for_segments else []
    if not entries and asr.get("cues"):
        entries = cues_to_entries(asr["cues"])

    with _stage("03_segment_plan"):
        net_dur = segment_cutter.calculate_net_dialogue_duration(entries)
        _log(
            f"  [對話淨長度] {net_dur:.2f}s（門檻 {dialogue_trim_threshold}s）；"
            f"trim={'on' if enable_dialogue_trim else 'off'}；"
            f"enhance={'on' if enable_enhance else 'off'}；"
            f"max_height={max_height}"
        )

        any_enhanced = False
        segments: list[tuple[float, float]] | None = None
        segment_decisions: list[dict] = []

        if (
            enable_dialogue_trim
            and net_dur > dialogue_trim_threshold
            and entries
        ):
            segments = segment_cutter.build_continuous_segments(
                entries,
                max_gap=segment_gap,
                max_dur=duration if duration > 0 else 99999.0,
            )
            trimmed = sum(e - s for s, e in segments)
            _log(
                f"  [分段] 對話 > {dialogue_trim_threshold}s → "
                f"{len(segments)} 段連貫區間（約 {trimmed:.1f}s，gap={segment_gap}s）"
            )
            for i, (s, e) in enumerate(segments, 1):
                _log(f"    段 {i:02d}: {s:.2f} → {e:.2f} ({e - s:.2f}s)")

            if enable_enhance:
                from audio_enhance_stage import analyze_and_enhance_segments

                with _stage("03b_enhance_detect", segments=len(segments)):
                    segment_decisions = analyze_and_enhance_segments(
                        proxy_path, segments
                    )
                for d in segment_decisions:
                    tag = "ENHANCE" if d.get("should_enhance") else "PASS"
                    _log(
                        f"    偵測 段{d['segment_index'] + 1:02d}: "
                        f"[{tag}] {d.get('reason')}"
                    )
            else:
                segment_decisions = [
                    {
                        "segment_index": i,
                        "should_enhance": False,
                        "reason": "此模式關閉 enhance",
                    }
                    for i in range(len(segments))
                ]
        else:
            segments = None
            segment_decisions = []
            trimmed = duration

    if segments is not None:
        _log(f"  [4/5] 下載高畫質分段（共 {len(segments)}，≤{max_height}P）…")
        parts: list[Path] = []
        with _stage(
            "04_high_segment_download",
            segments=len(segments),
            planned_sec=round(trimmed, 2),
        ):
            for i, (s, e) in enumerate(segments):
                part = work_dir / f"{video_stem}.seg{i:03d}.mp4"
                _log(f"    下載段 {i + 1}/{len(segments)}: {s:.2f}-{e:.2f}s")
                download_high_range(video_url, part, s, e)
                parts.append(part)

        with _stage("05_segment_enhance", segments=len(parts)):
            if enable_enhance:
                parts, any_enhanced = enhance_parts(parts, segment_decisions)
            else:
                any_enhanced = False
        with _stage("06_concat"):
            concat_videos(parts, high_path)

        # 字幕重對位
        with _stage("07_retime_subtitles"):
            orig_entries = (
                srt_text_to_entries(original_srt)
                if original_srt.strip()
                else []
            )
            trans_entries = (
                srt_text_to_entries(translated_srt)
                if translated_srt.strip()
                else []
            )
            if orig_entries:
                original_srt = _entries_to_srt(
                    segment_cutter.retime_subtitles(orig_entries, segments)
                )
            if trans_entries:
                translated_srt = _entries_to_srt(
                    segment_cutter.retime_subtitles(trans_entries, segments)
                )
            _log("  [OK] 字幕已依切除後時間軸 retime")
    else:
        _log(
            f"  [全片] 下載完整 ≤{max_height}P"
            + (
                f"（對話 ≤ {dialogue_trim_threshold}s 或 trim 關閉）"
                if enable_dialogue_trim
                else "（全片模式，不裁切）"
            )
        )
        with _stage("04_high_full_download"):
            download_high_full(video_url, high_path)
        with _stage("05_full_enhance"):
            if enable_enhance:
                high_path, any_enhanced = enhance_full_video(high_path)
            else:
                any_enhanced = False
        trimmed = duration

    # 5) 發布 + Meta + SRT
    with _stage("08_publish_meta"):
        _log(f"  [5/5] 發布 → {final_video}")
        if final_video.exists():
            final_video.unlink()
        shutil.move(str(high_path), str(final_video))

        if export_subtitles and translated_srt.strip():
            write_compatible_srt(final_srt, translated_srt)
        elif export_subtitles and original_srt.strip():
            write_compatible_srt(final_srt, original_srt)
        elif not export_subtitles:
            final_srt.unlink(missing_ok=True)

        try:
            import sites
            import video_meta

            if enable_metadata:
                adapter = sites.get_adapter_for_url(video_url)
                resolved = sites.resolve_playable(
                    adapter, video_url, purpose="info", prefer_lowest=False
                )
                info = dict(resolved.get("info") or {})
                info.setdefault("webpage_url", video_url)
                web_meta = video_meta.build_web_meta(info)
                web_meta = dict(web_meta)
                web_meta["pipeline_stage"] = pipeline_stage
                if segments is not None:
                    web_meta["trimmed_segments"] = [
                        [round(s, 3), round(e, 3)] for s, e in segments
                    ]
                    web_meta["net_dialogue_seconds"] = round(net_dur, 3)
                base_comment = ""
                if any_enhanced:
                    from audio_enhance_stage import ENHANCE_MARKER

                    base_comment = ENHANCE_MARKER
                video_meta.merge_write_mp4_meta(
                    final_video,
                    web_meta=web_meta,
                    original_srt=original_srt,
                    translated_srt=translated_srt,
                    subtitle_status=video_meta.build_subtitle_status(
                        outcome,
                        audio_enhanced=any_enhanced,
                    ),
                    base_comment=base_comment or None,
                )
                if archive_grid_on_done and source_is_grid and jpg_path.exists():
                    video_meta.write_grid_jpg_web_meta(
                        str(jpg_path), web_meta, url=video_url
                    )
                _log("  [META] 已寫入 WEB_META + 字幕")
            else:
                _log("  [META] 已由開關停用")
        except Exception as exc:
            _log(f"  [!] Meta 寫入失敗（影片已發布）：{exc}")

        if archive_grid_on_done and source_is_grid and jpg_path.exists():
            archive_grid(jpg_path, archive_dir)

    if not keep_proxy:
        shutil.rmtree(work_dir, ignore_errors=True)

    _log(f"[DONE] {final_video}")
    return final_video


def _entries_to_srt(entries: list[dict]) -> str:
    lines: list[str] = []
    index = 0
    for entry in entries:
        text = str(entry.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
        text = "\n".join(
            part.strip() for part in text.split("\n") if part.strip()
        ).strip()
        if not text:
            continue
        index += 1
        lines.append(str(index))
        lines.append(
            f"{segment_cutter.format_srt_time(entry['start'])} --> "
            f"{segment_cutter.format_srt_time(entry['end'])}"
        )
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="正式影片循序下載管線（低畫質→字幕→高畫質）"
    )
    parser.add_argument(
        "--asr-only",
        type=Path,
        help="僅對代理影片跑 ASR+翻譯，寫入 --result JSON",
    )
    parser.add_argument("--result", type=Path, help="ASR 結果 JSON 路徑")
    parser.add_argument(
        "--grid",
        type=Path,
        help="九宮格 JPG（內嵌 URL），執行完整正式片管線",
    )
    parser.add_argument(
        "--final-dir",
        type=Path,
        default=None,
        help="正式影片輸出目錄（預設 output/03_videos）",
    )
    parser.add_argument(
        "--keep-proxy",
        action="store_true",
        help="保留 work_dir 代理與分段暫存",
    )
    args = parser.parse_args(argv)

    if args.asr_only:
        if not args.result:
            print("--asr-only 需要 --result", file=sys.stderr)
            return 2
        payload = run_asr_translate_local(args.asr_only.resolve())
        # cues 可能含不可 JSON 的型別；只保留必要欄位
        safe_cues = []
        for cue in payload.get("cues") or []:
            safe_cues.append(
                {
                    "id": cue.get("id"),
                    "time": cue.get("time"),
                    "text": cue.get("text"),
                }
            )
        payload["cues"] = safe_cues
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 0

    if args.grid:
        try:
            process_full_video_from_grid(
                args.grid.resolve(),
                final_dir=args.final_dir,
                keep_proxy=args.keep_proxy,
            )
        except Exception as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 1
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""正式影片（03_videos）管線：240P 分段下載與 Demucs/ASR 串流重疊，
完成後平行執行翻譯、高畫質切塊下載與分段 enhance。

預覽影片（02_preview_videos）不走此模組，維持原下載＋背景字幕流程。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext, redirect_stdout
from pathlib import Path
from queue import Empty, Queue
from threading import BoundedSemaphore, Lock, Thread
from typing import Any, Iterator

import segment_cutter
from project_paths import (
    DOWNLOADED_DIR,
    LIB_DIR,
    MOSS_VENV_DIR,
    TASKS_DIR,
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
# 停頓 ≥ 1.5s 剪掉中間空白
SEGMENT_GAP = 1.5
# 可選的前後延伸秒數；所有流程預設關閉，明確開啟才使用 0.75s
SEGMENT_EDGE_PADDING = 0.75
THREE_PHASE_AUDIO_CROSSFADE = 0.08


def edge_padding_enabled(environment: dict[str, str] | None = None) -> bool:
    """對白前後 0.75s 延伸；所有流程預設 OFF。"""
    environment = os.environ if environment is None else environment
    value = environment.get("ENABLE_EDGE_PADDING", "0").strip().casefold()
    return value not in {"0", "false", "no", "off"}


def resolve_edge_padding_seconds(
    enabled: bool | None = None,
    environment: dict[str, str] | None = None,
) -> float:
    """回傳實際 edge_padding 秒數：明確開啟=0.75，預設/關閉=0。"""
    if enabled is None:
        enabled = edge_padding_enabled(environment)
    return float(SEGMENT_EDGE_PADDING) if enabled else 0.0


def three_phase_selection_enabled(
    environment: dict[str, str] | None = None,
) -> bool:
    """30 秒三段模型精選開關；Shorts／Video／Chosen 由控制器預設開啟。"""
    environment = os.environ if environment is None else environment
    value = environment.get("ENABLE_THREE_PHASE_SELECTION", "1").strip().casefold()
    return value not in {"0", "false", "no", "off"}


DOWNLOAD_SOCKET_TIMEOUT = 30
DOWNLOAD_RETRIES = 3
ASR_PROXY_DOWNLOAD_ATTEMPTS = 3
ASR_PROXY_RETRY_DELAY_SECONDS = 3.0
SEGMENT_DOWNLOAD_ATTEMPTS = 2
SEGMENT_RECOVERY_ATTEMPTS = 1
# 高畫質切塊採五條分開請求；每條相隔一秒啟動，避免同時衝擊來源站點。
SEGMENT_DOWNLOAD_WORKERS = 5
SEGMENT_DOWNLOAD_START_INTERVAL_SECONDS = 1.0
# 先多抓來源關鍵影格之前的內容，再在本機精確重編碼。這讓成品每段的第一格
# 都是新關鍵影格，而不依賴遠端 Range 恰好落在來源的 I-frame。
HIGH_RANGE_KEYFRAME_PREROLL_SECONDS = 5.0
HIGH_RANGE_KEYFRAME_POSTROLL_SECONDS = 1.0
HIGH_RANGE_CONCURRENT_FRAGMENTS = 1
ASR_STREAM_CHUNK_SECONDS = 180.0
MOSS_CUE_MERGE_VERSION = 2
MOSS_DUPLICATE_CUE_GAP_SECONDS = 0.2

# 多支來源管線可同時進入高畫質階段；此鎖使五條限制套用到整個程序，
# 而非每支影片各自五條而意外放大。
_SEGMENT_DOWNLOAD_SEMAPHORES: dict[int, BoundedSemaphore] = {}
_SEGMENT_DOWNLOAD_SEMAPHORE_LOCK = Lock()
_SEGMENT_DOWNLOAD_START_LOCK = Lock()
_NEXT_SEGMENT_DOWNLOAD_START = 0.0
_ASR_PROXY_FAILURE_LOG_LOCK = Lock()
_PIPELINE_CHECKPOINT_LOCK = Lock()
# 下一支影片可先跑 240P、Demucs、MOSS、模型翻譯；只有進到高畫質下載時才等候。
_HIGH_QUALITY_DOWNLOAD_PHASE_LOCK = Lock()
# 即使未來建立多個 MOSS worker，也不允許不同影片同時佔用 MOSS 推理。
_MOSS_INFERENCE_LOCK = Lock()

# 由外部 benchmark 注入 PipelineMetrics；未注入則不計時
_ACTIVE_METRICS: Any | None = None

CHECKPOINT_FILE_NAMES = {
    "asr_source.json",
    "selection.json",
    "translation.json",
    "pipeline_state.json",
}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子替換 JSON，程序中斷時不會破壞上一版 checkpoint。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _update_pipeline_state(path: Path | None, **changes: Any) -> None:
    if path is None:
        return
    with _PIPELINE_CHECKPOINT_LOCK:
        state = _read_json_dict(path)
        state.setdefault("schema", "pipeline_state_v1")
        state.update(changes)
        state["updated_at_epoch"] = round(time.time(), 3)
        _atomic_write_json(path, state)


def _record_high_quality_segment(
    state_path: Path | None,
    *,
    index: int,
    start: float,
    end: float,
    path: Path,
) -> None:
    if state_path is None:
        return
    with _PIPELINE_CHECKPOINT_LOCK:
        state = _read_json_dict(state_path)
        state.setdefault("schema", "pipeline_state_v1")
        high_quality = dict(state.get("high_quality") or {})
        segments = dict(high_quality.get("segments") or {})
        segments[str(index)] = {
            "complete": True,
            "start": round(float(start), 3),
            "end": round(float(end), 3),
            "path": path.name,
        }
        high_quality["segments"] = segments
        high_quality["completed"] = sum(
            1 for item in segments.values() if item.get("complete") is True
        )
        state["high_quality"] = high_quality
        state["updated_at_epoch"] = round(time.time(), 3)
        _atomic_write_json(state_path, state)


def _prepare_high_quality_manifest(
    state_path: Path | None,
    segments_to_download: list[tuple[float, float]],
    video_stem: str,
) -> dict[str, Any]:
    if state_path is None:
        return {}
    with _PIPELINE_CHECKPOINT_LOCK:
        state = _read_json_dict(state_path)
        state.setdefault("schema", "pipeline_state_v1")
        high_quality = dict(state.get("high_quality") or {})
        high_quality["mode"] = "segments"
        high_quality["total"] = len(segments_to_download)
        previous = dict(high_quality.get("segments") or {})
        high_quality["segments"] = {
            str(index): item
            for index, (start, end) in enumerate(segments_to_download)
            if (
                (item := dict(previous.get(str(index)) or {})).get("complete")
                is True
                and abs(float(item.get("start") or -1) - start) < 0.01
                and abs(float(item.get("end") or -1) - end) < 0.01
                and item.get("path") == f"{video_stem}.seg{index:03d}.mp4"
            )
        }
        high_quality["completed"] = len(high_quality["segments"])
        state["high_quality"] = high_quality
        state["updated_at_epoch"] = round(time.time(), 3)
        _atomic_write_json(state_path, state)
        return high_quality


def _cleanup_work_media_preserving_checkpoints(work_dir: Path) -> None:
    """成功發布後只清大型暫存，保留四份可稽核 checkpoint。"""
    for child in sorted(work_dir.rglob("*"), reverse=True):
        if child.is_file():
            if child.parent == work_dir and child.name in CHECKPOINT_FILE_NAMES:
                continue
            try:
                child.unlink()
            except OSError:
                pass
        elif child.is_dir():
            try:
                child.rmdir()
            except OSError:
                pass


def _source_asr_payload(
    asr: dict[str, Any],
    duration: float | None = None,
) -> dict[str, Any]:
    """從舊混合快取抽出不可被精選／翻譯覆蓋的原始 ASR。"""
    from translate_srt_openrouter import format_srt

    cues = list(asr.get("source_cues") or asr.get("cues") or [])
    source_duration = float(
        duration if duration is not None else asr.get("source_duration") or 0.0
    )
    return {
        "schema": "asr_source_v1",
        "complete": True,
        "language": asr.get("language"),
        "original_srt": format_srt(cues) if cues else str(
            asr.get("original_srt") or ""
        ),
        "translated_srt": "",
        "outcome": "transcribed" if cues else "empty",
        "cues": cues,
        "source_duration": source_duration,
        "singing_ranges": list(asr.get("singing_ranges") or []),
        "moss_cue_merge_version": asr.get("moss_cue_merge_version"),
    }


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


class _SegmentProgress:
    """以單行顯示分段下載與 Enhance 的整體進度。"""

    def __init__(self, total: int, *, enhance_enabled: bool) -> None:
        self.total = max(1, int(total))
        self.enhance_enabled = enhance_enabled
        self.downloaded = 0
        self.enhanced = 0
        self.retries = 0
        self.pending_failures = 0
        self.enhance_failures = 0
        self._lock = Lock()
        self._last_line_length = 0

    def _render(self) -> None:
        work_total = self.total * (2 if self.enhance_enabled else 1)
        completed = self.downloaded + (
            self.enhanced if self.enhance_enabled else 0
        )
        ratio = min(1.0, completed / work_total)
        width = 28
        filled = round(width * ratio)
        bar = "█" * filled + "░" * (width - filled)
        details = f"下載 {self.downloaded}/{self.total}"
        if self.enhance_enabled:
            details += f"｜增強 {self.enhanced}/{self.total}"
        if self.retries:
            details += f"｜重試 {self.retries}"
        if self.pending_failures:
            details += f"｜待補 {self.pending_failures}"
        if self.enhance_failures:
            details += f"｜增強失敗 {self.enhance_failures}"
        line = f"  [{bar}] {ratio * 100:5.1f}%｜{details}"
        padding = " " * max(0, self._last_line_length - len(line))
        sys.stdout.write(f"\r{line}{padding}")
        sys.stdout.flush()
        self._last_line_length = len(line)

    def update(
        self,
        *,
        downloaded: int = 0,
        enhanced: int = 0,
        retries: int = 0,
        pending_failures: int = 0,
        enhance_failures: int = 0,
    ) -> None:
        with self._lock:
            self.downloaded += downloaded
            self.enhanced += enhanced
            self.retries += retries
            self.pending_failures += pending_failures
            self.enhance_failures += enhance_failures
            self.pending_failures = max(0, self.pending_failures)
            self._render()

    def finish(self) -> None:
        with self._lock:
            self._render()
            sys.stdout.write("\n")
            sys.stdout.flush()


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def segment_download_workers(
    environment: dict[str, str] | None = None,
) -> int:
    """回傳高畫質切塊的同時下載請求數（預設五條、最多五條）。"""
    environment = os.environ if environment is None else environment
    try:
        requested = int(
            environment.get(
                "HIGH_SEGMENT_DOWNLOAD_WORKERS",
                str(SEGMENT_DOWNLOAD_WORKERS),
            )
        )
    except (TypeError, ValueError):
        requested = SEGMENT_DOWNLOAD_WORKERS
    return min(SEGMENT_DOWNLOAD_WORKERS, max(1, requested))


def segment_download_start_interval_seconds(
    environment: dict[str, str] | None = None,
) -> float:
    """回傳相鄰高畫質請求的最小啟動間隔（預設一秒）。"""
    environment = os.environ if environment is None else environment
    try:
        seconds = float(
            environment.get(
                "HIGH_SEGMENT_DOWNLOAD_START_INTERVAL_SECONDS",
                str(SEGMENT_DOWNLOAD_START_INTERVAL_SECONDS),
            )
        )
    except (TypeError, ValueError):
        seconds = SEGMENT_DOWNLOAD_START_INTERVAL_SECONDS
    return max(0.0, seconds)


@contextmanager
def segment_download_slot(workers: int) -> Iterator[None]:
    """取得全管線共用的高畫質切塊下載槽位，並錯開請求啟動時間。"""
    global _NEXT_SEGMENT_DOWNLOAD_START
    with _SEGMENT_DOWNLOAD_SEMAPHORE_LOCK:
        semaphore = _SEGMENT_DOWNLOAD_SEMAPHORES.setdefault(
            workers,
            BoundedSemaphore(workers),
        )
    with semaphore:
        with _SEGMENT_DOWNLOAD_START_LOCK:
            now = time.monotonic()
            scheduled = max(now, _NEXT_SEGMENT_DOWNLOAD_START)
            _NEXT_SEGMENT_DOWNLOAD_START = (
                scheduled + segment_download_start_interval_seconds()
            )
        delay = scheduled - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        yield


@contextmanager
def high_quality_download_phase(video_stem: str) -> Iterator[None]:
    """全程序一次只允許一支影片下載高畫質；不阻擋其他影片的 240P/ASR/翻譯。"""
    _log(f"  [高畫質排程] {video_stem} 等待高畫質下載槽位")
    with _HIGH_QUALITY_DOWNLOAD_PHASE_LOCK:
        _log(f"  [高畫質排程] {video_stem} 取得槽位，開始高畫質下載")
        yield


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


def load_embedded_translation(
    source_path: str | Path,
    video_url: str,
) -> dict[str, Any] | None:
    """讀取影片內嵌翻譯字幕；空白紀錄不視為可重用時間軸。"""
    path = Path(source_path).resolve()
    if path.suffix.casefold() not in {
        ".mp4", ".mkv", ".mov", ".webm", ".m4v",
    }:
        return None
    import video_meta

    source_meta = video_meta.read_mp4_meta(path)
    translated = source_meta.get("translated_srt") or ""
    if not translated.strip():
        return None
    original = source_meta.get("original_srt") or ""
    web_meta = source_meta.get("web_meta") or {}
    raw_segments = (
        web_meta.get("preview_trimmed_segments")
        or web_meta.get("trimmed_segments")
        or []
    )
    source_segments: list[tuple[float, float]] = []
    try:
        source_segments = [
            (float(item[0]), float(item[1]))
            for item in raw_segments
            if isinstance(item, (list, tuple))
            and len(item) == 2
            and float(item[1]) > float(item[0])
        ]
    except (TypeError, ValueError):
        source_segments = []

    timeline_restored = False
    if source_segments:
        translated_entries = srt_text_to_entries(translated)
        trimmed_duration = sum(end - start for start, end in source_segments)
        latest_subtitle_end = max(
            (entry["end"] for entry in translated_entries),
            default=0.0,
        )
        # 只有字幕仍落在剪輯後總長內，才視為相對時間並反向映射。
        if translated_entries and latest_subtitle_end <= trimmed_duration + 1.0:
            translated = _entries_to_srt(
                segment_cutter.restore_subtitles_to_source_timeline(
                    translated_entries,
                    source_segments,
                )
            )
            if original.strip():
                original = _entries_to_srt(
                    segment_cutter.restore_subtitles_to_source_timeline(
                        srt_text_to_entries(original),
                        source_segments,
                    )
                )
            timeline_restored = True

    duration = float(web_meta.get("duration") or 0.0)
    if duration <= 0:
        duration = remote_duration(video_url) or 0.0
    return {
        "language": None,
        "original_srt": original,
        "translated_srt": translated,
        "outcome": "translated",
        "cues": [],
        "source_duration": duration,
        "embedded_translation_reused": True,
        "embedded_timeline_restored": timeline_restored,
    }


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
        # 所有下載階段使用本程式自己的 [1/5]、[dl] 進度；不要混入 yt-dlp
        # 的 DEBUG、格式選擇與百分比輸出。失敗仍會透過例外回報給各層流程。
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "verbose": False,
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


def download_proxy_range(
    video_url: str,
    out_path: Path,
    start: float,
    end: float,
) -> Path:
    """下載單一 ≤240P 區段；暫時串流失敗時清理後重試。"""
    import yt_dlp

    start = max(0.0, float(start))
    end = max(start + 0.05, float(end))
    attempts = _positive_int_env(
        "ASR_PROXY_DOWNLOAD_ATTEMPTS", ASR_PROXY_DOWNLOAD_ATTEMPTS
    )
    try:
        retry_delay = max(
            0.0,
            float(
                os.getenv(
                    "ASR_PROXY_RETRY_DELAY_SECONDS",
                    str(ASR_PROXY_RETRY_DELAY_SECONDS),
                )
            ),
        )
    except ValueError:
        retry_delay = ASR_PROXY_RETRY_DELAY_SECONDS

    def _ranges(_info_dict, _ydl):
        yield {"start_time": start, "end_time": end}

    for attempt in range(1, attempts + 1):
        for partial in (
            out_path,
            Path(f"{out_path}.part"),
            Path(f"{out_path}.ytdl"),
            TEMP_DIR / f"{out_path.name}.part",
            TEMP_DIR / f"{out_path.name}.ytdl",
        ):
            partial.unlink(missing_ok=True)
        opts = _base_ydl_opts(out_path, "download_low", video_url)
        opts["format"] = PROXY_FORMAT
        opts["download_ranges"] = _ranges
        opts["force_keyframes_at_cuts"] = True
        label = f"（第 {attempt}/{attempts} 次）" if attempts > 1 else ""
        _log(
            f"  [1/5] 下載 240P ASR 區段 {start:.0f}–{end:.0f}s{label}"
            f" → {out_path.name}"
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([video_url])
            if not out_path.exists() or has_video_stream(out_path) is not True:
                raise RuntimeError("下載後檔案不存在或沒有可用的影像流")
            return out_path
        except Exception as exc:
            _record_asr_proxy_failure(
                video_url=video_url,
                out_path=out_path,
                start=start,
                end=end,
                attempt=attempt,
                attempts=attempts,
                error=exc,
            )
            if attempt >= attempts:
                raise RuntimeError(
                    "240P ASR 區段下載失敗："
                    f"{out_path.name} ({start:.2f}-{end:.2f}s)，"
                    f"已重試 {attempts} 次；最後錯誤：{exc}"
                ) from exc
            _log(
                f"  [240P 重試] 第 {attempt}/{attempts} 次失敗：{exc}；"
                f"{retry_delay:.1f}s 後清理暫存重試"
            )
            if retry_delay:
                time.sleep(retry_delay)
    raise AssertionError("ASR 代理下載重試迴圈未正常結束")


def _record_asr_proxy_failure(
    *,
    video_url: str,
    out_path: Path,
    start: float,
    end: float,
    attempt: int,
    attempts: int,
    error: Exception,
) -> None:
    """把可重現 ASR 分段失敗的上下文寫入 tasks，供後續診斷。"""
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "video_url": video_url,
        "output": str(out_path),
        "range_seconds": [round(start, 3), round(end, 3)],
        "attempt": attempt,
        "attempts": attempts,
        "error_type": type(error).__name__,
        "error": str(error),
    }
    try:
        log_path = TASKS_DIR / "ffmpeg-errors" / "asr-proxy-failures.jsonl"
        with _ASR_PROXY_FAILURE_LOG_LOCK:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as log_exc:
        _log(f"  [!] 無法寫入 ASR 失敗紀錄：{log_exc}")


def remote_duration(video_url: str) -> float | None:
    """只取遠端 metadata 的時長，供 240P/ASR 串流分段使用。"""
    try:
        import sites

        adapter = sites.get_adapter_for_url(video_url)
        resolved = sites.resolve_playable(
            adapter, video_url, purpose="download_low", prefer_lowest=True
        )
        value = (resolved.get("info") or {}).get("duration")
        duration = float(value)
        return duration if duration > 0 else None
    except Exception:
        return None


def asr_stream_enabled(environment: dict[str, str] | None = None) -> bool:
    environment = os.environ if environment is None else environment
    # 預設先完整下載一次 240P；只有明確開啟時才使用分段串流。
    value = environment.get("ENABLE_ASR_STREAM", "0").strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("ENABLE_ASR_STREAM 必須是 ON/OFF 布林值。")


def asr_stream_chunk_seconds(environment: dict[str, str] | None = None) -> float:
    environment = os.environ if environment is None else environment
    try:
        seconds = float(
            environment.get("ASR_STREAM_CHUNK_SECONDS", ASR_STREAM_CHUNK_SECONDS)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("ASR_STREAM_CHUNK_SECONDS 必須是正數秒數。") from exc
    if seconds <= 0:
        raise ValueError("ASR_STREAM_CHUNK_SECONDS 必須大於 0。")
    return seconds


def _high_format_opts() -> tuple[str, list[str], int]:
    """回傳 (format, format_sort, concurrent_fragments)。"""
    unlimited = os.getenv("HIGH_VIDEO_UNLIMITED", "0").strip().casefold() in {
        "1", "true", "yes", "on",
    }
    fmt = os.getenv("HIGH_VIDEO_FORMAT", HIGH_FORMAT).strip() or HIGH_FORMAT
    try:
        height = int(os.getenv("HIGH_VIDEO_HEIGHT", "720"))
    except ValueError:
        height = 720
    if os.getenv("HIGH_VIDEO_FORMAT") is None and unlimited:
        fmt = "bestvideo*+bestaudio/best"
    elif os.getenv("HIGH_VIDEO_FORMAT") is None:
        fmt = (
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/"
            f"bestvideo*+bestaudio/best"
        )
    sort = ["res"] if unlimited else [f"res:{height}"]
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


def _high_range_format_opts() -> tuple[str, list[str], int]:
    """範圍下載優先選直接 HTTPS MP4，避免 HLS 必經 FFmpeg 時拒絕奇異分片名稱。"""
    fmt, sort, concurrent = _high_format_opts()
    # 使用者明確指定格式時不可覆寫；否則範圍下載先選能讓 FFmpeg 直接 seek 的
    # HTTPS 檔案，沒有才回退既有最佳格式（可能是 HLS）。
    if os.getenv("HIGH_VIDEO_FORMAT") is not None:
        return fmt, sort, concurrent
    unlimited = os.getenv("HIGH_VIDEO_UNLIMITED", "0").strip().casefold() in {
        "1", "true", "yes", "on",
    }
    try:
        height = int(os.getenv("HIGH_VIDEO_HEIGHT", "720"))
    except ValueError:
        height = 720
    direct = (
        "best[protocol=https]"
        if unlimited
        else f"best[protocol=https][height<={height}]/best[protocol=https]"
    )
    return f"{direct}/{fmt}", sort, concurrent


def download_high_full(video_url: str, out_path: Path) -> Path:
    import yt_dlp

    if out_path.exists():
        out_path.unlink()
    fmt, fsort, concurrent = _high_range_format_opts()
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


def _decodes_cleanly(path: Path) -> bool:
    """完整解碼影音流，抓出 ffprobe 看不出的中間壞格。"""
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v", "error",
                "-xerror",
                "-i", str(path),
                "-map", "0:v?", "-map", "0:a?",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=7200,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _reencode_keyframe_safe_range(
    source_path: Path,
    out_path: Path,
    *,
    local_start: float,
    duration: float,
) -> Path:
    """從前置緩衝素材精確切出片段，輸出第一影格固定為新的關鍵影格。"""
    out_path.unlink(missing_ok=True)
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source_path),
        # -ss 放在輸入後，會完整解碼到切點，避免快速 seek 落在中間影格。
        "-ss", f"{local_start:.3f}", "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-force_key_frames", "0", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-movflags", "+faststart",
        str(out_path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=7200,
    )
    if (
        result.returncode != 0
        or has_video_stream(out_path) is not True
        or not _decodes_cleanly(out_path)
    ):
        out_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"高畫質片段重編碼／完整解碼驗證失敗：{(result.stderr or '')[-800:]}"
        )
    return out_path


def download_high_range(
    video_url: str,
    out_path: Path,
    start: float,
    end: float,
) -> Path:
    """下載帶前置緩衝的高畫質範圍，再本機精確重編碼成關鍵影格安全片段。"""
    import yt_dlp

    start = max(0.0, float(start))
    end = max(start + 0.05, float(end))
    source_start = max(0.0, start - HIGH_RANGE_KEYFRAME_PREROLL_SECONDS)
    source_end = end + HIGH_RANGE_KEYFRAME_POSTROLL_SECONDS
    source_path = out_path.with_name(f"{out_path.stem}.source{out_path.suffix}")
    out_path.unlink(missing_ok=True)
    source_path.unlink(missing_ok=True)

    def _ranges(info_dict, ydl):
        yield {"start_time": source_start, "end_time": source_end}

    fmt, fsort, concurrent = _high_range_format_opts()
    # 五條範圍請求已提供網路平行度，避免每條再開多個 fragment 造成限流。
    concurrent = _positive_int_env(
        "HIGH_RANGE_CONCURRENT_FRAGMENTS",
        HIGH_RANGE_CONCURRENT_FRAGMENTS,
    )
    opts = _base_ydl_opts(source_path, "download_full", video_url)
    opts["format"] = fmt
    opts["format_sort"] = fsort
    opts["concurrent_fragment_downloads"] = concurrent
    opts["download_ranges"] = _ranges
    # 不讓 yt-dlp 再用 FFmpeg 強制 Range 切點：串流片段在此步驟常回報
    # AVERROR_INVALIDDATA。後續會以前置緩衝完整解碼，再本機精確重編碼，
    # 因此輸出首格仍是關鍵影格且可完整驗證。
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video_url])
        if (
            not source_path.exists()
            or has_video_stream(source_path) is not True
            or not _decodes_cleanly(source_path)
        ):
            raise RuntimeError(
                f"高畫質前置緩衝下載失敗：{out_path.name} "
                f"({source_start:.2f}-{source_end:.2f}s)"
            )
        source_duration = probe_duration(source_path)
        local_start = start - source_start
        requested_duration = end - start
        if source_duration is not None:
            requested_duration = min(
                requested_duration,
                source_duration - local_start,
            )
        if requested_duration < 0.05:
            raise RuntimeError(
                f"高畫質前置緩衝長度不足：{out_path.name} "
                f"({start:.2f}-{end:.2f}s)"
            )
        return _reencode_keyframe_safe_range(
            source_path,
            out_path,
            local_start=local_start,
            duration=requested_duration,
        )
    finally:
        source_path.unlink(missing_ok=True)


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


def concat_videos_audio_crossfade(
    parts: list[Path],
    out_path: Path,
    fade_seconds: float = THREE_PHASE_AUDIO_CROSSFADE,
) -> Path:
    """畫面直接切換，僅以 acrossfade 讓相鄰切塊音訊自然銜接。"""
    if len(parts) <= 1:
        return concat_videos(parts, out_path)
    durations = [probe_duration(part) or 0.0 for part in parts]
    if any(duration <= fade_seconds * 2 for duration in durations):
        _log("  [Crossfade] 有過短片段，改用一般串接以保留內容")
        return concat_videos(parts, out_path)
    filters: list[str] = []
    for index in range(len(parts)):
        filters.extend([
            f"[{index}:v]setpts=PTS-STARTPTS[v{index}]",
            f"[{index}:a]asetpts=PTS-STARTPTS[a{index}]",
        ])
    # concat 保留每段完整畫面時長；不使用 xfade，避免任何畫面混合或時間軸重疊。
    video_inputs = "".join(f"[v{index}]" for index in range(len(parts)))
    filters.append(f"{video_inputs}concat=n={len(parts)}:v=1:a=0[vout]")
    audio_label = "a0"
    for index in range(1, len(parts)):
        next_audio = f"ax{index}"
        filters.append(
            f"[{audio_label}][a{index}]acrossfade=d={fade_seconds:.3f}[{next_audio}]"
        )
        audio_label = next_audio
    # acrossfade 讓音訊時間軸縮短；在尾端補靜音，使其與硬切畫面等長。
    filters.append(
        f"[{audio_label}]apad=pad_dur={fade_seconds * (len(parts) - 1):.3f}[aout]"
    )
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"]
    for part in parts:
        command.extend(["-i", str(part)])
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-movflags", "+faststart", str(out_path),
    ])
    result = subprocess.run(command, capture_output=True, text=True, timeout=7200)
    if result.returncode != 0 or not out_path.exists() or has_video_stream(out_path) is not True:
        raise RuntimeError(f"音訊 crossfade 串接失敗：{(result.stderr or '')[-800:]}")
    _log(f"  [Audio crossfade] 畫面硬切；音訊淡化 {fade_seconds:.2f}s，{len(parts)} 段")
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


def _cue_interval(cue: dict[str, Any]) -> tuple[float, float] | None:
    try:
        start_text, end_text = str(cue["time"]).split("-->", 1)
        start = segment_cutter.parse_srt_time(start_text.strip())
        end = segment_cutter.parse_srt_time(end_text.strip())
    except (KeyError, ValueError):
        return None
    return (start, end) if end > start else None


def _normalise_cue_text(text: Any) -> str:
    """供重複句比對使用；保留原文字於實際輸出。"""
    without_speaker = re.sub(r"\[[^\]]+\]", "", str(text or ""))
    return re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]", "", without_speaker).casefold()


def _join_cue_texts(items: list[dict[str, Any]]) -> str:
    """依時間順序把多條字幕接為一條，符合精選／翻譯輸入格式。"""
    texts = [re.sub(r"\s+", " ", str(item["text"] or "")).strip() for item in items]
    return " ".join(text for text in texts if text)


def _make_merged_cue(items: list[dict[str, Any]], *, keep_first_text: bool = False) -> dict[str, Any]:
    first = items[0]
    start = min(float(item["start"]) for item in items)
    end = max(float(item["end"]) for item in items)
    cue = dict(first["cue"])
    cue["time"] = (
        f"{segment_cutter.format_srt_time(start)} --> "
        f"{segment_cutter.format_srt_time(end)}"
    )
    cue["text"] = str(first["text"] or "") if keep_first_text else _join_cue_texts(items)
    return {"cue": cue, "start": start, "end": end, "text": cue["text"]}


def merge_moss_cues(cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只合併連續重複的 MOSS 字幕；短句完整保留交給三段精選。"""
    parsed: list[dict[str, Any]] = []
    for cue in cues:
        interval = _cue_interval(cue)
        if interval is None:
            continue
        start, end = interval
        parsed.append({"cue": dict(cue), "start": start, "end": end, "text": str(cue.get("text") or "")})
    parsed.sort(key=lambda item: (item["start"], item["end"]))
    if not parsed:
        return []

    # 1. 同一句連續重複：保留第一份文字，時間擴展到最後一次。
    deduplicated: list[dict[str, Any]] = []
    duplicate_links = 0
    for item in parsed:
        if (
            deduplicated
            and _normalise_cue_text(item["text"])
            and _normalise_cue_text(item["text"]) == _normalise_cue_text(deduplicated[-1]["text"])
            and item["start"] - deduplicated[-1]["end"] <= MOSS_DUPLICATE_CUE_GAP_SECONDS
        ):
            deduplicated[-1] = _make_merged_cue([deduplicated[-1], item], keep_first_text=True)
            duplicate_links += 1
        else:
            deduplicated.append(item)

    result: list[dict[str, Any]] = []
    for index, item in enumerate(deduplicated, 1):
        cue = dict(item["cue"])
        cue["id"] = index
        result.append(cue)
    _log(
        f"  [MOSS 字幕合併] {len(cues)} → {len(result)}；"
        f"連續重複={duplicate_links}；短句完整保留供三段精選"
    )
    return result


def _net_dialogue_excluding_singing(
    entries: list[dict], singing_ranges: list[tuple[float, float]] | list[list[float]],
) -> float:
    """只計純對話：字幕與 AED 唱歌範圍的重疊時間不算 30 秒門檻。"""
    if not entries:
        return 0.0
    dialogue = sorted(
        (float(item["start"]), float(item["end"]))
        for item in entries if float(item["end"]) > float(item["start"])
    )
    merged: list[tuple[float, float]] = []
    for start, end in dialogue:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    raw_songs = sorted(
        (float(start), float(end)) for start, end in singing_ranges if float(end) > float(start)
    )
    songs: list[tuple[float, float]] = []
    for start, end in raw_songs:
        if songs and start <= songs[-1][1]:
            songs[-1] = (songs[-1][0], max(songs[-1][1], end))
        else:
            songs.append((start, end))
    total = 0.0
    for start, end in merged:
        overlap = sum(
            max(0.0, min(end, song_end) - max(start, song_start))
            for song_start, song_end in songs
            if song_start < end and song_end > start
        )
        total += max(0.0, end - start - overlap)
    return total


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


def selective_download_enabled(
    environment: dict[str, str] | None = None,
) -> bool:
    """精選下載：先劇情+選擇性翻譯，再以保留對白時長判斷剪片／下載區段。

    預設 ON（ENABLE_SELECTIVE_DOWNLOAD 未設或為 1）。
    """
    environment = os.environ if environment is None else environment
    value = environment.get("ENABLE_SELECTIVE_DOWNLOAD", "1").strip().casefold()
    return value not in {"0", "false", "no", "off"}


def _cues_for_translation(asr: dict[str, Any]) -> list[dict[str, Any]]:
    # 精選結果會把 cues 換成保留句；source_cues 保留完整 ASR，
    # 讓預算規則更新後可以重新從全片字幕計算，而不是拿舊精選重算。
    cues = asr.get("source_cues") or asr.get("cues") or []
    if not cues and asr.get("original_srt"):
        cues = entries_to_cues(srt_text_to_entries(asr["original_srt"]))
    return list(cues)


def _cues_fingerprint(cues: list[dict[str, Any]]) -> str:
    payload = [
        {
            "id": int(cue["id"]),
            "time": str(cue.get("time") or ""),
            "text": str(cue.get("text") or ""),
        }
        for cue in cues
    ]
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _filter_cues_by_ids(
    cues: list[dict[str, Any]],
    keep_ids: set[int],
) -> list[dict[str, Any]]:
    return [dict(c) for c in cues if int(c.get("id", -1)) in keep_ids]


def _translation_model_candidates(primary: str) -> list[str]:
    """回傳主要模型與至多一個失敗後備模型。"""
    models = [primary]
    fallback = os.getenv(
        "TRANSLATE_FALLBACK_MODEL", "x-ai/grok-4.5"
    ).strip()
    if fallback and fallback.casefold() != primary.casefold():
        models.append(fallback)
    return models


def _log_translation_fallback(primary: str, fallback: str, exc: Exception) -> None:
    _log(
        f"  [!] {primary} 翻譯／精選失敗，改用後備模型 {fallback}：{exc}"
    )


def complete_cached_translation(
    asr: dict[str, Any],
    cache_path: Path | None = None,
    *,
    selective: bool | None = None,
    translation_path: Path | None = None,
    state_path: Path | None = None,
    reasoning_effort: str | None = None,
    batch_size: int | None = None,
) -> dict[str, Any]:
    """翻譯既有 ASR；開關為 ON 卻缺 key/時間軸時明確失敗。

    selective=True 時走精選翻譯（劇情 + 可跳號）；歌詞由 LLM 自行改完整翻譯。
    """
    use_selective = (
        selective_download_enabled() if selective is None else bool(selective)
    )
    # 一般翻譯已完成可直接重用；精選需有 selective_kept_ids 才算完成
    if (asr.get("translated_srt") or "").strip():
        if not use_selective or asr.get("selective_kept_ids") is not None:
            return asr
        # 舊快取只有完整譯文、無精選標記 → 清掉後重跑精選
        asr = dict(asr)
        asr["translated_srt"] = ""
    if not (asr.get("original_srt") or asr.get("cues")):
        return asr
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
    if not api_key:
        raise RuntimeError("翻譯已開啟，但找不到 OPENROUTER_API_KEY")
    from translate_srt_openrouter import (
        DEFAULT_MODEL,
        format_srt,
        translate_cues,
        translate_cues_selective,
    )

    cues = _cues_for_translation(asr)
    if not cues:
        raise RuntimeError("翻譯已開啟，但 ASR 快取沒有可翻譯的 cues")
    primary_model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
    for attempt, model_name in enumerate(_translation_model_candidates(primary_model)):
        try:
            if use_selective:
                result = translate_cues_selective(cues, api_key, model_name)
                kept = result["kept_cues"]
                asr["source_cues"] = cues
                asr["translated_srt"] = format_srt(kept)
                asr["plot_summary"] = result.get("plot") or ""
                asr["selective_is_full"] = bool(result.get("is_full"))
                asr["selective_kept_ids"] = [int(c["id"]) for c in kept]
                asr["selective_dropped_ids"] = list(result.get("dropped_ids") or [])
                # 精選省略時原文也只留對應句，方便 retime／剪片對齊
                if not result.get("is_full"):
                    keep_ids = {int(c["id"]) for c in kept}
                    filtered = _filter_cues_by_ids(cues, keep_ids)
                    if filtered:
                        asr["cues"] = filtered
                        asr["original_srt"] = format_srt(filtered)
            else:
                translate_kwargs: dict[str, Any] = {
                    "checkpoint_path": translation_path,
                    "reasoning_effort": reasoning_effort,
                }
                if batch_size is not None:
                    translate_kwargs["batch_size"] = batch_size
                translated = translate_cues(
                    cues,
                    api_key,
                    model_name,
                    **translate_kwargs,
                )
                asr["translated_srt"] = format_srt(translated)
                asr["outcome"] = "translated"
            asr["translation_model"] = model_name
            asr["translation_fallback_used"] = attempt > 0
            asr["outcome"] = "translated"
            break
        except Exception as exc:
            models = _translation_model_candidates(primary_model)
            if attempt + 1 < len(models):
                _log_translation_fallback(primary_model, models[attempt + 1], exc)
            else:
                raise RuntimeError(
                    f"翻譯已開啟，但 OpenRouter 翻譯失敗：{exc}"
                ) from exc
    if cache_path is not None:
        _atomic_write_json(cache_path, asr)
    _update_pipeline_state(
        state_path,
        translation={
            "complete": True,
            "model": asr.get("translation_model"),
            "cue_count": len(asr.get("cues") or []),
        },
    )
    return asr


def complete_three_phase_translation(
    asr: dict[str, Any],
    cache_path: Path | None = None,
    *,
    selection_path: Path | None = None,
    translation_path: Path | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    """30 秒三段規劃、嚴格選句後，再將保留句翻成繁中。"""
    short_limit = _three_phase_short_cue_limit()
    if short_limit > 0:
        return _complete_three_phase_with_preserved_short_cues(
            asr, short_limit, cache_path=cache_path, selection_path=selection_path,
            translation_path=translation_path, state_path=state_path,
        )
    if (
        (asr.get("translated_srt") or "").strip()
        and asr.get("selection_mode") == "three_phase_30s"
        and asr.get("three_phase_budget_version") == 4
    ):
        return asr
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
    if not api_key:
        raise RuntimeError("三段精選已開啟，但找不到 OPENROUTER_API_KEY")
    from translate_srt_openrouter import (
        DEFAULT_MODEL,
        format_srt,
        select_cues_three_phase,
        three_phase_budget,
        translate_cues,
    )

    cues = _cues_for_translation(asr)
    if not cues:
        raise RuntimeError("三段精選需要帶時間軸的 ASR cues")
    selection_primary = os.getenv(
        "THREE_PHASE_SELECTION_MODEL", "z-ai/glm-5.2"
    ).strip() or "z-ai/glm-5.2"
    selection_models = [selection_primary]
    selection_fallback = os.getenv(
        "THREE_PHASE_SELECTION_FALLBACK_MODEL", "x-ai/grok-4.5"
    ).strip()
    if (
        selection_fallback
        and selection_fallback.casefold() != selection_primary.casefold()
    ):
        selection_models.append(selection_fallback)
    translation_primary = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
    translation_models = _translation_model_candidates(translation_primary)
    selection_reasoning = (
        os.getenv("THREE_PHASE_SELECTION_REASONING", "minimal").strip()
        or "minimal"
    )
    translation_reasoning = (
        os.getenv("THREE_PHASE_TRANSLATE_REASONING", "minimal").strip()
        or "minimal"
    )
    budget = three_phase_budget(cues)
    source_fingerprint = _cues_fingerprint(cues)

    # 完整字幕本來就少於整體 7.5N 上限時，直接翻譯全片，
    # 不浪費一次精選請求，也不做不必要的刪句。
    if len(cues) < int(budget["total"]):
        _log(
            f"  [三段精選] 完整字幕 {len(cues)} 句 < 總上限 {budget['total']} 句，"
            "改為全片翻譯"
        )
        all_ids = [int(cue["id"]) for cue in cues]
        if selection_path is not None:
            _atomic_write_json(
                selection_path,
                {
                    "schema": "selection_v1",
                    "complete": True,
                    "source_fingerprint": source_fingerprint,
                    "mode": "three_phase_30s",
                    "budget_version": 4,
                    "model": None,
                    "is_full": True,
                    "kept_ids": all_ids,
                    "dropped_ids": [],
                    "plot": "",
                    "phases": {},
                    "budget": budget,
                },
            )
        _update_pipeline_state(
            state_path,
            selection={"complete": True, "kept": len(all_ids), "is_full": True},
        )
        translated = None
        used_model = translation_primary
        fallback_used = False
        for attempt, model_name in enumerate(translation_models):
            try:
                translated = translate_cues(
                    cues,
                    api_key,
                    model_name,
                    batch_size=0,
                    checkpoint_path=translation_path,
                    reasoning_effort=translation_reasoning,
                )
                used_model = model_name
                fallback_used = attempt > 0
                break
            except Exception as exc:
                if attempt + 1 < len(translation_models):
                    _log_translation_fallback(
                        translation_primary, translation_models[attempt + 1], exc
                    )
                else:
                    raise RuntimeError(f"全片翻譯失敗：{exc}") from exc
        asr = dict(asr)
        asr["cues"] = cues
        asr["source_cues"] = cues
        asr["original_srt"] = format_srt(cues)
        asr["translated_srt"] = format_srt(translated)
        asr["plot_summary"] = ""
        asr["three_phase_design"] = ""
        asr["three_phase_selection"] = {}
        asr["three_phase_budget"] = budget
        asr["three_phase_budget_version"] = 4
        asr["selective_kept_ids"] = all_ids
        asr["selective_dropped_ids"] = []
        asr["selective_is_full"] = True
        asr["selection_mode"] = "three_phase_30s"
        asr["translation_model"] = used_model
        asr["translation_fallback_used"] = fallback_used
        asr["outcome"] = "translated"
        _update_pipeline_state(
            state_path,
            translation={
                "complete": True,
                "model": used_model,
                "cue_count": len(all_ids),
            },
        )
        if cache_path is not None:
            _atomic_write_json(cache_path, asr)
        return asr

    selected = None
    translated = None
    selection_model = selection_primary
    if selection_path is not None and selection_path.is_file():
        saved = _read_json_dict(selection_path)
        if (
            saved.get("schema") == "selection_v1"
            and saved.get("complete") is True
            and saved.get("source_fingerprint") == source_fingerprint
            and saved.get("budget_version") == 4
            and saved.get("model") == selection_primary
            and saved.get("reasoning_effort") == selection_reasoning
        ):
            kept_ids = {int(value) for value in saved.get("kept_ids") or []}
            kept_cues = _filter_cues_by_ids(cues, kept_ids)
            if kept_cues:
                selected = {
                    "kept_cues": kept_cues,
                    "plot": str(saved.get("plot") or ""),
                    "phases": dict(saved.get("phases") or {}),
                    "budget": dict(saved.get("budget") or budget),
                }
                selection_model = str(saved.get("model") or selection_primary)
                _log(
                    f"  [三段精選 checkpoint] 重用 {len(kept_cues)} 條選句"
                )
                _update_pipeline_state(
                    state_path,
                    selection={
                        "complete": True,
                        "model": selection_model,
                        "kept": len(kept_cues),
                        "resumed": True,
                    },
                )
    if selected is None:
        for attempt, model_name in enumerate(selection_models):
            try:
                selected = select_cues_three_phase(
                    cues,
                    api_key,
                    model_name,
                    reasoning_effort=selection_reasoning,
                )
                selection_model = model_name
                if selection_path is not None:
                    kept_ids = [
                        int(cue["id"]) for cue in selected["kept_cues"]
                    ]
                    kept_id_set = set(kept_ids)
                    _atomic_write_json(
                        selection_path,
                        {
                            "schema": "selection_v1",
                            "complete": True,
                            "source_fingerprint": source_fingerprint,
                            "mode": "three_phase_30s",
                            "budget_version": 4,
                            "model": model_name,
                            "reasoning_effort": selection_reasoning,
                            "is_full": len(kept_ids) == len(cues),
                            "kept_ids": kept_ids,
                            "dropped_ids": [
                                int(cue["id"])
                                for cue in cues
                                if int(cue["id"]) not in kept_id_set
                            ],
                            "plot": selected.get("plot") or "",
                            "phases": selected.get("phases") or {},
                            "budget": selected.get("budget") or budget,
                        },
                    )
                _update_pipeline_state(
                    state_path,
                    selection={
                        "complete": True,
                        "model": model_name,
                        "kept": len(selected["kept_cues"]),
                    },
                )
                break
            except Exception as exc:
                if attempt + 1 < len(selection_models):
                    _log_translation_fallback(
                        selection_primary, selection_models[attempt + 1], exc
                    )
                else:
                    raise RuntimeError(f"三段精選失敗：{exc}") from exc
    candidate_kept = selected["kept_cues"] if selected is not None else []
    for attempt, model_name in enumerate(translation_models):
        try:
            translated = translate_cues(
                candidate_kept,
                api_key,
                model_name,
                batch_size=0,
                checkpoint_path=translation_path,
                reasoning_effort=translation_reasoning,
            )
            used_model = model_name
            fallback_used = attempt > 0
            break
        except Exception as exc:
            if attempt + 1 < len(translation_models):
                _log_translation_fallback(
                    translation_primary, translation_models[attempt + 1], exc
                )
            else:
                raise RuntimeError(f"精選字幕翻譯失敗：{exc}") from exc
    if selected is None or translated is None:
        raise RuntimeError("三段精選／翻譯沒有產生結果")

    kept = selected["kept_cues"]
    kept_ids = [int(cue["id"]) for cue in kept]
    is_full = len(kept) == len(cues)
    asr = dict(asr)
    asr["cues"] = kept
    asr["source_cues"] = cues
    asr["original_srt"] = format_srt(kept)
    asr["translated_srt"] = format_srt(translated)
    asr["plot_summary"] = ""
    asr["three_phase_design"] = ""
    asr["three_phase_selection"] = selected["phases"]
    asr["three_phase_budget"] = selected["budget"]
    asr["three_phase_budget_version"] = 4
    asr["selective_kept_ids"] = kept_ids
    asr["selective_dropped_ids"] = sorted(
        int(cue["id"]) for cue in cues if int(cue["id"]) not in set(kept_ids)
    )
    asr["selective_is_full"] = is_full
    asr["selection_mode"] = "three_phase_30s"
    asr["translation_model"] = used_model
    asr["translation_fallback_used"] = fallback_used
    asr["selection_model"] = selection_model
    asr["outcome"] = "translated"
    _update_pipeline_state(
        state_path,
        selection={
            "complete": True,
            "model": selection_model,
            "kept": len(kept_ids),
        },
        translation={
            "complete": True,
            "model": used_model,
            "cue_count": len(kept_ids),
        },
    )
    if cache_path is not None:
        _atomic_write_json(cache_path, asr)
    return asr


def _three_phase_short_cue_limit() -> float:
    """回傳要排除三段精選額度的短句秒數；預設 0=全部送入精選。"""
    try:
        value = float(os.getenv("THREE_PHASE_SHORT_CUE_SECONDS", "0"))
    except ValueError as exc:
        raise ValueError("THREE_PHASE_SHORT_CUE_SECONDS 必須是秒數") from exc
    if value < 0:
        raise ValueError("THREE_PHASE_SHORT_CUE_SECONDS 不可小於 0")
    return value


def _complete_three_phase_with_preserved_short_cues(
    asr: dict[str, Any], short_limit: float, *, cache_path: Path | None,
    selection_path: Path | None, translation_path: Path | None,
    state_path: Path | None,
) -> dict[str, Any]:
    """短句不占 N 額度，但仍保留、翻譯並交給成品剪輯。"""
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
    if not api_key:
        raise RuntimeError("三段精選已開啟，但找不到 OPENROUTER_API_KEY")
    from translate_srt_openrouter import (
        DEFAULT_MODEL,
        _cue_duration_seconds,
        format_srt,
        select_cues_three_phase,
        three_phase_budget,
        translate_cues,
    )
    cues = _cues_for_translation(asr)
    if not cues:
        raise RuntimeError("三段精選需要帶時間軸的 ASR cues")
    short_cues = [
        cue for cue in cues if _cue_duration_seconds(cue) <= short_limit
    ]
    short_ids = {int(cue["id"]) for cue in short_cues}
    candidates = [cue for cue in cues if int(cue["id"]) not in short_ids]
    if not candidates:
        candidates, short_cues = cues, []
    budget = three_phase_budget(candidates)
    selection_primary = (
        os.getenv("THREE_PHASE_SELECTION_MODEL", "z-ai/glm-5.2").strip()
        or "z-ai/glm-5.2"
    )
    selection_reasoning = (
        os.getenv("THREE_PHASE_SELECTION_REASONING", "minimal").strip()
        or "minimal"
    )
    if len(candidates) < int(budget["total"]):
        selected = {
            "kept_cues": candidates,
            "plot": "",
            "phases": {},
            "budget": budget,
        }
        selection_model: str | None = None
        _log(
            f"  [三段精選] 長句 {len(candidates)} 條 < 總上限 "
            f"{budget['total']} 條，長句也完整保留"
        )
    else:
        selection_models = [selection_primary]
        fallback = os.getenv(
            "THREE_PHASE_SELECTION_FALLBACK_MODEL", "x-ai/grok-4.5"
        ).strip()
        if fallback and fallback.casefold() != selection_primary.casefold():
            selection_models.append(fallback)
        selected = None
        selection_model = selection_primary
        for attempt, candidate_model in enumerate(selection_models):
            try:
                selected = select_cues_three_phase(
                    candidates,
                    api_key,
                    candidate_model,
                    reasoning_effort=selection_reasoning,
                )
                selection_model = candidate_model
                break
            except Exception as exc:
                if attempt + 1 >= len(selection_models):
                    raise RuntimeError(f"三段精選失敗：{exc}") from exc
                _log_translation_fallback(candidate_model, selection_models[attempt + 1], exc)
        if selected is None:
            raise RuntimeError("三段精選沒有產生結果")
    selected_ids = {int(cue["id"]) for cue in selected["kept_cues"]}
    final_cues = [
        cue for cue in cues
        if int(cue["id"]) in selected_ids or int(cue["id"]) in short_ids
    ]
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
    translation_reasoning = (
        os.getenv("THREE_PHASE_TRANSLATE_REASONING", "minimal").strip()
        or "minimal"
    )
    translation_models = _translation_model_candidates(model)
    translated = None
    fallback_used = False
    for attempt, candidate_model in enumerate(translation_models):
        try:
            translated = translate_cues(
                final_cues,
                api_key,
                candidate_model,
                batch_size=0,
                checkpoint_path=translation_path,
                reasoning_effort=translation_reasoning,
            )
            model = candidate_model
            fallback_used = attempt > 0
            break
        except Exception as exc:
            if attempt + 1 >= len(translation_models):
                raise RuntimeError(f"精選字幕翻譯失敗：{exc}") from exc
            _log_translation_fallback(candidate_model, translation_models[attempt + 1], exc)
    if translated is None:
        raise RuntimeError("精選字幕翻譯沒有產生結果")
    result = dict(asr)
    final_ids = {int(cue["id"]) for cue in final_cues}
    result.update({
        "cues": final_cues,
        "source_cues": cues,
        "original_srt": format_srt(final_cues),
        "translated_srt": format_srt(translated),
        "plot_summary": selected.get("plot") or "",
        "three_phase_design": "",
        "three_phase_selection": selected.get("phases") or {},
        "three_phase_budget": budget,
        "three_phase_budget_version": 5,
        "selection_mode": "three_phase_short_cues_preserved",
        "selective_kept_ids": [int(cue["id"]) for cue in final_cues],
        "selective_dropped_ids": [
            int(cue["id"]) for cue in cues if int(cue["id"]) not in final_ids
        ],
        "selective_is_full": len(final_cues) == len(cues),
        "short_cues_preserved": len(short_cues),
        "short_cue_seconds": short_limit,
        "selection_model": selection_model,
        "translation_model": model,
        "translation_fallback_used": fallback_used,
        "outcome": "translated",
    })
    if selection_path is not None:
        _atomic_write_json(selection_path, {
            "schema": "selection_v1",
            "complete": True,
            "mode": result["selection_mode"],
            "budget_version": 5,
            "budget": budget,
            "model": selection_model,
            "reasoning_effort": selection_reasoning,
            "selected_long_ids": sorted(selected_ids),
            "preserved_short_ids": sorted(short_ids),
            "kept_ids": sorted(final_ids),
            "dropped_ids": result["selective_dropped_ids"],
            "is_full": result["selective_is_full"],
            "plot": result["plot_summary"],
            "phases": result["three_phase_selection"],
        })
    if cache_path is not None:
        _atomic_write_json(cache_path, result)
    _update_pipeline_state(
        state_path,
        selection={"complete": True, "model": selection_model, "kept": len(final_cues)},
        translation={"complete": True, "model": model, "cue_count": len(final_cues)},
    )
    return result


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
    _log(f"  [2/5] {backend.display_name} 辨識（Demucs 人聲軌）")
    cues, language = run_subtitle._transcribe_with_chunks(proxy_path, backend)
    _log(f"  語言：{language}；字幕段落：{len(cues)}")
    original_srt = format_srt(cues) if cues else ""
    translated_srt = ""
    outcome = "empty"
    translation_model = model_name
    if cues and translation_enabled:
        _log("  [3/5] OpenRouter 翻譯")
        for attempt, candidate_model in enumerate(
            _translation_model_candidates(model_name)
        ):
            try:
                translated = translate_cues(cues, api_key, candidate_model)
                translated_srt = format_srt(translated)
                model_name = candidate_model
                outcome = "translated"
                break
            except Exception as exc:
                models = _translation_model_candidates(model_name)
                if attempt + 1 < len(models):
                    _log_translation_fallback(
                        model_name, models[attempt + 1], exc
                    )
                else:
                    _log(f"  [!] 翻譯失敗，保留原文：{exc}")
                    outcome = "translation_failed"
        translation_model = model_name
    elif cues:
        _log("  [3/5] OpenRouter 翻譯已由開關停用")
        outcome = "transcribed"
    return {
        "language": language,
        "original_srt": original_srt,
        "translated_srt": translated_srt,
        "outcome": outcome,
        "translation_model": translation_model,
        "translation_fallback_used": bool(
            cues and model_name.casefold()
            != os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).casefold()
        ),
        "cues": cues,
    }


def run_asr_translate(proxy_path: Path, work_dir: Path) -> dict[str, Any]:
    """先以 Demucs 分離人聲，再依 ASR_BACKEND 執行轉寫與翻譯。"""
    from asr_audio import prepare_asr_audio
    from asr_backends import selected_asr_backend_name

    asr_audio = prepare_asr_audio(proxy_path, work_dir)
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
        return run_asr_translate_local(asr_audio)

    # MOSS：需在 moss venv
    if "moss" in str(current).casefold() and current.exists():
        return run_asr_translate_local(asr_audio)

    moss_python = Path(os.getenv("MOSS_PYTHON", str(DEFAULT_MOSS_PYTHON)))
    if not moss_python.is_file():
        raise RuntimeError(
            f"找不到 MOSS 環境：{moss_python}。請先執行 00_setup_or_update.bat。"
        )

    result_path = work_dir / "asr_worker_result.json"
    result_path.unlink(missing_ok=True)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env.setdefault("ASR_BACKEND", "moss")
    cmd = [
        str(moss_python),
        str(Path(__file__).resolve()),
        "--asr-only",
        str(asr_audio),
        "--result",
        str(result_path),
    ]
    _log("  [2/5] 啟動 MOSS 子程序做 ASR + 翻譯…")
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, check=False)
    if proc.returncode != 0 or not result_path.is_file():
        raise RuntimeError(
            f"MOSS ASR 子程序失敗，ExitCode={proc.returncode}"
        )
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    result_path.unlink(missing_ok=True)
    return payload


def run_asr_only_local(proxy_path: Path) -> dict[str, Any]:
    """只做 ASR；OpenRouter 翻譯留到第二階段平行執行。"""
    import run_subtitle
    from asr_backends import create_backend
    from translate_srt_openrouter import format_srt

    backend = create_backend().load()
    _log(f"  [2/5] {backend.display_name} 辨識（Demucs 人聲軌）")
    cues, language = run_subtitle._transcribe_with_chunks(proxy_path, backend)
    _log(f"  語言：{language}；字幕段落：{len(cues)}")
    return {
        "language": language,
        "original_srt": format_srt(cues) if cues else "",
        "translated_srt": "",
        "outcome": "transcribed" if cues else "empty",
        "cues": cues,
    }


def run_asr_only(proxy_path: Path, work_dir: Path) -> dict[str, Any]:
    """先分離人聲後 ASR，不在此階段呼叫 OpenRouter。"""
    from asr_audio import prepare_asr_audio
    from asr_backends import selected_asr_backend_name

    backend_name = selected_asr_backend_name()
    use_vad_roformer = (
        backend_name == "moss"
        and os.getenv("ENABLE_FIRERED_VAD", "1").strip().casefold()
        not in {"0", "false", "no", "off"}
        and os.getenv("ASR_VOCAL_SEPARATOR", "roformer").strip().casefold()
        in {"roformer", "mel-band-roformer", "melbandroformer"}
    )
    if use_vad_roformer:
        return _run_vad_roformer_moss(proxy_path, work_dir)

    asr_audio = prepare_asr_audio(proxy_path, work_dir)
    current = Path(sys.executable).resolve()
    if backend_name in {"voxtral", "grok-stt", "whisper"}:
        return run_asr_only_local(asr_audio)
    if "moss" in str(current).casefold() and current.exists():
        payload = run_asr_only_local(asr_audio)
        payload["cues"] = merge_moss_cues(payload.get("cues") or [])
        from translate_srt_openrouter import format_srt

        payload["original_srt"] = format_srt(payload["cues"])
        payload["moss_cue_merge_version"] = MOSS_CUE_MERGE_VERSION
        return payload

    moss_python = Path(os.getenv("MOSS_PYTHON", str(DEFAULT_MOSS_PYTHON)))
    if not moss_python.is_file():
        raise RuntimeError(
            f"找不到 MOSS 環境：{moss_python}。請先執行 00_setup_or_update.bat。"
        )
    result_path = work_dir / "asr_worker_result.json"
    result_path.unlink(missing_ok=True)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["ENABLE_TRANSLATION"] = "0"
    env.setdefault("ASR_BACKEND", "moss")
    cmd = [
        str(moss_python),
        str(Path(__file__).resolve()),
        "--asr-only",
        str(asr_audio),
        "--result",
        str(result_path),
    ]
    _log("  [2/5] 啟動 MOSS 子程序做 ASR…")
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, check=False)
    if proc.returncode != 0 or not result_path.is_file():
        raise RuntimeError(f"MOSS ASR 子程序失敗，ExitCode={proc.returncode}")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    result_path.unlink(missing_ok=True)
    payload["cues"] = merge_moss_cues(payload.get("cues") or [])
    from translate_srt_openrouter import format_srt

    payload["original_srt"] = format_srt(payload["cues"])
    payload["moss_cue_merge_version"] = MOSS_CUE_MERGE_VERSION
    return payload


def _run_vad_roformer_moss(proxy_path: Path, work_dir: Path) -> dict[str, Any]:
    """將 VAD 人聲段分離後，按最多三分鐘送 MOSS 並回映原時間軸。"""
    from asr_backends import asr_batch_size
    from asr_vad_roformer import map_compact_time, prepare_vad_roformer_audio
    from translate_srt_openrouter import format_srt

    prepared = prepare_vad_roformer_audio(proxy_path, work_dir)
    if not prepared.chunks:
        return {
            "language": "multilingual",
            "original_srt": "",
            "translated_srt": "",
            "outcome": "empty",
            "cues": [],
            "singing_ranges": [list(item) for item in prepared.singing_ranges],
            "speech_ranges": [list(item) for item in prepared.speech_ranges],
        }
    raw_results: list[dict[str, Any]] = []
    chunk_size = asr_batch_size()
    for start in range(0, len(prepared.chunks), chunk_size):
        group = prepared.chunks[start:start + chunk_size]
        _log(
            f"  [MOSS] VAD/RoFormer 三分鐘音檔 {start + 1}-"
            f"{start + len(group)}/{len(prepared.chunks)}"
        )
        raw_results.extend(
            run_asr_audio_batch(
                [Path(item["path"]) for item in group],
                work_dir / f"moss-vad-{start + 1:03d}",
            )
        )
    cues: list[dict[str, Any]] = []
    languages: list[str] = []
    for chunk, result in zip(prepared.chunks, raw_results, strict=True):
        language = str(result.get("language") or "").strip()
        if language and language not in languages:
            languages.append(language)
        for cue in result.get("cues") or []:
            try:
                start_text, end_text = str(cue["time"]).split("-->", 1)
                compact_start = segment_cutter.parse_srt_time(start_text.strip())
                compact_end = segment_cutter.parse_srt_time(end_text.strip())
                original_start = map_compact_time(compact_start, chunk["mapping"])
                original_end = map_compact_time(compact_end, chunk["mapping"])
            except (KeyError, TypeError, ValueError):
                continue
            if original_end <= original_start:
                continue
            cues.append({
                "id": len(cues) + 1,
                "time": (
                    f"{segment_cutter.format_srt_time(original_start)} --> "
                    f"{segment_cutter.format_srt_time(original_end)}"
                ),
                "text": str(cue.get("text") or ""),
            })
    cues = merge_moss_cues(cues)
    return {
        "language": ",".join(languages) or "multilingual",
        "original_srt": format_srt(cues) if cues else "",
        "translated_srt": "",
        "outcome": "transcribed" if cues else "empty",
        "cues": cues,
        "singing_ranges": [list(item) for item in prepared.singing_ranges],
        "speech_ranges": [list(item) for item in prepared.speech_ranges],
        "asr_audio_mode": "firered_vad_roformer_3min",
        "asr_chunk_count": len(prepared.chunks),
        "moss_cue_merge_version": MOSS_CUE_MERGE_VERSION,
    }


def run_asr_batch_local(proxy_paths: list[Path]) -> list[dict[str, Any]]:
    """同一個 backend 載入一次，對多個已就緒 ASR 片段做真正的 batch。"""
    from asr_backends import create_backend

    if not proxy_paths:
        return []
    backend = create_backend().load()
    return _run_asr_batch_with_backend(proxy_paths, backend)


def _run_asr_batch_with_backend(
    proxy_paths: list[Path], backend: Any,
) -> list[dict[str, Any]]:
    """以已載入的 backend 執行 ASR；供常駐 MOSS worker 重複使用。"""
    from translate_srt_openrouter import format_srt

    _log(f"  [2/5] {backend.display_name} 批次辨識：BS={len(proxy_paths)}")
    release = getattr(backend, "release_transient_memory", None)
    try:
        batch_fn = getattr(backend, "transcribe_batch", None)
        if callable(batch_fn):
            values = list(batch_fn(proxy_paths))
        else:
            values = [backend.transcribe(path) for path in proxy_paths]
    finally:
        if callable(release):
            release()
    if len(values) != len(proxy_paths):
        raise RuntimeError(
            f"ASR 批次回傳 {len(values)} 筆，預期 {len(proxy_paths)} 筆。"
        )
    results: list[dict[str, Any]] = []
    for cues, language in values:
        results.append(
            {
                "language": language,
                "original_srt": format_srt(cues) if cues else "",
                "translated_srt": "",
                "outcome": "transcribed" if cues else "empty",
                "cues": cues,
            }
        )
    return results


def _uses_external_moss() -> bool:
    """目前程序不是 MOSS venv，且 ASR 指定為 MOSS 時才需要常駐子程序。"""
    from asr_backends import selected_asr_backend_name

    current = Path(sys.executable).resolve()
    return (
        selected_asr_backend_name() == "moss"
        and not ("moss" in str(current).casefold() and current.exists())
    )


class MossAsrWorker:
    """一次管線執行常駐的 MOSS 子程序，避免每個片段或影片重載權重。"""

    def __init__(self) -> None:
        moss_python = Path(os.getenv("MOSS_PYTHON", str(DEFAULT_MOSS_PYTHON)))
        if not moss_python.is_file():
            raise RuntimeError(
                f"找不到 MOSS 環境：{moss_python}。請先執行 00_setup_or_update.bat。"
            )
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["ENABLE_TRANSLATION"] = "0"
        env.setdefault("ASR_BACKEND", "moss")
        self._process = subprocess.Popen(
            [str(moss_python), str(Path(__file__).resolve()), "--asr-worker"],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._closed = False
        self._lock = Lock()
        _log("  [MOSS 常駐] 已啟動；後續 ASR 片段不會重載權重")

    def transcribe(self, audio_paths: list[Path]) -> list[dict[str, Any]]:
        from asr_vad_roformer import gpu_inference_lock

        if self._closed or self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("MOSS 常駐程序已關閉")
        request = {"audio": [str(Path(path).resolve()) for path in audio_paths]}
        # self._lock 保護同一常駐程序的 stdin/stdout；全域鎖保證多支影片、
        # 甚至意外建立多個 worker 時，仍只會有一筆 MOSS 推理在執行。
        with gpu_inference_lock("MOSS"), _MOSS_INFERENCE_LOCK, self._lock:
            try:
                self._process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self._process.stdin.flush()
                line = self._process.stdout.readline()
            except OSError as exc:
                raise RuntimeError("MOSS 常駐程序通訊失敗") from exc
        if not line:
            raise RuntimeError(
                f"MOSS 常駐程序提早結束，ExitCode={self._process.poll()}"
            )
        try:
            reply = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"MOSS 常駐程序回傳無效 JSON：{line[:160]}") from exc
        if reply.get("error"):
            raise RuntimeError(f"MOSS 常駐程序失敗：{reply['error']}")
        payload = reply.get("results")
        if not isinstance(payload, list) or len(payload) != len(audio_paths):
            raise RuntimeError("MOSS 常駐程序回傳格式或數量不符。")
        return payload

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.stdin is not None and self._process.poll() is None:
                self._process.stdin.write('{"command":"shutdown"}\n')
                self._process.stdin.flush()
                self._process.stdin.close()
            self._process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self._process.kill()
            self._process.wait(timeout=10)


@contextmanager
def moss_asr_session() -> Iterator[MossAsrWorker | None]:
    """MOSS 才建立常駐程序；其他 ASR backend 完全維持原有獨立流程。"""
    if not _uses_external_moss():
        yield None
        return
    worker = MossAsrWorker()
    try:
        yield worker
    finally:
        worker.close()


def _safe_asr_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """讓 MOSS 子程序輸出的 cues 一定可 JSON 序列化。"""
    safe_cues = []
    for cue in payload.get("cues") or []:
        safe_cues.append(
            {
                "id": cue.get("id"),
                "time": cue.get("time"),
                "text": cue.get("text"),
            }
        )
    result = dict(payload)
    result["cues"] = safe_cues
    return result


def run_moss_asr_worker() -> int:
    """MOSS 常駐程序入口：stdin/stdout 僅走一行一筆 JSON 協定。"""
    from asr_backends import create_backend

    protocol_stdout = sys.stdout
    # transformers／ModelScope 的進度輸出不可混入協定 stdout，全部改送終端 stderr。
    with redirect_stdout(sys.stderr):
        backend = create_backend().load()
        _log("[MOSS 常駐] 權重已載入，等待 ASR 工作")
    for raw in sys.stdin:
        try:
            request = json.loads(raw)
            if request.get("command") == "shutdown":
                return 0
            audio = request.get("audio")
            if not isinstance(audio, list) or not audio:
                raise ValueError("audio 必須是非空陣列")
            paths = [Path(item).resolve() for item in audio]
            with redirect_stdout(sys.stderr):
                results = _run_asr_batch_with_backend(paths, backend)
            reply: dict[str, Any] = {
                "results": [_safe_asr_payload(item) for item in results]
            }
        except Exception as exc:
            reply = {"error": str(exc)}
        protocol_stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        protocol_stdout.flush()
    return 0


def run_asr_batch(
    proxy_paths: list[Path],
    work_dir: Path,
    moss_worker: MossAsrWorker | None = None,
) -> list[dict[str, Any]]:
    """相容入口：每段先 Demucs，再以目前可用的動態 BS 做 ASR。"""
    from asr_audio import prepare_asr_audio

    if not proxy_paths:
        return []
    asr_audio = [
        prepare_asr_audio(path, work_dir / f"demucs-{index:03d}")
        for index, path in enumerate(proxy_paths, 1)
    ]
    return run_asr_audio_batch(asr_audio, work_dir, moss_worker=moss_worker)


def run_asr_audio_batch(
    asr_audio: list[Path],
    work_dir: Path,
    moss_worker: MossAsrWorker | None = None,
) -> list[dict[str, Any]]:
    """對已完成人聲分離的音檔做 ASR；讓 Demucs 與 MOSS 可用佇列重疊。"""
    from asr_backends import selected_asr_backend_name

    if not asr_audio:
        return []
    if moss_worker is not None:
        try:
            return moss_worker.transcribe(asr_audio)
        except RuntimeError as exc:
            _log(f"  [MOSS 常駐] 失敗，回退單次程序：{exc}")
            moss_worker.close()
    backend_name = selected_asr_backend_name()
    current = Path(sys.executable).resolve()
    if backend_name in {"voxtral", "grok-stt", "whisper"} or (
        "moss" in str(current).casefold() and current.exists()
    ):
        if backend_name == "moss":
            from asr_vad_roformer import gpu_inference_lock

            with gpu_inference_lock("MOSS"), _MOSS_INFERENCE_LOCK:
                return run_asr_batch_local(asr_audio)
        return run_asr_batch_local(asr_audio)

    moss_python = Path(os.getenv("MOSS_PYTHON", str(DEFAULT_MOSS_PYTHON)))
    if not moss_python.is_file():
        raise RuntimeError(
            f"找不到 MOSS 環境：{moss_python}。請先執行 00_setup_or_update.bat。"
        )
    result_path = work_dir / "asr_batch_result.json"
    result_path.unlink(missing_ok=True)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["ENABLE_TRANSLATION"] = "0"
    env.setdefault("ASR_BACKEND", "moss")
    cmd = [
        str(moss_python),
        str(Path(__file__).resolve()),
        "--asr-batch",
        *(str(path) for path in asr_audio),
        "--result",
        str(result_path),
    ]
    _log(f"  [2/5] 啟動 MOSS 子程序批次 ASR：BS={len(asr_audio)}")
    # 沒有常駐 worker 時也同樣要串列化，避免兩支影片各自啟動 MOSS 搶資源。
    from asr_vad_roformer import gpu_inference_lock

    with gpu_inference_lock("MOSS"), _MOSS_INFERENCE_LOCK:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env, check=False)
    if proc.returncode != 0 or not result_path.is_file():
        raise RuntimeError(f"MOSS 批次 ASR 子程序失敗，ExitCode={proc.returncode}")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or len(payload) != len(asr_audio):
        raise RuntimeError("MOSS 批次 ASR 回傳格式或數量不符。")
    return payload


def _offset_asr_cues(cues: list[dict], offset: float, first_id: int) -> list[dict]:
    """把單一 3 分鐘 ASR 結果換回原影片的絕對時間軸。"""
    adjusted: list[dict] = []
    for index, cue in enumerate(cues, first_id):
        try:
            start_text, end_text = str(cue["time"]).split("-->", 1)
            start = segment_cutter.parse_srt_time(start_text.strip()) + offset
            end = segment_cutter.parse_srt_time(end_text.strip()) + offset
        except (KeyError, ValueError):
            continue
        item = dict(cue)
        item["id"] = index
        item["time"] = (
            f"{segment_cutter.format_srt_time(start)} --> "
            f"{segment_cutter.format_srt_time(end)}"
        )
        adjusted.append(item)
    return adjusted


def run_streamed_asr(
    video_url: str,
    work_dir: Path,
    video_stem: str,
    *,
    moss_worker: MossAsrWorker | None = None,
    max_duration: float | None = None,
    checkpoint_path: Path | None = None,
    state_path: Path | None = None,
) -> tuple[dict[str, Any], float]:
    """分段下載 240P 並批次 ASR；可限制只分析影片開頭一段時間。"""
    from translate_srt_openrouter import format_srt

    stream_enabled = asr_stream_enabled()
    duration = remote_duration(video_url) if stream_enabled else None
    if max_duration is not None:
        max_duration = max(0.05, float(max_duration))
        duration = min(duration, max_duration) if duration is not None else max_duration
    if max_duration is None and (not stream_enabled or duration is None):
        proxy_path = work_dir / f"{video_stem}.proxy.mp4"
        _log("  [ASR 串流] 無法取得時長或已關閉，改用完整 240P 代理")
        download_proxy_low(video_url, proxy_path)
        result = run_asr_only(proxy_path, work_dir)
        return result, probe_duration(proxy_path) or 0.0

    chunk_seconds = asr_stream_chunk_seconds()
    ranges = [
        (start, min(duration, start + chunk_seconds))
        for start in (index * chunk_seconds for index in range(math.ceil(duration / chunk_seconds)))
    ]
    _log(
        f"  [ASR 串流] 240P 共 {len(ranges)} 段，每段最多 {chunk_seconds / 60:.1f} 分鐘；"
        "下載→人聲分離→MOSS 三段佇列重疊"
    )
    from asr_backends import asr_batch_size

    demucs_queue: Queue[tuple[int, float, Path] | None] = Queue()
    moss_queue: Queue[tuple[int, float, Path] | None] = Queue()
    completed: dict[int, tuple[float, dict[str, Any]]] = {}
    worker_errors: list[BaseException] = []
    batch_limit = asr_batch_size()
    if checkpoint_path is not None and checkpoint_path.is_file():
        checkpoint = _read_json_dict(checkpoint_path)
        same_source = (
            checkpoint.get("schema") == "asr_source_v1"
            and abs(float(checkpoint.get("source_duration") or -1) - duration) < 0.5
            and abs(float(checkpoint.get("chunk_seconds") or -1) - chunk_seconds) < 0.01
        )
        if same_source:
            for key, item in dict(checkpoint.get("chunks") or {}).items():
                try:
                    index = int(key)
                    result = dict(item["result"])
                    start = float(item["start"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 1 <= index <= len(ranges):
                    completed[index] = (start, result)
            if completed:
                _log(
                    f"  [ASR checkpoint] 已完成 {len(completed)}/{len(ranges)} 段，"
                    "本次只補缺少部分"
                )

    def build_asr_snapshot(*, complete: bool) -> dict[str, Any]:
        merged: list[dict[str, Any]] = []
        languages: list[str] = []
        chunks: dict[str, Any] = {}
        for index in sorted(completed):
            start, result = completed[index]
            cues = result.get("cues") or []
            merged.extend(_offset_asr_cues(cues, start, len(merged) + 1))
            language = result.get("language")
            if language and language not in languages:
                languages.append(str(language))
            chunks[str(index)] = {
                "start": start,
                "end": ranges[index - 1][1],
                "result": result,
            }
        merged = merge_moss_cues(merged)
        return {
            "schema": "asr_source_v1",
            "complete": complete,
            "language": ",".join(languages) or "multilingual",
            "original_srt": format_srt(merged) if merged else "",
            "translated_srt": "",
            "outcome": "transcribed" if merged else "empty",
            "cues": merged,
            "source_duration": duration,
            "chunk_seconds": chunk_seconds,
            "total_chunks": len(ranges),
            "completed_chunks": sorted(completed),
            "chunks": chunks,
            "moss_cue_merge_version": MOSS_CUE_MERGE_VERSION,
        }

    def persist_asr_checkpoint() -> None:
        if checkpoint_path is None:
            return
        with _PIPELINE_CHECKPOINT_LOCK:
            snapshot = build_asr_snapshot(
                complete=len(completed) == len(ranges)
            )
            _atomic_write_json(checkpoint_path, snapshot)
        _update_pipeline_state(
            state_path,
            asr={
                "complete": snapshot["complete"],
                "completed": len(completed),
                "total": len(ranges),
            },
        )

    def consume_demucs_queue() -> None:
        """下載一完成就做人聲分離；不等待 MOSS 是否空閒。"""
        from asr_audio import prepare_asr_audio

        while True:
            job = demucs_queue.get()
            if job is None:
                demucs_queue.task_done()
                moss_queue.put(None)
                return
            index, start, proxy_path = job
            try:
                _log(f"  [人聲分離佇列] 開始第 {index}/{len(ranges)} 段")
                audio_path = prepare_asr_audio(
                    proxy_path,
                    work_dir / f"demucs-{index:03d}",
                )
                moss_queue.put((index, start, audio_path))
                _log(
                    f"  [人聲分離佇列] 完成第 {index}/{len(ranges)} 段；"
                    f"等待 MOSS {moss_queue.qsize()} 段"
                )
            except BaseException as exc:
                worker_errors.append(exc)
                moss_queue.put(None)
                return
            finally:
                demucs_queue.task_done()

    def consume_moss_queue() -> None:
        while True:
            first = moss_queue.get()
            if first is None:
                moss_queue.task_done()
                return
            jobs = [first]
            stop_after_batch = False
            # 不等待湊滿 BS：人聲分離一完成就立即送唯一的 MOSS 推理槽。
            while len(jobs) < batch_limit:
                try:
                    next_job = moss_queue.get_nowait()
                except Empty:
                    break
                if next_job is None:
                    moss_queue.task_done()
                    stop_after_batch = True
                    break
                jobs.append(next_job)
            try:
                queued = moss_queue.qsize()
                _log(
                    f"  [MOSS 佇列] 已就緒 {queued + len(jobs)} 段；"
                    f"取得唯一推理槽後送 MOSS，BS={len(jobs)}/{batch_limit}"
                )
                batch_work = work_dir / f"asr-batch-{jobs[0][0]:03d}"
                paths = [job[2] for job in jobs]
                results = run_asr_audio_batch(
                    paths,
                    batch_work,
                    moss_worker=moss_worker,
                )
                for job, result in zip(jobs, results):
                    completed[job[0]] = (job[1], result)
                persist_asr_checkpoint()
            except BaseException as exc:
                worker_errors.append(exc)
                return
            finally:
                for _job in jobs:
                    moss_queue.task_done()
            if stop_after_batch:
                return

    demucs_worker = Thread(
        target=consume_demucs_queue,
        name="demucs-queue",
        daemon=True,
    )
    moss_queue_worker = Thread(
        target=consume_moss_queue,
        name="moss-queue",
        daemon=True,
    )
    demucs_worker.start()
    moss_queue_worker.start()
    try:
        for index, (start, end) in enumerate(ranges, 1):
            if worker_errors:
                break
            if index in completed:
                _log(f"  [ASR checkpoint] 跳過已完成第 {index}/{len(ranges)} 段")
                continue
            proxy_part = work_dir / f"{video_stem}.proxy.asr{index:03d}.mp4"
            if not (
                proxy_part.is_file()
                and has_video_stream(proxy_part) is True
                and _decodes_cleanly(proxy_part)
            ):
                download_proxy_range(video_url, proxy_part, start, end)
            else:
                _log(f"  [240P checkpoint] 重用第 {index}/{len(ranges)} 段")
            demucs_queue.put((index, start, proxy_part))
            _log(
                f"  [240P 佇列] 第 {index}/{len(ranges)} 段下載完成；"
                f"已排入人聲分離，等待中 {demucs_queue.qsize()} 段"
            )
    finally:
        demucs_queue.put(None)
        demucs_worker.join()
        moss_queue_worker.join()
    if worker_errors:
        raise RuntimeError(f"ASR 佇列失敗：{worker_errors[0]}") from worker_errors[0]
    if len(completed) != len(ranges):
        raise RuntimeError(
            f"ASR 佇列完成數量不符：{len(completed)}/{len(ranges)}"
        )

    snapshot = build_asr_snapshot(complete=True)
    if checkpoint_path is not None:
        with _PIPELINE_CHECKPOINT_LOCK:
            _atomic_write_json(checkpoint_path, snapshot)
        _update_pipeline_state(
            state_path,
            asr={"complete": True, "completed": len(ranges), "total": len(ranges)},
        )
    for index in range(1, len(ranges) + 1):
        _start, result = completed[index]
        cues = result.get("cues") or []
        _log(f"  [ASR 串流] 完成 {index}/{len(ranges)}：{len(cues)} 段字幕")
    return snapshot, duration


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


def enhance_full_video(
    video: Path,
    audio_worker=None,
) -> tuple[Path, bool]:
    """整片走既有 prepare_audio_media。"""
    from audio_enhance_stage import auto_enhance_enabled, prepare_audio_media

    if not auto_enhance_enabled():
        return video, False
    try:
        prepared = (
            prepare_audio_media([video])
            if audio_worker is None
            else audio_worker.prepare([video])
        )
    except RuntimeError as exc:
        if audio_worker is None:
            raise
        _log(f"  [音訊 Enhance 常駐] 失敗，回退單次程序：{exc}")
        audio_worker.close()
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


def _translate_after_asr(
    asr: dict[str, Any],
    cache_path: Path | None,
    translation_path: Path | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    """第二階段的 OpenRouter 工作；翻譯失敗時保留原文並讓其他並行工作完成。"""
    try:
        # 精選翻譯會在分段規劃前同步完成；能進到背景翻譯的都是一般翻譯。
        # 明確關閉 selective，避免一般翻譯受環境開關影響而誤走精選。
        return complete_cached_translation(
            asr,
            cache_path,
            selective=False,
            translation_path=translation_path,
            state_path=state_path,
        )
    except Exception as exc:
        _log(f"  [!] OpenRouter 翻譯失敗，保留原文：{exc}")
        asr["translated_srt"] = ""
        asr["outcome"] = "translation_failed"
        if cache_path is not None:
            _atomic_write_json(cache_path, asr)
        _update_pipeline_state(
            state_path,
            translation={"complete": False, "error": str(exc)},
        )
        return asr


def run_parallel_delivery_phase(
    video_url: str,
    work_dir: Path,
    video_stem: str,
    asr: dict[str, Any],
    segments: list[tuple[float, float]] | None,
    *,
    enable_translation: bool,
    enable_enhance: bool,
    asr_cache: Path | None,
    translation_path: Path | None = None,
    state_path: Path | None = None,
    audio_worker=None,
    audio_crossfade_seconds: float = 0.0,
) -> tuple[Path, str, str, bool]:
    """平行執行 OpenRouter、切塊下載與切塊後 enhance，全部完成才回傳。"""
    high_path = work_dir / f"{video_stem}.high.mp4"
    original_srt = asr.get("original_srt") or ""
    translated_srt = asr.get("translated_srt") or ""

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="translate") as translator, ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="enhance"
    ) as enhancer:
        def submit_enhance(path: Path) -> Future[tuple[Path, bool]]:
            if audio_worker is None:
                return enhancer.submit(enhance_full_video, path)
            return enhancer.submit(enhance_full_video, path, audio_worker)

        translation_future: Future[dict[str, Any]] | None = None
        if enable_translation:
            _log("  [第二階段] OpenRouter 翻譯、高畫質下載、Enhance 可同時進行")
            translation_future = translator.submit(
                _translate_after_asr,
                asr,
                asr_cache,
                translation_path,
                state_path,
            )
        else:
            _log("  [第二階段] 翻譯已關閉；開始高畫質下載與 Enhance")

        enhanced = False
        if segments is None:
            _log("  [第二階段] 下載完整高畫質影片")
            with high_quality_download_phase(video_stem):
                download_high_full(video_url, high_path)
            _update_pipeline_state(
                state_path,
                high_quality={"complete": True, "mode": "full"},
            )
            enhance_future = (
                submit_enhance(high_path)
                if enable_enhance
                else None
            )
            if enhance_future is not None:
                high_path, enhanced = enhance_future.result()
        else:
            download_workers = segment_download_workers()
            _log(
                f"  [第二階段] 下載 {len(segments)} 個高畫質切塊"
                f"（並行請求={download_workers}；"
                f"每 {segment_download_start_interval_seconds():.1f}s 啟動；"
                "完成即補下一段）"
            )
            progress = _SegmentProgress(
                len(segments),
                enhance_enabled=enable_enhance,
            )
            progress.update()
            part_futures: dict[
                int,
                tuple[Path, Future[tuple[Path, bool]] | None],
            ] = {}
            failed_downloads: list[
                tuple[int, Path, float, float, Exception]
            ] = []
            saved_high = _prepare_high_quality_manifest(
                state_path,
                segments,
                video_stem,
            )
            saved_segments = dict(saved_high.get("segments") or {})

            def queue_enhance(
                index: int,
                part: Path,
            ) -> None:
                future = submit_enhance(part) if enable_enhance else None
                part_futures[index] = (part, future)
                progress.update(
                    downloaded=1,
                    enhanced=1 if future is None else 0,
                )
                if future is not None:
                    future.add_done_callback(
                        lambda completed: progress.update(
                            enhanced=1,
                            enhance_failures=(
                                1 if completed.exception() is not None else 0
                            ),
                        )
                    )

            def try_download(
                index: int,
                part: Path,
                start: float,
                end: float,
                attempts: int,
            ) -> Exception | None:
                last_error: Exception | None = None
                for attempt in range(attempts):
                    try:
                        download_high_range(
                            video_url,
                            part,
                            start,
                            end,
                        )
                        _record_high_quality_segment(
                            state_path,
                            index=index,
                            start=start,
                            end=end,
                            path=part,
                        )
                        return None
                    except Exception as exc:
                        last_error = exc
                        if attempt + 1 < attempts:
                            progress.update(retries=1)
                return last_error

            normal_attempts = _positive_int_env(
                "SEGMENT_DOWNLOAD_ATTEMPTS",
                SEGMENT_DOWNLOAD_ATTEMPTS,
            )

            def download_in_slot(
                index: int,
                part: Path,
                start: float,
                end: float,
            ) -> Exception | None:
                with segment_download_slot(download_workers):
                    return try_download(
                        index,
                        part,
                        start,
                        end,
                        normal_attempts,
                    )

            still_failed: list[
                tuple[int, Path, float, float, Exception]
            ] = []
            # 此鎖只包住高畫質下載與補抓。下一支影片可以在等待時繼續 240P、
            # 人聲分離、MOSS 與翻譯；一旦進到高畫質則依影片順序接力。
            with high_quality_download_phase(video_stem):
                queued_downloads: dict[
                    Future[Exception | None], tuple[int, Path, float, float]
                ] = {}
                # ThreadPoolExecutor 會固定保留 download_workers 個進行中請求；任何
                # 一段完成時會自動取出下一段，無須猜測連線／啟動延遲再人工等待。
                with ThreadPoolExecutor(
                    max_workers=download_workers,
                    thread_name_prefix="high-range",
                ) as downloader:
                    for index, (start, end) in enumerate(segments):
                        part = work_dir / f"{video_stem}.seg{index:03d}.mp4"
                        saved_part = dict(saved_segments.get(str(index)) or {})
                        range_matches = (
                            saved_part.get("complete") is True
                            and abs(float(saved_part.get("start") or -1) - start)
                            < 0.01
                            and abs(float(saved_part.get("end") or -1) - end)
                            < 0.01
                            and saved_part.get("path") == part.name
                        )
                        if (
                            range_matches
                            and part.exists()
                            and has_video_stream(part) is True
                            and _decodes_cleanly(part)
                        ):
                            _log(
                                f"  [高畫質 checkpoint] 重用第 "
                                f"{index + 1}/{len(segments)} 段"
                            )
                            queue_enhance(index, part)
                            continue
                        if part.exists():
                            part.unlink(missing_ok=True)
                        future = downloader.submit(
                            download_in_slot,
                            index,
                            part,
                            start,
                            end,
                        )
                        queued_downloads[future] = (index, part, start, end)

                    for future in as_completed(queued_downloads):
                        index, part, start, end = queued_downloads[future]
                        try:
                            error = future.result()
                        except Exception as exc:  # 防禦：工作執行緒不可中斷整批下載。
                            error = exc
                        if error is None:
                            queue_enhance(index, part)
                        else:
                            failed_downloads.append((index, part, start, end, error))
                            progress.update(pending_failures=1)

                recovery_attempts = _positive_int_env(
                    "SEGMENT_RECOVERY_ATTEMPTS",
                    SEGMENT_RECOVERY_ATTEMPTS,
                )
                if failed_downloads:
                    _log(
                        f"\n  [第二階段] {len(failed_downloads)} 段並行下載失敗，"
                        "改為單線補下載"
                    )
                for index, part, start, end, previous_error in failed_downloads:
                    progress.update(retries=1)
                    error = try_download(
                        index,
                        part,
                        start,
                        end,
                        recovery_attempts,
                    )
                    if error is None:
                        progress.update(pending_failures=-1)
                        queue_enhance(index, part)
                    else:
                        still_failed.append(
                            (index, part, start, end, error or previous_error)
                        )

            parts: list[Path] = []
            for index in sorted(part_futures):
                part, future = part_futures[index]
                if future is None:
                    parts.append(part)
                    continue
                try:
                    enhanced_part, part_enhanced = future.result()
                    parts.append(enhanced_part)
                    enhanced = enhanced or part_enhanced
                except Exception:
                    # Enhance 失敗不應浪費已下載區段；保留原音繼續合併。
                    parts.append(part)
            progress.finish()
            if still_failed:
                failed_text = "、".join(
                    f"{index + 1}({start:.2f}-{end:.2f}s)"
                    for index, _part, start, end, _error in still_failed
                )
                raise RuntimeError(
                    f"{len(still_failed)} 個高畫質區段多次重試仍失敗："
                    f"{failed_text}；已完成區段會保留供下次續跑。"
                ) from still_failed[0][4]
            if audio_crossfade_seconds > 0:
                concat_videos_audio_crossfade(
                    parts,
                    high_path,
                    audio_crossfade_seconds,
                )
            else:
                concat_videos(parts, high_path)

        if translation_future is not None:
            translated_asr = translation_future.result()
            translated_srt = translated_asr.get("translated_srt") or ""
            asr.update(translated_asr)
        else:
            # 已完成精選翻譯或重用內嵌翻譯時，不可把現成譯文清空。
            asr["translated_srt"] = translated_srt
            if translated_srt.strip():
                asr["outcome"] = asr.get("outcome") or "translated"
            else:
                asr["outcome"] = "transcribed" if original_srt else "empty"
            if asr_cache is not None:
                _atomic_write_json(asr_cache, asr)

    if segments is not None:
        if original_srt.strip():
            original_srt = _entries_to_srt(
                segment_cutter.retime_subtitles(
                    srt_text_to_entries(original_srt),
                    segments,
                    crossfade_seconds=0.0,
                )
            )
        if translated_srt.strip():
            translated_srt = _entries_to_srt(
                segment_cutter.retime_subtitles(
                    srt_text_to_entries(translated_srt),
                    segments,
                    crossfade_seconds=0.0,
                )
            )
        _log("  [第二階段] 翻譯、切塊下載與 Enhance 已全部完成；字幕已 retime")
    else:
        _log("  [第二階段] 翻譯、完整下載與 Enhance 已全部完成")
    if state_path is not None:
        high_quality_state = dict(
            _read_json_dict(state_path).get("high_quality") or {}
        )
        high_quality_state["complete"] = True
        _update_pipeline_state(
            state_path,
            high_quality=high_quality_state,
        )
    return high_path, original_srt, translated_srt, enhanced


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
    enable_selective_download: bool | None = None,
    enable_three_phase_selection: bool | None = None,
    enable_metadata: bool | None = None,
    dialogue_trim_threshold: float = DIALOGUE_TRIM_THRESHOLD,
    segment_gap: float = SEGMENT_GAP,
    enable_edge_padding: bool | None = None,
    force: bool = False,
    work_bucket: str = "03_videos",
    pipeline_stage: str | None = None,
    archive_grid_on_done: bool = True,
    moss_worker: MossAsrWorker | None = None,
    audio_worker=None,
    analysis_limit_seconds: float | None = None,
    reuse_embedded_translation: bool = False,
    always_download_subtitle_ranges: bool = False,
    require_subtitle_ranges: bool = False,
    unlimited_high_quality: bool = False,
) -> Path:
    """
    單支來源循序流程：從九宮格或影片取得 URL，低畫質代理 → ASR/翻譯 →
    高畫質 →（可）enhance → 發布。

    max_height：高畫質上限（預設讀 HIGH_VIDEO_HEIGHT，否則 720）
    enable_enhance：是否允許音訊增強（預設讀 AUDIO_AUTO_ENHANCE）
    enable_dialogue_trim：是否依停頓門檻移除長停頓並分段下載
    enable_selective_download：精選下載——先劇情+選擇性翻譯，再只下載保留對白
    enable_three_phase_selection：30 秒 N 三段精選；Shorts／Video／Chosen 預設開啟
    segment_gap：相鄰對白停頓 ≥ 此秒數則剪開（預設 1.5）
    enable_edge_padding：對白前後 0.75s 延伸開關（預設關閉）
    always_download_subtitle_ranges：舊呼叫相容參數，不再繞過純語音 30 秒保護
    """
    ensure_output_directories()
    jpg_path = Path(jpg_path).resolve()
    source_suffix = jpg_path.suffix.casefold()
    source_is_grid = source_suffix in {".jpg", ".jpeg", ".png"}
    pipeline_stage = pipeline_stage or {
        "02_shorts": "shorts",
        "05_chosen": "chosen",
    }.get(work_bucket, "video")
    if pipeline_stage not in {"shorts", "video", "chosen"}:
        raise ValueError(f"不支援的 Pipeline 類型：{pipeline_stage}")
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
    if enable_selective_download is None:
        enable_selective_download = selective_download_enabled()
    if enable_three_phase_selection is None:
        enable_three_phase_selection = (
            pipeline_stage in {"shorts", "video", "chosen"}
            and three_phase_selection_enabled()
        )
    if enable_three_phase_selection:
        enable_selective_download = True
        enable_dialogue_trim = True
    if enable_metadata is None:
        enable_metadata = os.getenv(
            "ENABLE_METADATA", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
    # 精選下載需要翻譯；關閉翻譯時自動關閉精選
    if enable_selective_download and not enable_translation:
        _log("  [精選下載] 翻譯已關閉，精選下載自動改為 OFF")
        enable_selective_download = False
    if enable_three_phase_selection and not enable_translation:
        _log("  [三段精選] 翻譯已關閉，三段精選自動改為 OFF")
        enable_three_phase_selection = False
    if enable_edge_padding is None:
        enable_edge_padding = edge_padding_enabled()
    edge_pad = resolve_edge_padding_seconds(enable_edge_padding)
    # 下載高度寫入 env，供 _high_format_opts 使用
    os.environ["HIGH_VIDEO_HEIGHT"] = str(max_height)
    os.environ["HIGH_VIDEO_UNLIMITED"] = "1" if unlimited_high_quality else "0"
    os.environ["ENABLE_TRANSLATION"] = "1" if enable_translation else "0"
    os.environ["ENABLE_SELECTIVE_DOWNLOAD"] = (
        "1" if enable_selective_download else "0"
    )
    os.environ["ENABLE_THREE_PHASE_SELECTION"] = (
        "1" if enable_three_phase_selection else "0"
    )
    os.environ["ENABLE_EDGE_PADDING"] = "1" if enable_edge_padding else "0"
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
    asr_source_path = work_dir / "asr_source.json"
    selection_path = work_dir / "selection.json"
    translation_path = work_dir / "translation.json"
    state_path = work_dir / "pipeline_state.json"
    legacy_asr_cache = work_dir / "asr_result.json"
    reuse_asr = os.getenv("REUSE_ASR_RESULT", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }
    embedded_asr: dict[str, Any] | None = None
    if reuse_embedded_translation and not source_is_grid:
        try:
            embedded_asr = load_embedded_translation(jpg_path, video_url)
        except Exception as exc:
            _log(f"  [Shorts] 讀取內嵌翻譯失敗，改走 240P 分析：{exc}")
    # 保留已完成的 240P／高畫質切片；是否可重用由 manifest 的範圍驗證決定。
    for stale in work_dir.glob(f"{video_stem}.high*.mp4"):
        stale.unlink(missing_ok=True)

    if not asr_source_path.is_file() and legacy_asr_cache.is_file():
        legacy = _read_json_dict(legacy_asr_cache)
        if legacy:
            migrated = _source_asr_payload(legacy)
            _atomic_write_json(asr_source_path, migrated)
            _log("  [checkpoint] 已將舊 asr_result.json 遷移為 asr_source.json")

    _log("=" * 60)
    _log(f"正式片循序管線：{video_stem}")
    _log(f"URL：{video_url}")
    _log("=" * 60)

    # 第一階段：每下載完 3 分鐘 240P 區段，就立即排入 Demucs + ASR。
    # OpenRouter 會留到所有 ASR 就緒後的第二階段，避免阻塞下載佇列。
    with _stage("01_stream_proxy_and_asr"):
        if embedded_asr is not None:
            asr = embedded_asr
            duration = float(asr.get("source_duration") or 0.0)
            _atomic_write_json(
                asr_source_path,
                _source_asr_payload(asr, duration),
            )
            _log("  [Shorts] 已有內嵌翻譯字幕，直接重用時間軸")
            if asr.get("embedded_timeline_restored"):
                _log("  [Shorts] 已將剪輯後字幕反向映射回原片絕對時間")
        elif (
            reuse_asr
            and asr_source_path.is_file()
            and _read_json_dict(asr_source_path).get("complete") is True
        ):
            try:
                asr = _read_json_dict(asr_source_path)
                if asr.get("moss_cue_merge_version") != MOSS_CUE_MERGE_VERSION:
                    previous_merge = asr.get("moss_cue_merge_version")
                    if previous_merge is not None:
                        # v1 已把短句併入其他字幕，無法由其輸出還原；必須從
                        # MOSS 原始結果重跑，才能讓全部短句進入新三段精選規則。
                        _log("  [ASR] 舊字幕已合併短句，重新 MOSS 以還原完整短句")
                        asr, duration = run_streamed_asr(
                            video_url,
                            work_dir,
                            video_stem,
                            moss_worker=moss_worker,
                            max_duration=analysis_limit_seconds,
                            checkpoint_path=asr_source_path,
                            state_path=state_path,
                        )
                        asr["source_duration"] = duration
                        _atomic_write_json(asr_source_path, _source_asr_payload(asr, duration))
                    else:
                        from translate_srt_openrouter import format_srt

                        asr["cues"] = merge_moss_cues(asr.get("cues") or [])
                        asr["original_srt"] = format_srt(asr["cues"])
                        asr["moss_cue_merge_version"] = MOSS_CUE_MERGE_VERSION
                        _atomic_write_json(asr_source_path, asr)
                        _log("  [ASR] 已將舊 MOSS 字幕套用重複句合併")
                if not enable_translation:
                    asr["translated_srt"] = ""
                    asr["outcome"] = "transcribed"
                duration = float(asr.get("source_duration") or 0.0)
                if duration <= 0:
                    duration = remote_duration(video_url) or 0.0
                _log(
                    f"  [ASR] 重用完整原始字幕：{asr_source_path.name}"
                    f"（cues={len(asr.get('cues') or [])}，"
                    f"outcome={asr.get('outcome')}）"
                )
            except Exception as exc:
                if not enable_asr:
                    raise RuntimeError(
                        f"ASR 已關閉，但字幕快取無法讀取：{exc}"
                    ) from exc
                _log(f"  [!] 讀取字幕快取失敗，改重新 ASR：{exc}")
                asr, duration = run_streamed_asr(
                    video_url,
                    work_dir,
                    video_stem,
                    moss_worker=moss_worker,
                    max_duration=analysis_limit_seconds,
                    checkpoint_path=asr_source_path,
                    state_path=state_path,
                )
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
            duration = remote_duration(video_url) or 0.0
        else:
            asr, duration = run_streamed_asr(
                video_url,
                work_dir,
                video_stem,
                moss_worker=moss_worker,
                max_duration=analysis_limit_seconds,
                checkpoint_path=asr_source_path,
                state_path=state_path,
            )
            asr["source_duration"] = duration
            try:
                _atomic_write_json(
                    asr_source_path,
                    _source_asr_payload(asr, duration),
                )
            except Exception:
                pass

    original_srt = asr.get("original_srt") or ""
    translated_srt = asr.get("translated_srt") or ""
    outcome = asr.get("outcome") or "empty"

    # 先用完整原始 ASR 判斷兩個保護條件，不能拿模型刪句後的字幕重算。
    source_cues = _cues_for_translation(asr)
    source_entries = cues_to_entries(source_cues) if source_cues else []
    singing_ranges = list(asr.get("singing_ranges") or [])
    source_net_dur = _net_dialogue_excluding_singing(source_entries, singing_ranges)
    source_budget: dict[str, Any] = {}
    source_under_budget = False
    if enable_three_phase_selection and source_cues:
        from translate_srt_openrouter import _cue_duration_seconds, three_phase_budget

        short_limit = _three_phase_short_cue_limit()
        long_cues = [
            cue for cue in source_cues
            if _cue_duration_seconds(cue) > short_limit
        ]
        # 全部都是短句時仍需有可用基準，避免空清單無法計算預算。
        budget_cues = long_cues or source_cues
        source_budget = three_phase_budget(budget_cues)
        source_under_budget = len(budget_cues) < int(source_budget["total"])
    shorts_full_analysis_range = (
        pipeline_stage == "shorts"
        and embedded_asr is None
        and analysis_limit_seconds is not None
        and source_net_dur < dialogue_trim_threshold
    )
    speech_under_threshold = source_net_dur < dialogue_trim_threshold
    three_phase_translation_reasoning = (
        os.getenv("THREE_PHASE_TRANSLATE_REASONING", "minimal").strip()
        or "minimal"
    )

    # 精選下載：必須先完成選擇性翻譯，才能依保留 id 規劃高畫質下載區段
    # （不能與下載平行）。歌詞由 LLM 改完整翻譯時 is_full=True。
    selective_done = embedded_asr is not None
    if (
        enable_selective_download
        and enable_translation
        and embedded_asr is None
    ):
        with _stage("02a_selective_translate"):
            if (
                not speech_under_threshold
                and not source_under_budget
                and (asr.get("translated_srt") or "").strip()
                and asr.get("selective_kept_ids") is not None
                and (
                    not enable_three_phase_selection
                    or (
                        asr.get("selection_mode") == "three_phase_short_cues_preserved"
                        and asr.get("three_phase_budget_version") == 5
                    )
                )
            ):
                _log("  [精選下載] 重用快取內已有精選翻譯結果")
                selective_done = True
            else:
                _log(
                    "  [三段精選] 等完整 240P/MOSS 後送 GLM 5.2 minimal 精選、"
                    "Grok 4.3 minimal 一次翻譯（失敗改 Grok 4.5）："
                    "30 秒 N 三段、保留完整選段…"
                    if enable_three_phase_selection
                    else "  [精選下載] 先劇情整理 + 選擇性翻譯（歌詞則完整翻譯）…"
                )
                try:
                    if enable_three_phase_selection and speech_under_threshold:
                        analysis_label = (
                            f"前{analysis_limit_seconds / 60:.0f}分鐘分析範圍"
                            if analysis_limit_seconds is not None
                            else "完整來源"
                        )
                        _log(
                            f"  [三段精選保護] 原始純語音 {source_net_dur:.2f}s "
                            f"< {dialogue_trim_threshold:.1f}s，完整翻譯{analysis_label}"
                        )
                        from translate_srt_openrouter import format_srt

                        fresh_asr = dict(asr)
                        fresh_asr["translated_srt"] = ""
                        fresh_asr["cues"] = source_cues
                        fresh_asr["source_cues"] = source_cues
                        fresh_asr["original_srt"] = format_srt(source_cues)
                        asr = complete_cached_translation(
                            fresh_asr,
                            selective=False,
                            translation_path=translation_path,
                            state_path=state_path,
                            reasoning_effort=three_phase_translation_reasoning,
                            batch_size=0,
                        )
                        all_ids = [int(cue["id"]) for cue in source_cues]
                        asr["selection_mode"] = "three_phase_short_cues_preserved"
                        asr["three_phase_selection_gate"] = "speech_under_30s"
                        asr["three_phase_budget"] = source_budget
                        asr["three_phase_budget_version"] = 5
                        asr["selective_is_full"] = True
                        asr["selective_kept_ids"] = all_ids
                        asr["selective_dropped_ids"] = []
                        if selection_path is not None:
                            _atomic_write_json(
                                selection_path,
                                {
                                    "schema": "selection_v1",
                                    "complete": True,
                                    "mode": "three_phase_short_cues_preserved",
                                    "budget_version": 5,
                                    "model": None,
                                    "is_full": True,
                                    "gate": "speech_under_30s",
                                    "kept_ids": all_ids,
                                    "dropped_ids": [],
                                    "plot": "",
                                    "phases": {},
                                    "budget": source_budget,
                                },
                            )
                    else:
                        asr = (
                            complete_three_phase_translation(
                                asr,
                                selection_path=selection_path,
                                translation_path=translation_path,
                                state_path=state_path,
                            )
                            if enable_three_phase_selection
                            else complete_cached_translation(
                                asr,
                                selective=True,
                                translation_path=translation_path,
                                state_path=state_path,
                            )
                        )
                    selective_done = True
                except Exception as exc:
                    saved_selection = _read_json_dict(selection_path)
                    _update_pipeline_state(
                        state_path,
                        selection={
                            "complete": saved_selection.get("complete") is True,
                            "error": (
                                None
                                if saved_selection.get("complete") is True
                                else str(exc)
                            ),
                            "publication_blocked": True,
                        },
                        translation={
                            "complete": False,
                            "error": str(exc),
                        },
                    )
                    raise RuntimeError(
                        "三段精選／翻譯失敗，已停止發布；"
                        "不會回退成全日文對白成品。"
                    ) from exc
        original_srt = asr.get("original_srt") or original_srt
        translated_srt = asr.get("translated_srt") or translated_srt
        outcome = asr.get("outcome") or outcome
        if selective_done and embedded_asr is None:
            is_full = bool(asr.get("selective_is_full"))
            kept_n = len(asr.get("selective_kept_ids") or [])
            drop_n = len(asr.get("selective_dropped_ids") or [])
            _log(
                f"  [精選下載] "
                f"{'完整/歌詞' if is_full else '精選省略'}："
                f"保留 {kept_n} 條、省略 {drop_n} 條"
            )
            plot = (asr.get("plot_summary") or "").strip()
            if plot:
                _log(f"  [劇情] {plot[:180]}{'…' if len(plot) > 180 else ''}")

    # 優先用譯文估對話；否則用原文
    srt_for_segments = translated_srt.strip() or original_srt
    entries = srt_text_to_entries(srt_for_segments) if srt_for_segments else []
    if not entries and asr.get("cues"):
        entries = cues_to_entries(asr["cues"])

    with _stage("03_segment_plan"):
        # 精選開啟時 entries 已是保留句（或歌詞完整句），用它算淨長再比 >30s
        net_dur = _net_dialogue_excluding_singing(entries, singing_ranges)
        duration_source = (
            "精選對白"
            if (
                enable_selective_download
                and selective_done
                and not bool(asr.get("selective_is_full"))
            )
            else (
                "完整/歌詞對白"
                if (
                    enable_selective_download
                    and selective_done
                    and bool(asr.get("selective_is_full"))
                )
                else "ASR/譯文對白"
            )
        )
        _log(
            f"  [對話淨長度] {net_dur:.2f}s（{duration_source}；"
            f"門檻 {dialogue_trim_threshold}s）；"
            f"trim={'on' if enable_dialogue_trim else 'off'}；"
            f"selective={'on' if enable_selective_download else 'off'}；"
            f"enhance={'on' if enable_enhance else 'off'}；"
            f"畫質={'來源最高' if unlimited_high_quality else f'{max_height}P 上限'}"
        )

        any_enhanced = False
        segments: list[tuple[float, float]] | None = None
        if require_subtitle_ranges and not entries and not shorts_full_analysis_range:
            raise RuntimeError(
                "Shorts 沒有可用的翻譯字幕時間軸，為避免誤抓完整高畫質影片已停止。"
            )
        # 原始純語音不足 30 秒時，Shorts 保留分析用前 9 分鐘，避免只輸出幾秒字幕。
        if shorts_full_analysis_range:
            full_end = min(
                float(analysis_limit_seconds),
                duration if duration > 0 else float(analysis_limit_seconds),
            )
            segments = [(0.0, full_end)] if full_end > 0 else None
            trimmed = full_end
            _log(
                f"  [分段] 原始純語音 {source_net_dur:.1f}s < "
                f"{dialogue_trim_threshold:.1f}s → Shorts 保留前 "
                f"{full_end / 60:.1f} 分鐘"
            )
        # 一律用「精選後（或完整）對白淨長」判斷是否 > 門檻再剪片／分段下載
        elif enable_dialogue_trim and net_dur > dialogue_trim_threshold and entries:
            segments = segment_cutter.build_continuous_segments(
                entries,
                max_gap=segment_gap,
                max_dur=duration if duration > 0 else 99999.0,
                edge_padding=edge_pad,
            )
            trimmed = sum(e - s for s, e in segments)
            _log(
                f"  [分段] 停頓≥{segment_gap}s 剪掉；"
                f"前後延伸={edge_pad}s（開關"
                f"{'ON' if enable_edge_padding else 'OFF'}）；"
                f"{duration_source} {net_dur:.1f}s "
                f"> {dialogue_trim_threshold}s → "
                f"{len(segments)} 段（約 {trimmed:.1f}s）"
            )
            if enable_three_phase_selection:
                _log(
                    "  [三段精選] 保留選段完整起訖；"
                    f"僅音訊 crossfade {THREE_PHASE_AUDIO_CROSSFADE:.2f}s"
                )
            for i, (s, e) in enumerate(segments, 1):
                _log(f"    段 {i:02d}: {s:.2f} → {e:.2f} ({e - s:.2f}s)")

        else:
            segments = None
            trimmed = duration
            if enable_dialogue_trim and entries:
                _log(
                    f"  [分段] {duration_source} {net_dur:.1f}s "
                    f"≤ {dialogue_trim_threshold}s → 不剪片／下全片"
                )

    # 第二階段：高畫質切塊下載 + Enhance。
    # 精選已先譯完時不再平行翻譯；否則翻譯與下載可同時進行。
    with _stage("02_parallel_translate_download_enhance"):
        high_path, original_srt, translated_srt, any_enhanced = (
            run_parallel_delivery_phase(
                video_url,
                work_dir,
                video_stem,
                asr,
                segments,
                enable_translation=(
                    enable_translation and not selective_done
                ),
                enable_enhance=enable_enhance,
                asr_cache=None,
                translation_path=translation_path,
                state_path=state_path,
                audio_worker=audio_worker,
                audio_crossfade_seconds=(
                    THREE_PHASE_AUDIO_CROSSFADE
                    if enable_three_phase_selection and segments is not None
                    else 0.0
                ),
            )
        )
    outcome = asr.get("outcome") or outcome

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
                web_meta["published_stage"] = pipeline_stage
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

    _update_pipeline_state(
        state_path,
        publication={
            "complete": True,
            "output": str(final_video),
            "subtitle": str(final_srt) if export_subtitles else None,
        },
    )
    if not keep_proxy:
        _cleanup_work_media_preserving_checkpoints(work_dir)

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
        help="僅對代理影片跑 ASR，寫入 --result JSON",
    )
    parser.add_argument(
        "--asr-batch",
        type=Path,
        nargs="+",
        metavar="AUDIO",
        help="對多個已分離人聲軌做 MOSS 批次 ASR，寫入 --result JSON 陣列",
    )
    parser.add_argument(
        "--asr-worker",
        action="store_true",
        help="啟動常駐 MOSS ASR worker（供主程序內部呼叫）",
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

    if args.asr_worker:
        if args.asr_only or args.asr_batch or args.result:
            print("--asr-worker 不可與其他 ASR 參數同時使用", file=sys.stderr)
            return 2
        return run_moss_asr_worker()

    if args.asr_only or args.asr_batch:
        if not args.result:
            print("--asr-only 或 --asr-batch 需要 --result", file=sys.stderr)
            return 2
        if args.asr_only and args.asr_batch:
            print("--asr-only 不能與 --asr-batch 同時使用", file=sys.stderr)
            return 2
        if args.asr_batch:
            payload: Any = run_asr_batch_local(
                [path.resolve() for path in args.asr_batch]
            )
            payload = [_safe_asr_payload(item) for item in payload]
        else:
            payload = _safe_asr_payload(run_asr_only_local(args.asr_only.resolve()))
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

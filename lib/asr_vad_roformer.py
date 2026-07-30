# -*- coding: utf-8 -*-
"""FireRed VAD/AED → Mel-Band-RoFormer → 最多三分鐘 ASR 音檔。

每個人聲段在壓縮音檔中的位置都保留映射表，MOSS 的字幕可回寫為原始
影片時間軸。GPU 以跨程序鎖保護，階段結束即釋放模型與 CUDA 快取。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

import numpy as np

from project_paths import LIB_DIR


SAMPLE_RATE = 16_000
DEFAULT_CHUNK_SECONDS = 180.0
DEFAULT_GAP_SECONDS = 0.35
DEFAULT_VAD_EDGE_PADDING_SECONDS = 0.5
_THREAD_LOCK = Lock()


@dataclass(frozen=True)
class VadRoformerAudio:
    chunks: list[dict[str, Any]]
    singing_ranges: list[tuple[float, float]]
    speech_ranges: list[tuple[float, float]]


@contextmanager
def gpu_inference_lock(label: str) -> Iterator[None]:
    """Windows 跨程序 GPU 排他鎖；VAD、RoFormer、MOSS 共用。"""
    import msvcrt

    lock_path = LIB_DIR / ".asr-gpu.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK, lock_path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(0.2)
        try:
            print(f"  [GPU 鎖] {label} 取得 GPU", flush=True)
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _release_cuda() -> None:
    try:
        import gc
        import torch

        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _merge_ranges(ranges: list[tuple[float, float]], max_gap: float = 0.4) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted((float(a), float(b)) for a, b in ranges if float(b) > float(a)):
        if merged and start <= merged[-1][1] + max_gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _extend_ranges(
    ranges: list[tuple[float, float]], duration: float, padding: float,
) -> list[tuple[float, float]]:
    """VAD 人聲段前後延伸，並在延伸後重新合併相鄰範圍。"""
    return _merge_ranges([
        (max(0.0, start - padding), min(duration, end + padding))
        for start, end in ranges
    ])


def _vad_edge_padding_seconds() -> float:
    try:
        value = float(
            os.getenv("VAD_EDGE_PADDING_SECONDS", DEFAULT_VAD_EDGE_PADDING_SECONDS)
        )
    except ValueError as exc:
        raise ValueError("VAD_EDGE_PADDING_SECONDS 必須是非負秒數") from exc
    if value < 0:
        raise ValueError("VAD_EDGE_PADDING_SECONDS 必須是非負秒數")
    return value


def _extract_mono_16k(source: Path, work_dir: Path) -> Path:
    output = work_dir / f"{source.stem}.vad-16k.wav"
    if output.is_file() and output.stat().st_size > 0:
        return output
    output.unlink(missing_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-c:a", "pcm_s16le", str(output),
        ],
        check=True,
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("FireRed VAD 前處理沒有產出有效 WAV")
    return output


def _detect_events(source: Path, work_dir: Path) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """分開卸載 VAD 與 AED，確保下一個模型才佔 VRAM。"""
    from fireredvad import FireRedAed, FireRedAedConfig, FireRedVad, FireRedVadConfig

    model_root = LIB_DIR / "fireredvad" / "model"
    if not (model_root / "VAD").is_dir() or not (model_root / "AED").is_dir():
        raise RuntimeError("找不到 FireRedVAD 模型；請先執行其安裝流程。")
    audio = _extract_mono_16k(source, work_dir)
    import soundfile as sf

    source_duration = sf.info(audio).frames / SAMPLE_RATE
    edge_padding = _vad_edge_padding_seconds()
    use_gpu = os.getenv("FIRERED_USE_GPU", "1").strip().casefold() not in {"0", "false", "no", "off"}
    with gpu_inference_lock("FireRed VAD/AED"):
        vad = FireRedVad.from_pretrained(
            str(model_root / "VAD"),
            FireRedVadConfig(
                use_gpu=use_gpu, speech_threshold=0.4, min_speech_frame=20,
                max_speech_frame=2000, min_silence_frame=20, extend_speech_frame=10,
            ),
        )
        vad_result, _ = vad.detect(str(audio))
        del vad
        _release_cuda()
        aed = FireRedAed.from_pretrained(
            str(model_root / "AED"),
            FireRedAedConfig(
                use_gpu=use_gpu, speech_threshold=0.4, singing_threshold=0.5,
                music_threshold=0.5, min_event_frame=20, max_event_frame=2000,
                min_silence_frame=20,
            ),
        )
        aed_result, _ = aed.detect(str(audio))
        del aed
        _release_cuda()
    speech = _extend_ranges(
        _merge_ranges(list(vad_result.get("timestamps") or [])),
        source_duration,
        edge_padding,
    )
    events = aed_result.get("event2timestamps") or {}
    singing = _merge_ranges(list(events.get("singing") or []))
    return speech, singing


def _time_map(compact_start: float, compact_end: float, start: float, end: float) -> dict[str, float]:
    return {
        "compact_start": round(compact_start, 6),
        "compact_end": round(compact_end, 6),
        "original_start": round(start, 6),
        "original_end": round(end, 6),
    }


def prepare_vad_roformer_audio(source: Path, work_dir: Path) -> VadRoformerAudio:
    """先合併 VAD 人聲，再對每個最多三分鐘音檔做一次 RoFormer 分離。"""
    import librosa
    import soundfile as sf
    from mel_band_roformer_vocal import SAMPLE_RATE as ROFORMER_RATE, _load_model, _resolve_device, _separate

    source = Path(source).resolve()
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_path = work_dir / "vad_roformer_manifest.json"
    expected_padding = _vad_edge_padding_seconds()
    if cache_path.is_file():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        paths = [Path(item["path"]) for item in payload.get("chunks") or []]
        if (
            payload.get("schema") == "vad_roformer_v3"
            and float(payload.get("edge_padding_seconds", -1)) == expected_padding
            and paths
            and all(path.is_file() and path.stat().st_size > 0 for path in paths)
        ):
            return VadRoformerAudio(
                chunks=list(payload["chunks"]),
                singing_ranges=[tuple(item) for item in payload.get("singing_ranges") or []],
                speech_ranges=[tuple(item) for item in payload.get("speech_ranges") or []],
            )

    speech_ranges, singing_ranges = _detect_events(source, work_dir)
    if not speech_ranges:
        return VadRoformerAudio([], singing_ranges, [])
    audio_path = _extract_mono_16k(source, work_dir)
    audio, sample_rate = sf.read(audio_path, dtype="float32")
    if sample_rate != SAMPLE_RATE:
        raise RuntimeError(f"FireRed 音檔採樣率不符：{sample_rate}")
    chunk_seconds = float(os.getenv("MOSS_CHUNK_SECONDS", DEFAULT_CHUNK_SECONDS))
    gap_seconds = float(os.getenv("VAD_COMPACT_GAP_SECONDS", DEFAULT_GAP_SECONDS))
    if chunk_seconds <= 0 or gap_seconds < 0:
        raise ValueError("MOSS_CHUNK_SECONDS 必須為正數，VAD_COMPACT_GAP_SECONDS 不可小於 0")
    output_dir = work_dir / "vad-roformer-moss"
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_chunks: list[dict[str, Any]] = []
    parts: list[np.ndarray] = []
    mapping: list[dict[str, float]] = []
    compact_duration = 0.0

    def flush() -> None:
        nonlocal parts, mapping, compact_duration
        if not parts:
            return
        path = output_dir / f"compact-{len(raw_chunks) + 1:03d}.source.wav"
        merged = np.concatenate(parts)
        sf.write(path, merged, SAMPLE_RATE, subtype="PCM_16")
        raw_chunks.append({
            "source_path": path,
            "duration": round(len(merged) / SAMPLE_RATE, 6),
            "mapping": mapping,
        })
        parts, mapping, compact_duration = [], [], 0.0

    # 先按 VAD 後時長組成最多三分鐘的原始人聲範圍；RoFormer 不在此階段
    # 逐小段載入，避免小段邊界讓分離結果與 MOSS 上下文破碎。
    for speech_start, speech_end in speech_ranges:
        start = speech_start
        while start < speech_end - 0.001:
            gap = gap_seconds if parts else 0.0
            capacity = chunk_seconds - compact_duration - gap
            if capacity <= 0.01:
                flush()
                continue
            end = min(speech_end, start + capacity)
            source_piece = audio[round(start * SAMPLE_RATE):round(end * SAMPLE_RATE)]
            if not len(source_piece):
                start = end
                continue
            if gap:
                parts.append(np.zeros(round(gap * SAMPLE_RATE), dtype=np.float32))
                compact_duration += gap
            compact_start = compact_duration
            parts.append(source_piece)
            compact_duration += len(source_piece) / SAMPLE_RATE
            mapping.append(_time_map(compact_start, compact_duration, start, end))
            start = end
    flush()

    chunks: list[dict[str, Any]] = []
    device = _resolve_device(os.getenv("ROFORMER_DEVICE", "auto").strip() or "auto")
    overlap = int(os.getenv("ROFORMER_OVERLAP", "2"))
    with gpu_inference_lock("Mel-Band-RoFormer"):
        model = _load_model(device)
        try:
            for raw_chunk in raw_chunks:
                compact, source_rate = sf.read(raw_chunk["source_path"], dtype="float32")
                if source_rate != SAMPLE_RATE:
                    raise RuntimeError(f"VAD 合併音檔採樣率不符：{source_rate}")
                stereo = librosa.resample(
                    np.repeat(compact[None, :], 2, axis=0),
                    orig_sr=SAMPLE_RATE, target_sr=ROFORMER_RATE, axis=-1,
                ).astype(np.float32, copy=False)
                vocals = _separate(model, stereo, device, overlap=overlap)
                vocals_16k = librosa.resample(
                    vocals.mean(axis=0), orig_sr=ROFORMER_RATE, target_sr=SAMPLE_RATE,
                ).astype(np.float32, copy=False)
                path = output_dir / f"speech-{len(chunks) + 1:03d}.wav"
                sf.write(path, vocals_16k, SAMPLE_RATE, subtype="PCM_16")
                chunks.append({
                    "path": str(path),
                    "duration": round(len(vocals_16k) / SAMPLE_RATE, 6),
                    "mapping": raw_chunk["mapping"],
                })
        finally:
            del model
            _release_cuda()

    payload = {
        "schema": "vad_roformer_v3",
        "edge_padding_seconds": expected_padding,
        "chunks": chunks,
        "singing_ranges": singing_ranges,
        "speech_ranges": speech_ranges,
    }
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return VadRoformerAudio(chunks, singing_ranges, speech_ranges)


def map_compact_time(value: float, mapping: list[dict[str, float]]) -> float:
    for item in mapping:
        if item["compact_start"] - 0.05 <= value <= item["compact_end"] + 0.05:
            ratio = (value - item["compact_start"]) / max(item["compact_end"] - item["compact_start"], 0.001)
            return item["original_start"] + ratio * (item["original_end"] - item["original_start"])
    previous = [item for item in mapping if item["compact_end"] <= value]
    return previous[-1]["original_end"] if previous else mapping[0]["original_start"]

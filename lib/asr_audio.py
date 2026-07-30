# -*- coding: utf-8 -*-
"""ASR 共用音訊前處理：預設以 Mel-Band-RoFormer 分離人聲。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def demucs_asr_enabled(environment: dict[str, str] | None = None) -> bool:
    environment = os.environ if environment is None else environment
    value = environment.get("ENABLE_DEMUCS_ASR", "1").strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        "ENABLE_DEMUCS_ASR 必須是 1/0、true/false、yes/no 或 on/off"
    )


def _demucs_device(torch_module) -> str:
    requested = os.getenv("DEMUCS_DEVICE", "auto").strip().casefold()
    if requested == "auto":
        return "cuda" if torch_module.cuda.is_available() else "cpu"
    if requested == "cuda" and not torch_module.cuda.is_available():
        raise RuntimeError("DEMUCS_DEVICE=cuda，但目前沒有可用 CUDA")
    if requested not in {"cuda", "cpu"}:
        raise ValueError("DEMUCS_DEVICE 只能是 auto、cuda 或 cpu")
    return requested


def _roformer_device() -> str:
    requested = os.getenv("ROFORMER_DEVICE", "auto").strip() or "auto"
    from mel_band_roformer_vocal import _resolve_device

    return _resolve_device(requested)


def _prepare_roformer_audio(source: Path, work_dir: Path) -> Path:
    """抽取 WAV 後以 Mel-Band-RoFormer 產出人聲軌。"""
    from mel_band_roformer_vocal import separate_file

    source_wav = work_dir / f"{source.stem}.asr-source.wav"
    output_dir = work_dir / "roformer"
    vocals_path = output_dir / f"{source_wav.stem}_vocals.wav"
    if not vocals_path.is_file() or vocals_path.stat().st_size == 0:
        source_wav.unlink(missing_ok=True)
        print(f"  [RoFormer] 抽取音訊 → {source_wav.name}", flush=True)
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source), "-vn", "-ac", "2", "-ar", "44100",
                "-c:a", "pcm_s16le", str(source_wav),
            ],
            check=True,
        )
        if not source_wav.is_file() or source_wav.stat().st_size == 0:
            raise RuntimeError("RoFormer 前處理沒有產出有效 WAV")
        device = _roformer_device()
        overlap = int(os.getenv("ROFORMER_OVERLAP", "2"))
        print(
            f"  [RoFormer] 分離人聲 → {vocals_path.name}（{device}）",
            flush=True,
        )
        vocals_path, _ = separate_file(source_wav, output_dir, device, overlap)
    return vocals_path


def prepare_asr_audio(source: Path, work_dir: Path) -> Path:
    """將來源媒體分離為 vocals.wav；關閉開關時原樣回傳。

    預設使用 Mel-Band-RoFormer，保留 `ASR_VOCAL_SEPARATOR=demucs` 做舊流程
    相容；`ENABLE_DEMUCS_ASR` 是既有 UI 開關名稱，語意為是否進行人聲分離。
    """
    source = Path(source).resolve()
    if not demucs_asr_enabled():
        return source
    if not source.is_file():
        raise RuntimeError(f"ASR 來源不存在：{source}")

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    separator_kind = os.getenv("ASR_VOCAL_SEPARATOR", "roformer").strip().casefold()
    if separator_kind in {"roformer", "mel-band-roformer", "melbandroformer"}:
        return _prepare_roformer_audio(source, work_dir)
    if separator_kind != "demucs":
        raise ValueError("ASR_VOCAL_SEPARATOR 只能是 roformer 或 demucs")

    try:
        import torch
        from demucs.api import Separator, save_audio
    except ImportError as exc:
        raise RuntimeError(
            "找不到 Demucs；請執行 00_setup_or_update.bat 更新依賴。"
        ) from exc

    device = _demucs_device(torch)
    model = os.getenv("DEMUCS_MODEL", "htdemucs").strip() or "htdemucs"
    try:
        shifts = max(0, int(os.getenv("DEMUCS_SHIFTS", "1")))
        overlap = float(os.getenv("DEMUCS_OVERLAP", "0.25"))
    except ValueError as exc:
        raise ValueError("DEMUCS_SHIFTS 或 DEMUCS_OVERLAP 格式錯誤") from exc
    if not 0.0 <= overlap < 1.0:
        raise ValueError("DEMUCS_OVERLAP 必須大於等於 0 且小於 1")

    output = work_dir / f"{source.stem}.asr-vocals.wav"
    temporary = output.with_name(f".{output.stem}.tmp{output.suffix}")
    output.unlink(missing_ok=True)
    temporary.unlink(missing_ok=True)

    separator = None
    try:
        print(
            f"  [Demucs] 分離人聲 → {output.name}（{model}／{device}）",
            flush=True,
        )
        separator = Separator(
            model=model,
            device=device,
            shifts=shifts,
            overlap=overlap,
        )
        _, stems = separator.separate_audio_file(str(source))
        vocals = stems.get("vocals")
        if vocals is None:
            raise RuntimeError("Demucs 輸出沒有 vocals 人聲軌")
        save_audio(
            vocals.cpu(),
            temporary,
            samplerate=separator.samplerate,
            clip="rescale",
            bits_per_sample=16,
        )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("Demucs 沒有產出有效的人聲音檔")
        temporary.replace(output)
        return output
    finally:
        temporary.unlink(missing_ok=True)
        del separator
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

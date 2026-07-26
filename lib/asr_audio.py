# -*- coding: utf-8 -*-
"""ASR 共用音訊前處理：Demucs 分離人聲後才交給辨識器。"""

from __future__ import annotations

import os
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


def prepare_asr_audio(source: Path, work_dir: Path) -> Path:
    """將來源媒體分離為 vocals.wav；關閉開關時原樣回傳。"""
    source = Path(source).resolve()
    if not demucs_asr_enabled():
        return source
    if not source.is_file():
        raise RuntimeError(f"ASR 來源不存在：{source}")

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

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
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

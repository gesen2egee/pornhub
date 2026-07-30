"""KimberleyJensen Mel-Band-Roformer 人聲分離器。

只安裝實際推理需要的相依，保留專案既有 CUDA PyTorch，避免官方完整
requirements.txt 透過 torchvision 改動 MOSS 的 PyTorch 版本。首次 --setup
會下載固定 Git commit 的推理原始碼與 Hugging Face 上 MIT 授權的權重。
"""

from __future__ import annotations

import argparse
import importlib
import sys
import urllib.request
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import numpy as np

from project_paths import LIB_DIR


SOURCE_COMMIT = "25f44ffb55ee3c301281bba21b2d6d311cb69ae2"
SOURCE_ROOT = (
    "https://raw.githubusercontent.com/KimberleyJensen/"
    f"Mel-Band-Roformer-Vocal-Model/{SOURCE_COMMIT}"
)
MODEL_REPOSITORY = "KimberleyJSN/melbandroformer"
MODEL_FILENAME = "MelBandRoformer.ckpt"
SAMPLE_RATE = 44_100
CHUNK_SAMPLES = 352_800  # 官方權重以 8 秒立體聲區塊訓練

CACHE_DIR = LIB_DIR / "mel_band_roformer_vocal"
VENDOR_DIR = CACHE_DIR / "vendor"
MODEL_DIR = CACHE_DIR / "model"
MODEL_PATH = MODEL_DIR / MODEL_FILENAME

VENDOR_FILES = (
    "models/mel_band_roformer/__init__.py",
    "models/mel_band_roformer/attend.py",
    "models/mel_band_roformer/mel_band_roformer.py",
)

MODEL_KWARGS: dict[str, Any] = {
    "dim": 384,
    "depth": 6,
    "stereo": True,
    "num_stems": 1,
    "time_transformer_depth": 1,
    "freq_transformer_depth": 1,
    "num_bands": 60,
    "dim_head": 64,
    "heads": 8,
    "attn_dropout": 0.0,
    "ff_dropout": 0.0,
    "flash_attn": True,
    "dim_freqs_in": 1025,
    "sample_rate": SAMPLE_RATE,
    "stft_n_fft": 2048,
    "stft_hop_length": 441,
    "stft_win_length": 2048,
    "stft_normalized": False,
    "mask_estimator_depth": 2,
    "multi_stft_resolution_loss_weight": 1.0,
    "multi_stft_resolutions_window_sizes": (4096, 2048, 1024, 512, 256),
    "multi_stft_hop_size": 147,
    "multi_stft_normalized": False,
}


def _download(url: str, destination: Path) -> None:
    """原子化下載固定版本的官方推理原始碼。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "pornhub-roformer-setup"})
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            while block := response.read(1024 * 1024):
                output.write(block)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_assets() -> Path:
    """下載官方程式和權重；既有完整檔案不重複下載。"""
    for relative_name in VENDOR_FILES:
        destination = VENDOR_DIR / relative_name
        if not destination.is_file():
            print(f"下載推理程式：{relative_name}", flush=True)
            _download(f"{SOURCE_ROOT}/{relative_name}", destination)

    if not MODEL_PATH.is_file() or MODEL_PATH.stat().st_size < 900_000_000:
        print("下載 MelBandRoformer.ckpt（約 871 MiB，首次僅需一次）…", flush=True)
        from huggingface_hub import hf_hub_download

        downloaded = hf_hub_download(
            repo_id=MODEL_REPOSITORY,
            filename=MODEL_FILENAME,
            local_dir=str(MODEL_DIR),
        )
        if Path(downloaded).resolve() != MODEL_PATH.resolve():
            raise RuntimeError(f"權重下載位置不符：{downloaded}")
    return MODEL_PATH


def _load_model(device: str):
    """以固定官方原始碼和權重建立模型。"""
    import torch

    model_path = ensure_assets()
    vendor_path = str(VENDOR_DIR)
    if vendor_path not in sys.path:
        sys.path.insert(0, vendor_path)
    importlib.invalidate_caches()
    from models.mel_band_roformer import MelBandRoformer

    model = MelBandRoformer(**MODEL_KWARGS)
    try:
        state_dict = torch.load(model_path, map_location="cpu", weights_only=True)
    except TypeError:  # 相容舊版 PyTorch。
        state_dict = torch.load(model_path, map_location="cpu")
    model.load_state_dict(state_dict, strict=True)
    model.eval().to(device)
    return model


def _resolve_device(requested: str) -> str:
    import torch

    if requested == "auto":
        return "cuda:0" if torch.cuda.is_available() else "cpu"
    if requested.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("指定 CUDA，但目前 VENV 沒有可用 NVIDIA CUDA。")
    return requested


def _load_stereo_audio(path: Path) -> np.ndarray:
    """讀取並重取樣為模型訓練時的 44.1 kHz 立體聲。"""
    import librosa
    import soundfile as sf

    audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    elif audio.shape[1] > 2:
        audio = audio[:, :2]
    audio = np.ascontiguousarray(audio.T)
    if sample_rate != SAMPLE_RATE:
        audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=SAMPLE_RATE, axis=-1)
    return np.ascontiguousarray(audio, dtype=np.float32)


def _separate(model, mixture: np.ndarray, device: str, overlap: int) -> np.ndarray:
    """以官方重疊區塊策略分離人聲，避免長音檔產生接縫。"""
    import torch

    if not 2 <= overlap <= 8:
        raise ValueError("--overlap 必須介於 2 到 8。")
    chunk = CHUNK_SAMPLES
    step = chunk // overlap
    fade = chunk // 10
    border = chunk - step
    mix = torch.from_numpy(mixture)
    if mix.shape[1] > 2 * border:
        mix = torch.nn.functional.pad(mix, (border, border), mode="reflect")

    total = mix.shape[1]
    result = torch.zeros_like(mix, dtype=torch.float32, device=device)
    counter = torch.zeros_like(result)
    window = torch.ones(chunk, device=device)
    window[:fade] *= torch.linspace(0, 1, fade, device=device)
    window[-fade:] *= torch.linspace(1, 0, fade, device=device)
    autocast = (
        torch.autocast(device_type="cuda", dtype=torch.float16)
        if device.startswith("cuda")
        else nullcontext()
    )

    print(f"分離 {total / SAMPLE_RATE:.1f} 秒音訊，區塊重疊={overlap}…", flush=True)
    with torch.inference_mode(), autocast:
        for start in range(0, total, step):
            part = mix[:, start : start + chunk]
            length = part.shape[1]
            if length < chunk:
                if length > chunk // 2 + 1:
                    part = torch.nn.functional.pad(part, (0, chunk - length), mode="reflect")
                else:
                    part = torch.nn.functional.pad(part, (0, chunk - length))
            vocal = model(part.unsqueeze(0).to(device))[0].float()
            local_window = window.clone()
            if start == 0:
                local_window[:fade] = 1
            if start + chunk >= total:
                local_window[-fade:] = 1
            result[:, start : start + length] += vocal[:, :length] * local_window[:length]
            counter[:, start : start + length] += local_window[:length]
    result = (result / counter.clamp_min(1e-8)).cpu().numpy()
    if mixture.shape[1] > 2 * border:
        result = result[:, border:-border]
    return np.nan_to_num(result, copy=False)


def separate_file(input_path: Path, output_dir: Path, device: str, overlap: int) -> tuple[Path, Path]:
    import soundfile as sf

    if not input_path.is_file():
        raise FileNotFoundError(f"找不到輸入音檔：{input_path}")
    model = _load_model(device)
    mixture = _load_stereo_audio(input_path)
    vocals = _separate(model, mixture, device, overlap)
    instrumental = mixture[:, : vocals.shape[1]] - vocals
    output_dir.mkdir(parents=True, exist_ok=True)
    vocals_path = output_dir / f"{input_path.stem}_vocals.wav"
    instrumental_path = output_dir / f"{input_path.stem}_instrumental.wav"
    sf.write(vocals_path, vocals.T, SAMPLE_RATE, subtype="PCM_16")
    sf.write(instrumental_path, instrumental.T, SAMPLE_RATE, subtype="PCM_16")
    return vocals_path, instrumental_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Mel-Band-Roformer 人聲／伴奏分離")
    parser.add_argument("--setup", action="store_true", help="下載固定官方原始碼與模型權重")
    parser.add_argument("--check", action="store_true", help="驗證相依與模型可載入")
    parser.add_argument("--input", type=Path, help="輸入音檔（建議 WAV）")
    parser.add_argument("--output-dir", type=Path, help="輸出資料夾")
    parser.add_argument("--device", default="auto", help="auto、cuda:0 或 cpu")
    parser.add_argument("--overlap", type=int, default=2, help="區塊重疊 2 至 8，越高越慢")
    args = parser.parse_args()

    if args.setup:
        ensure_assets()
        print(f"[OK] 模型位置：{MODEL_PATH}")
        return 0
    if args.check:
        device = _resolve_device(args.device)
        _load_model(device)
        print(f"[OK] 模型可在 {device} 載入。")
        return 0
    if args.input is None or args.output_dir is None:
        parser.error("請指定 --input 與 --output-dir，或使用 --setup / --check。")
    device = _resolve_device(args.device)
    vocals, instrumental = separate_file(args.input, args.output_dir, device, args.overlap)
    print(f"[OK] 人聲：{vocals}")
    print(f"[OK] 伴奏：{instrumental}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

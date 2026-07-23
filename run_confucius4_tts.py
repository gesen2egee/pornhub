"""使用 Confucius4-TTS 執行跨語言 zero-shot 語音合成。"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = ROOT / "confucius4_tts"
SOURCE_DIR = RUNTIME_DIR / "source"
CONFIG_PATH = SOURCE_DIR / "config" / "inference_config.yaml"
MODEL_CACHE = RUNTIME_DIR / "model-cache"
SUPPORTED_LANGUAGES = (
    "zh",
    "en",
    "ja",
    "ko",
    "de",
    "fr",
    "es",
    "id",
    "it",
    "th",
    "pt",
    "ru",
    "ms",
    "vi",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Confucius4-TTS 跨語言 zero-shot 語音合成"
    )
    parser.add_argument(
        "--prompt-wav",
        "--prompt_wav",
        dest="prompt_wav",
        type=Path,
        help="用來複製音色與情緒的參考 WAV",
    )
    parser.add_argument("--text", help="要合成的文字")
    parser.add_argument(
        "--lang",
        choices=SUPPORTED_LANGUAGES,
        default="zh",
        help="合成語言代碼（預設：zh）",
    )
    parser.add_argument(
        "--output",
        "--out",
        dest="output",
        type=Path,
        default=Path("tts_output.wav"),
        help="輸出 WAV（預設：tts_output.wav）",
    )
    parser.add_argument(
        "--device",
        choices=("cuda", "cpu", "auto"),
        default="cuda",
        help="推理裝置；auto 會在無 CUDA 時改用 CPU（預設：cuda）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只檢查安裝與 CUDA，不載入或下載模型",
    )
    return parser


def validate_request(args: argparse.Namespace) -> tuple[Path, Path]:
    if args.prompt_wav is None:
        raise ValueError("缺少 --prompt-wav 參考音訊")
    if not args.text or not args.text.strip():
        raise ValueError("缺少 --text 合成文字")

    prompt_wav = args.prompt_wav.expanduser().resolve()
    if not prompt_wav.is_file():
        raise ValueError(f"找不到參考音訊：{prompt_wav}")
    if prompt_wav.suffix.lower() != ".wav":
        raise ValueError("參考音訊必須是 WAV；可先用 FFmpeg 轉換")

    output = args.output.expanduser().resolve()
    if output.suffix.lower() != ".wav":
        raise ValueError("輸出檔名必須使用 .wav")
    return prompt_wav, output


def select_device(torch_module: Any, requested: str) -> str:
    cuda_available = bool(torch_module.cuda.is_available())
    if requested == "cuda" and not cuda_available:
        raise RuntimeError(
            "找不到可用的 CUDA；請檢查安裝，或明確傳入 --device cpu。"
        )
    if requested == "auto":
        return "cuda" if cuda_available else "cpu"
    return requested


def configure_runtime() -> None:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(
            "找不到 Confucius4-TTS，請先執行 install_confucius4_tts.bat。"
        )
    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(MODEL_CACHE))
    os.chdir(SOURCE_DIR)
    sys.path.insert(0, str(SOURCE_DIR))


def check_installation(torch_module: Any) -> int:
    from confucius4_tts_setup import EXPECTED_COMMIT, verify_source
    from confuciustts.cli.inference import ConfuciusTTS  # noqa: F401

    verify_source()
    device = select_device(torch_module, "cuda")
    print(f"安裝正常：Confucius4-TTS {EXPECTED_COMMIT[:7]}", flush=True)
    print(
        f"推理裝置：{device} / {torch_module.cuda.get_device_name(0)}",
        flush=True,
    )
    print(f"模型快取：{MODEL_CACHE}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        configure_runtime()
        import torch

        if args.check:
            return check_installation(torch)

        prompt_wav, output = validate_request(args)
        device = select_device(torch, args.device)

        import torchaudio
        from confuciustts.cli.inference import ConfuciusTTS

        print(f"載入模型（{device}），第一次執行需要下載模型…", flush=True)
        model = ConfuciusTTS(config_path=str(CONFIG_PATH), device=device)
        started_at = time.perf_counter()
        audio = model.generate(
            text=args.text.strip(),
            lang=args.lang,
            prompt_wav=str(prompt_wav),
            verbose=True,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        torchaudio.save(str(output), audio.cpu(), model.sample_rate)
        elapsed = time.perf_counter() - started_at
        duration = audio.shape[-1] / model.sample_rate
        print(
            f"完成：{output}（音訊 {duration:.2f} 秒，推理 {elapsed:.2f} 秒）",
            flush=True,
        )
        return 0
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        print(f"[錯誤] {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

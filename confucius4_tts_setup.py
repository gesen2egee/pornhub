"""驗證 Confucius4-TTS 的獨立 Windows CUDA 執行環境。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = ROOT / "confucius4_tts"
SOURCE_DIR = RUNTIME_DIR / "source"
EXPECTED_COMMIT = "186983518e9e8ab9af69cabdda3436a76d6ccdfb"


def ensure_cuda(torch_module: Any) -> str:
    """拒絕意外使用 CPU，並回傳目前 CUDA GPU 名稱。"""
    if not torch_module.cuda.is_available():
        raise RuntimeError(
            "找不到可用的 NVIDIA CUDA；請確認 NVIDIA Driver 與 CUDA 版 PyTorch。"
        )
    return str(torch_module.cuda.get_device_name(0))


def verify_source(
    source_dir: Path = SOURCE_DIR,
    expected_commit: str = EXPECTED_COMMIT,
) -> None:
    """確認官方原始碼、設定檔與固定版本皆存在。"""
    required = (
        source_dir / "config" / "inference_config.yaml",
        source_dir / "confuciustts" / "cli" / "inference.py",
        source_dir / "checkpoints" / "tokenizer.json",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("Confucius4-TTS 原始碼不完整：" + "、".join(missing))

    result = subprocess.run(
        ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    actual_commit = result.stdout.strip()
    if actual_commit != expected_commit:
        raise RuntimeError(
            f"Confucius4-TTS 版本不符：預期 {expected_commit}，實際 {actual_commit}"
        )


def main() -> int:
    import torch

    verify_source()
    gpu_name = ensure_cuda(torch)
    sys.path.insert(0, str(SOURCE_DIR))
    from confuciustts.cli.inference import ConfuciusTTS  # noqa: F401

    print(f"CUDA GPU：{gpu_name}", flush=True)
    print(f"PyTorch：{torch.__version__}", flush=True)
    print(f"Confucius4-TTS commit：{EXPECTED_COMMIT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

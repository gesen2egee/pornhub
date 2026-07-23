"""驗證 Windows CUDA 環境並下載 MOSS ModelScope snapshot。"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
MOSS_CACHE = ROOT / "moss" / "model-cache"
DEFAULT_MODEL_ID = "openmoss/MOSS-Transcribe-Diarize"


def ensure_cuda(torch_module: Any) -> str:
    """拒絕 CPU fallback，並回傳目前 CUDA GPU 名稱。"""
    if not torch_module.cuda.is_available():
        raise RuntimeError(
            "找不到可用的 NVIDIA CUDA；MOSS 安裝不會自動改用 CPU。"
        )
    return str(torch_module.cuda.get_device_name(0))


def download_snapshot(
    snapshot_download_fn: Callable[..., str],
    *,
    cache_dir: Path = MOSS_CACHE,
    model_id: str = DEFAULT_MODEL_ID,
) -> Path:
    """從 ModelScope 下載並驗證 snapshot 目錄。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = Path(
        snapshot_download_fn(model_id, cache_dir=str(cache_dir))
    )
    if not snapshot_path.is_dir():
        raise RuntimeError(f"ModelScope snapshot 不存在：{snapshot_path}")
    return snapshot_path


def main() -> int:
    import torch
    from modelscope import snapshot_download
    from transformers import AutoProcessor

    gpu_name = ensure_cuda(torch)
    model_id = os.getenv("MOSS_MODEL", DEFAULT_MODEL_ID)
    print(f"CUDA GPU：{gpu_name}", flush=True)
    print(f"下載 ModelScope 模型：{model_id}", flush=True)
    snapshot_path = download_snapshot(
        snapshot_download,
        cache_dir=MOSS_CACHE,
        model_id=model_id,
    )
    AutoProcessor.from_pretrained(
        str(snapshot_path),
        trust_remote_code=True,
    )
    print(f"MOSS snapshot 驗證完成：{snapshot_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

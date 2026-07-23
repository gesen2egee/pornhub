"""使用 Qwen3-ASR 與 ForcedAligner 產生含時間戳逐字稿。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_part, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d},{millis:03d}"


def _normalise_item(item: Any) -> dict[str, Any]:
    return {
        "text": str(getattr(item, "text", "")),
        "start_time": round(float(getattr(item, "start_time", 0.0)), 3),
        "end_time": round(float(getattr(item, "end_time", 0.0)), 3),
    }


def _make_srt(items: list[dict[str, Any]]) -> str:
    cues: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        text = "".join(part["text"] for part in current).strip()
        if text:
            cues.append(
                {
                    "start_time": current[0]["start_time"],
                    "end_time": current[-1]["end_time"],
                    "text": text,
                }
            )
        current.clear()

    for item in items:
        if not item["text"]:
            continue
        if current:
            current_text = "".join(part["text"] for part in current)
            gap = item["start_time"] - current[-1]["end_time"]
            duration = item["end_time"] - current[0]["start_time"]
            if len(current_text) >= 42 or duration >= 6.0 or gap >= 1.0:
                flush()
        current.append(item)
        if item["text"][-1:] in "。！？!?；;\n":
            flush()
    flush()

    return "\n\n".join(
        f"{index}\n{_srt_time(cue['start_time'])} --> {_srt_time(cue['end_time'])}\n{cue['text']}"
        for index, cue in enumerate(cues, start=1)
    ) + ("\n" if cues else "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--language", default=None, help="例如 Chinese；留空則自動辨識")
    parser.add_argument("--asr-model", default="Qwen/Qwen3-ASR-1.7B")
    parser.add_argument("--aligner-model", default="Qwen/Qwen3-ForcedAligner-0.6B")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    cache_dir = Path(os.environ.get("QWEN_ASR_CACHE", str(project_root / "hf-cache")))
    cache_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(cache_dir))
    os.environ.setdefault("HF_HUB_CACHE", str(cache_dir / "hub"))

    import torch
    from qwen_asr import Qwen3ASRModel

    if not torch.cuda.is_available():
        raise RuntimeError("找不到 CUDA GPU；目前腳本預期使用 NVIDIA GPU 執行。")

    print(f"使用 GPU：{torch.cuda.get_device_name(0)}", flush=True)
    print(f"載入 ASR：{args.asr_model}", flush=True)
    model = Qwen3ASRModel.from_pretrained(
        args.asr_model,
        dtype=torch.float16,
        device_map="cuda:0",
        max_inference_batch_size=1,
        max_new_tokens=2048,
        forced_aligner=args.aligner_model,
        forced_aligner_kwargs={
            "dtype": torch.float16,
            "device_map": "cuda:0",
        },
    )

    print(f"開始辨識：{args.audio}", flush=True)
    result = model.transcribe(
        audio=str(args.audio),
        language=args.language,
        return_time_stamps=True,
    )[0]

    items = [_normalise_item(item) for item in (result.time_stamps or [])]
    payload = {
        "language": result.language,
        "text": result.text,
        "time_stamps": items,
    }
    stem = args.output_dir / args.audio.stem
    json_path = stem.with_suffix(".json")
    srt_path = stem.with_suffix(".srt")
    txt_path = stem.with_suffix(".txt")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    srt_path.write_text(_make_srt(items), encoding="utf-8-sig")
    txt_path.write_text(
        f"語言：{result.language}\n\n{result.text}\n",
        encoding="utf-8",
    )
    print(f"語言：{result.language}", flush=True)
    print(f"逐字項目：{len(items)}", flush=True)
    print(f"已輸出：{srt_path}", flush=True)
    print(f"已輸出：{json_path}", flush=True)
    print(f"已輸出：{txt_path}", flush=True)


if __name__ == "__main__":
    main()

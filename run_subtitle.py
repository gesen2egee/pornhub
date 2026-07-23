"""從 low_videos 辨識字幕，翻譯後只輸出同名 SRT 到 videos。"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VIDEOS = ROOT / "videos"
TEMP_AUDIO = ROOT / "temp" / "subtitle" / "audio"
QWEN_ROOT = ROOT / "qwen-asr"
QWEN_CACHE = QWEN_ROOT / "hf-cache"

os.environ.setdefault("HF_HOME", str(QWEN_CACHE))
os.environ.setdefault("HF_HUB_CACHE", str(QWEN_CACHE / "hub"))
sys.path.insert(0, str(QWEN_ROOT))

from transcribe import _make_srt, _normalise_item  # noqa: E402
from translate_srt_openrouter import (  # noqa: E402
    DEFAULT_MODEL,
    format_srt,
    parse_srt,
    translate_cues,
)


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm"}


def _find_videos() -> list[Path]:
    candidates: list[Path] = []
    configured_dir = os.getenv("LOW_VIDEO_DIR")
    if configured_dir:
        candidates.append(Path(configured_dir))
    candidates.extend([ROOT / "low_videos", ROOT / "low_video"])
    existing_dirs = [path for path in candidates if path.exists()]
    if not existing_dirs:
        raise FileNotFoundError(
            f"找不到輸入資料夾，已檢查：{', '.join(map(str, candidates))}"
        )
    for directory in existing_dirs:
        videos = sorted(
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        )
        if videos:
            return videos
    return []


def _extract_audio(video: Path, audio: Path) -> None:
    audio.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(audio),
    ]
    subprocess.run(command, check=True)


def _load_asr_model():
    import torch
    from qwen_asr import Qwen3ASRModel

    if not torch.cuda.is_available():
        raise RuntimeError("找不到 CUDA GPU，無法使用 Qwen3-ASR。")
    print(f"使用 GPU：{torch.cuda.get_device_name(0)}", flush=True)
    print("載入 Qwen3-ASR 與 ForcedAligner（只載入一次）...", flush=True)
    return Qwen3ASRModel.from_pretrained(
        "Qwen/Qwen3-ASR-1.7B",
        dtype=torch.float16,
        device_map="cuda:0",
        max_inference_batch_size=1,
        max_new_tokens=2048,
        forced_aligner="Qwen/Qwen3-ForcedAligner-0.6B",
        forced_aligner_kwargs={
            "dtype": torch.float16,
            "device_map": "cuda:0",
        },
    )


def _audio_name(video: Path) -> str:
    digest = hashlib.sha1(str(video).encode("utf-8")).hexdigest()[:12]
    return f"{video.stem}_{digest}.wav"


def process_video(video: Path, model, api_key: str, model_name: str) -> Path:
    output_srt = VIDEOS / f"{video.stem}.srt"
    audio = TEMP_AUDIO / _audio_name(video)
    try:
        print(f"\n處理：{video.name}", flush=True)
        print("  1/3 抽取音訊", flush=True)
        _extract_audio(video, audio)
        print("  2/3 Qwen3-ASR 辨識與時間對齊", flush=True)
        result = model.transcribe(
            audio=str(audio),
            language=None,
            return_time_stamps=True,
        )[0]
        items = [_normalise_item(item) for item in (result.time_stamps or [])]
        raw_srt = _make_srt(items)
        cues = parse_srt(raw_srt)
        if not cues:
            raise RuntimeError("ASR 沒有產生有效字幕段落。")
        print(f"  語言：{result.language}；字幕段落：{len(cues)}", flush=True)
        print("  3/3 OpenRouter Grok 4.5 翻譯", flush=True)
        translated = translate_cues(cues, api_key, model_name)
        temporary_output = output_srt.with_name(output_srt.name + ".tmp")
        temporary_output.write_text(format_srt(translated), encoding="utf-8-sig")
        temporary_output.replace(output_srt)
        print(f"完成：{output_srt}", flush=True)
        return output_srt
    finally:
        if audio.exists():
            audio.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="覆寫已存在的同名 SRT")
    parser.add_argument("--limit", type=int, default=0, help="只處理前 N 部影片，0 表示全部")
    parser.add_argument("--dry-run", action="store_true", help="只列出待處理影片，不呼叫模型/API")
    args = parser.parse_args()

    videos = _find_videos()
    if not videos:
        print("輸入資料夾目前沒有可處理的影片（支援 mp4/mkv/mov/webm）。")
        return 0 if args.dry_run else 1
    pending = [
        video
        for video in videos
        if args.force or not (VIDEOS / f"{video.stem}.srt").exists()
    ]
    if args.limit > 0:
        pending = pending[: args.limit]
    print(f"輸入影片：{len(videos)} 部；待處理：{len(pending)} 部", flush=True)
    if args.dry_run:
        for video in pending:
            print(f"{video} -> {VIDEOS / (video.stem + '.srt')}")
        return 0
    if not pending:
        print("沒有需要處理的影片。")
        return 0

    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
    if not api_key:
        print("錯誤：找不到 OPENROUTER_API_KEY 環境變數。", file=sys.stderr)
        return 2
    if shutil.which("ffmpeg") is None:
        print("錯誤：找不到 FFmpeg。", file=sys.stderr)
        return 2

    VIDEOS.mkdir(parents=True, exist_ok=True)
    model = _load_asr_model()
    model_name = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
    failures = 0
    for video in pending:
        try:
            process_video(video, model, api_key, model_name)
        except Exception as exc:
            failures += 1
            print(f"失敗：{video.name}：{exc}", file=sys.stderr, flush=True)
    print(f"批次完成：成功 {len(pending) - failures} 部，失敗 {failures} 部。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

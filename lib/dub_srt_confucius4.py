"""依同名繁中 SRT，使用 Confucius4-TTS 批次替影片配音。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from project_paths import CONFUCIUS_DIR, PREVIEW_VIDEOS_DIR, TASKS_DIR
from translate_srt_openrouter import parse_srt


RUNTIME_DIR = CONFUCIUS_DIR
SOURCE_DIR = RUNTIME_DIR / "source"
CONFIG_PATH = SOURCE_DIR / "config" / "inference_config.yaml"
MODEL_CACHE = RUNTIME_DIR / "model-cache"
DEFAULT_INPUT = PREVIEW_VIDEOS_DIR / "demucs_translated"
DEFAULT_OUTPUT = PREVIEW_VIDEOS_DIR / "demucs_translated_zh"
DEFAULT_REFERENCE_MANIFEST = (
    TASKS_DIR / "demucs-moss-retry" / "status.jsonl"
)
TEMP_ROOT = TASKS_DIR / "confucius4_tts_dub"
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm"}


def timestamp_seconds(value: str) -> float:
    """將 SRT 時間戳轉成秒數。"""
    hours, minutes, remainder = value.replace(".", ",").split(":")
    seconds, milliseconds = remainder.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(milliseconds) / 1000
    )


def cue_ranges(srt_path: Path) -> list[tuple[float, float, str]]:
    """讀取有效的 SRT cue。"""
    cues = parse_srt(srt_path.read_text(encoding="utf-8-sig"))
    result = []
    for cue in cues:
        try:
            start_text, end_text = str(cue["time"]).split(" --> ", maxsplit=1)
        except ValueError as exc:
            raise ValueError(f"SRT 時間軸無效：{cue['time']!r}") from exc
        start = timestamp_seconds(start_text)
        end = timestamp_seconds(end_text)
        text = " ".join(str(cue["text"]).split())
        if text and end > start:
            result.append((start, end, text))
    return result


def load_reference_manifest(manifest_path: Path) -> dict[str, Path]:
    """從 Demucs 狀態紀錄載入影片名稱與 vocals.wav 的對應。"""
    if not manifest_path.is_file():
        raise RuntimeError(f"找不到 Demucs reference manifest：{manifest_path}")
    references: dict[str, Path] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if (
            item.get("stage") == "demucs"
            and item.get("outcome") == "ok"
            and item.get("vocals")
        ):
            references[str(item["video"])] = Path(str(item["vocals"]))
    return references


def reference_window(
    start: float,
    end: float,
    video_duration: float,
    minimum_duration: float = 5.0,
) -> tuple[float, float]:
    """保留 cue 同時段，並向前後補足可用的 reference context。"""
    cue_duration = end - start
    wanted = max(cue_duration, minimum_duration)
    center = (start + end) / 2
    window_start = max(0.0, center - wanted / 2)
    window_end = min(video_duration, window_start + wanted)
    window_start = max(0.0, window_end - wanted)
    return window_start, window_end


def atempo_filters(factor: float) -> str:
    """建立相容於 FFmpeg atempo 0.5～2.0 範圍的 filter chain。"""
    if factor <= 0:
        raise ValueError("atempo factor 必須大於 0")
    filters: list[float] = []
    while factor > 2.0:
        filters.append(2.0)
        factor /= 2.0
    while factor < 0.5:
        filters.append(0.5)
        factor /= 0.5
    filters.append(factor)
    return ",".join(f"atempo={value:.8f}" for value in filters)


def run_command(command: list[str]) -> None:
    subprocess.run(command, check=True)


def video_duration(video: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return float(result.stdout.strip())


def extract_reference(
    reference_audio: Path,
    output: Path,
    start: float,
    end: float,
) -> None:
    run_command(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{end - start:.3f}",
            "-i",
            str(reference_audio),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(output),
        ]
    )


def fit_audio(
    input_wav: Path,
    output_wav: Path,
    generated_duration: float,
    target_duration: float,
    sample_rate: int,
) -> None:
    factor = generated_duration / target_duration
    run_command(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_wav),
            "-af",
            atempo_filters(factor),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            str(output_wav),
        ]
    )


def mux_dub(video: Path, voice_track: Path, output: Path) -> None:
    filter_complex = (
        "[0:a]aresample=22050[original];"
        "[1:a]asplit=2[sidechain][voice];"
        "[original][sidechain]sidechaincompress="
        "threshold=0.015:ratio=12:attack=5:release=250[ducked];"
        "[ducked][voice]amix=inputs=2:duration=first:"
        "dropout_transition=0:normalize=0,"
        "alimiter=limit=0.95[aout]"
    )
    run_command(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-i",
            str(voice_track),
            "-filter_complex",
            filter_complex,
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )


def prepare_runtime() -> tuple[Any, Any]:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(
            "找不到 Confucius4-TTS，請先執行 install_confucius4_tts.bat。"
        )
    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(MODEL_CACHE))
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.chdir(SOURCE_DIR)
    sys.path.insert(0, str(SOURCE_DIR))

    import torch
    import torchaudio

    if not torch.cuda.is_available():
        raise RuntimeError("批次配音需要 NVIDIA CUDA。")
    return torch, torchaudio


def process_video(
    model: Any,
    torch_module: Any,
    torchaudio_module: Any,
    video: Path,
    srt_path: Path,
    reference_audio: Path,
    output_dir: Path,
    *,
    force: bool,
) -> Path:
    output = output_dir / video.name
    output_srt = output_dir / srt_path.name
    if output.exists() and not force:
        print(f"略過既有輸出：{output}", flush=True)
        return output

    ranges = cue_ranges(srt_path)
    if not ranges:
        raise RuntimeError(f"SRT 沒有有效字幕：{srt_path}")

    duration = video_duration(video)
    key = hashlib.sha1(str(video).encode("utf-8")).hexdigest()[:12]
    work_dir = TEMP_ROOT / key
    work_dir.mkdir(parents=True, exist_ok=True)
    sample_rate = int(model.sample_rate)
    full_track = torch_module.zeros(
        (1, max(1, round(duration * sample_rate))),
        dtype=torch_module.float32,
    )

    print(f"處理：{video.name}（{len(ranges)} 段）", flush=True)
    print(f"  Demucs reference：{reference_audio}", flush=True)
    for index, (start, end, text) in enumerate(ranges, start=1):
        reference = work_dir / f"{index:04d}_reference.wav"
        generated = work_dir / f"{index:04d}_generated.wav"
        fitted = work_dir / f"{index:04d}_fitted.wav"
        reference_start, reference_end = reference_window(start, end, duration)
        extract_reference(
            reference_audio,
            reference,
            reference_start,
            reference_end,
        )
        print(
            f"  {index}/{len(ranges)} {start:.2f}-{end:.2f}s：{text}",
            flush=True,
        )
        with torch_module.inference_mode():
            audio = model.generate(
                text=text,
                lang="zh",
                prompt_wav=str(reference),
                verbose=False,
            )
        audio = audio.detach().cpu()
        torchaudio_module.save(str(generated), audio, sample_rate)
        generated_duration = audio.shape[-1] / sample_rate
        fit_audio(
            generated,
            fitted,
            generated_duration,
            end - start,
            sample_rate,
        )
        fitted_audio, fitted_rate = torchaudio_module.load(str(fitted))
        if fitted_rate != sample_rate:
            fitted_audio = torchaudio_module.functional.resample(
                fitted_audio,
                fitted_rate,
                sample_rate,
            )
        if fitted_audio.shape[0] > 1:
            fitted_audio = fitted_audio.mean(dim=0, keepdim=True)
        offset = round(start * sample_rate)
        available = full_track.shape[-1] - offset
        length = min(available, fitted_audio.shape[-1])
        if length > 0:
            full_track[:, offset : offset + length] += fitted_audio[:, :length]

    peak = float(full_track.abs().max())
    if peak > 0.98:
        full_track *= 0.98 / peak
    voice_track = work_dir / "voice_track.wav"
    torchaudio_module.save(str(voice_track), full_track, sample_rate)

    output_dir.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_name(output.stem + ".tmp" + output.suffix)
    mux_dub(video, voice_track, temporary_output)
    temporary_output.replace(output)
    shutil.copy2(srt_path, output_srt)
    print(f"完成：{output}", flush=True)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用 Confucius4-TTS 將同名繁中 SRT 配音至影片"
    )
    parser.add_argument(
        "input_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_INPUT,
        help="含影片與同名 SRT 的目錄",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="輸出目錄",
    )
    parser.add_argument(
        "--reference-manifest",
        type=Path,
        default=DEFAULT_REFERENCE_MANIFEST,
        help="Demucs status.jsonl，用來對應分離人聲 WAV",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多處理幾支影片")
    parser.add_argument("--force", action="store_true", help="覆寫既有輸出")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    reference_manifest = args.reference_manifest.expanduser().resolve()
    if not input_dir.is_dir():
        print(f"[錯誤] 找不到輸入目錄：{input_dir}", file=sys.stderr)
        return 2

    videos = sorted(
        path
        for path in input_dir.iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
    )
    try:
        references = load_reference_manifest(reference_manifest)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"[錯誤] {exc}", file=sys.stderr)
        return 2
    pairs = [
        (video, video.with_suffix(".srt"), references.get(video.name))
        for video in videos
    ]
    pairs = [
        (video, srt, reference)
        for video, srt, reference in pairs
        if srt.is_file() and reference is not None and reference.is_file()
    ]
    if args.limit > 0:
        pairs = pairs[: args.limit]
    if not pairs:
        print("[錯誤] 找不到具有同名 SRT 的影片。", file=sys.stderr)
        return 2

    try:
        torch_module, torchaudio_module = prepare_runtime()
        from confuciustts.cli.inference import ConfuciusTTS

        print(f"載入 Confucius4-TTS，共 {len(pairs)} 支影片…", flush=True)
        model = ConfuciusTTS(config_path=str(CONFIG_PATH), device="cuda")
        for video, srt_path, reference_audio in pairs:
            process_video(
                model,
                torch_module,
                torchaudio_module,
                video,
                srt_path,
                reference_audio,
                output_dir,
                force=args.force,
            )
        return 0
    except (ImportError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[錯誤] {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

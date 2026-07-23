"""Whisper 與 MOSS 的統一 ASR backend 介面。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
WHISPER_CACHE = ROOT / "whisper" / "model-cache"
MOSS_CACHE = ROOT / "moss" / "model-cache"
DEFAULT_MOSS_MODEL = "openmoss/MOSS-Transcribe-Diarize"
DEFAULT_MOSS_PROMPT = (
    "請將音訊轉寫為文字，每一段需以起始時間戳和說話人編號"
    "（[S01]、[S02]、[S03]…）開頭，正文為對應的語音內容，"
    "並在段末標註結束時間戳，以清晰標明該段語音範圍。"
)


def resolve_backend(environment: Mapping[str, str] | None = None) -> str:
    """解析 ASR backend；未設定時預設使用 MOSS。"""
    environment = os.environ if environment is None else environment
    backend = environment.get("ASR_BACKEND", "moss").strip().lower() or "moss"
    if backend not in {"whisper", "moss"}:
        raise ValueError("ASR_BACKEND 只允許 whisper、moss。")
    return backend


def srt_time(seconds: float) -> str:
    """把秒數轉成 SRT 時間格式。"""
    milliseconds = round(float(seconds) * 1000)
    if milliseconds < 0:
        raise ValueError("時間戳不可小於 0。")
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_part, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d},{millis:03d}"


def moss_segments_to_cues(segments: list[Any]) -> list[dict[str, Any]]:
    """把 MOSS 官方 parser segments 轉成共用 SRT cue。"""
    cues: list[dict[str, Any]] = []
    for index, segment in enumerate(segments, start=1):
        start = float(segment.start)
        end = float(segment.end)
        speaker = str(segment.speaker).strip()
        text = str(segment.text).strip()
        if start < 0 or end <= start or not speaker:
            raise ValueError(f"MOSS segment 無效：{segment!r}")
        if not text:
            continue
        if not speaker.startswith("["):
            speaker = f"[{speaker}]"
        cues.append(
            {
                "id": index,
                "time": f"{srt_time(start)} --> {srt_time(end)}",
                "text": f"{speaker} {text}",
            }
        )
    if not cues:
        raise RuntimeError("MOSS 沒有產生有效字幕段落。")
    return cues


def build_moss_prompt(environment: Mapping[str, str] | None = None) -> str:
    """建立官方轉錄提示，並選擇性附加 hotwords。"""
    environment = os.environ if environment is None else environment
    hotwords = [
        item.strip()
        for item in environment.get("MOSS_HOTWORDS", "").split(",")
        if item.strip()
    ]
    if not hotwords:
        return DEFAULT_MOSS_PROMPT
    return f"{DEFAULT_MOSS_PROMPT}熱詞提示：{', '.join(hotwords)}"


class WhisperBackend:
    """保留既有 faster-whisper 推理參數的 adapter。"""

    name = "whisper"
    display_name = "faster-whisper"

    def __init__(self) -> None:
        self.model: Any | None = None

    def load(self) -> "WhisperBackend":
        from faster_whisper import WhisperModel

        model_name = os.getenv("WHISPER_MODEL", "large-v3")
        device = os.getenv("WHISPER_DEVICE", "cuda")
        compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "float16")
        print(
            f"載入 Whisper：{model_name}，device={device}，"
            f"compute_type={compute_type}",
            flush=True,
        )
        self.model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            download_root=str(WHISPER_CACHE),
        )
        return self

    def transcribe(self, video: Path) -> tuple[list[dict[str, Any]], str]:
        if self.model is None:
            raise RuntimeError("Whisper backend 尚未載入。")
        language = os.getenv("WHISPER_LANGUAGE") or None
        options: dict[str, Any] = {"beam_size": 5, "vad_filter": True}
        if language:
            options["language"] = language
        segments, info = self.model.transcribe(str(video), **options)
        cues: list[dict[str, Any]] = []
        for index, segment in enumerate(segments, start=1):
            text = segment.text.strip()
            if not text:
                continue
            cues.append(
                {
                    "id": index,
                    "time": (
                        f"{srt_time(segment.start)} --> {srt_time(segment.end)}"
                    ),
                    "text": text,
                }
            )
        return cues, getattr(info, "language", "unknown")


class MossBackend:
    """以 ModelScope snapshot 執行 MOSS-Transcribe-Diarize。"""

    name = "moss"
    display_name = "MOSS-Transcribe-Diarize"

    def __init__(self, torch_module: Any | None = None) -> None:
        self._torch = torch_module
        self.model: Any | None = None
        self.processor: Any | None = None
        self.device: Any | None = None
        self.dtype: Any | None = None
        self._build_messages: Any | None = None
        self._generate: Any | None = None
        self._parse: Any | None = None

    def load(self) -> "MossBackend":
        if self._torch is None:
            import torch

            self._torch = torch
        if not self._torch.cuda.is_available():
            raise RuntimeError(
                "MOSS backend 需要 NVIDIA CUDA，不會自動退回 CPU 或 Whisper。"
            )

        from modelscope import snapshot_download
        from moss_transcribe_diarize import parse_transcript
        from moss_transcribe_diarize.inference_utils import (
            build_transcription_messages,
            generate_transcription,
        )
        from transformers import AutoModelForCausalLM, AutoProcessor

        model_id = os.getenv("MOSS_MODEL", DEFAULT_MOSS_MODEL)
        model_dir = snapshot_download(model_id, cache_dir=str(MOSS_CACHE))
        self.device = self._torch.device(os.getenv("MOSS_DEVICE", "cuda:0"))
        dtype_name = os.getenv("MOSS_DTYPE", "bfloat16").strip().lower()
        dtype_table = {
            "bfloat16": self._torch.bfloat16,
            "bf16": self._torch.bfloat16,
            "float16": self._torch.float16,
            "fp16": self._torch.float16,
        }
        if dtype_name not in dtype_table:
            raise ValueError("MOSS_DTYPE 只允許 bfloat16、bf16、float16、fp16。")
        self.dtype = dtype_table[dtype_name]
        print(
            f"載入 MOSS：{model_id}，device={self.device}，dtype={dtype_name}",
            flush=True,
        )
        self.model = AutoModelForCausalLM.from_pretrained(
            model_dir,
            trust_remote_code=True,
            dtype="auto",
        ).to(dtype=self.dtype).to(self.device).eval()
        self.processor = AutoProcessor.from_pretrained(
            model_dir,
            trust_remote_code=True,
        )
        self._build_messages = build_transcription_messages
        self._generate = generate_transcription
        self._parse = parse_transcript
        return self

    def transcribe(self, video: Path) -> tuple[list[dict[str, Any]], str]:
        if (
            self.model is None
            or self.processor is None
            or self._build_messages is None
            or self._generate is None
            or self._parse is None
        ):
            raise RuntimeError("MOSS backend 尚未載入。")
        try:
            max_new_tokens = int(os.getenv("MOSS_MAX_NEW_TOKENS", "65536"))
        except ValueError as exc:
            raise ValueError("MOSS_MAX_NEW_TOKENS 必須是整數。") from exc
        if max_new_tokens <= 0:
            raise ValueError("MOSS_MAX_NEW_TOKENS 必須大於 0。")
        messages = self._build_messages(video, prompt=build_moss_prompt())
        result = self._generate(
            self.model,
            self.processor,
            messages,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            device=self.device,
            dtype=self.dtype,
        )
        segments = list(self._parse(result["text"]))
        return moss_segments_to_cues(segments), "multilingual"


def create_backend(
    environment: Mapping[str, str] | None = None,
) -> WhisperBackend | MossBackend:
    """建立但不載入 backend，讓 dry-run 不觸發模型依賴。"""
    backend = resolve_backend(environment)
    if backend == "moss":
        return MossBackend()
    return WhisperBackend()

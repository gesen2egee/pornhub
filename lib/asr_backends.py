"""ASR backends：MOSS / Whisper 本機，以及 OpenRouter Voxtral / Grok STT 雲端轉寫。"""

from __future__ import annotations

import base64
import concurrent.futures
import copy
import gc
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from project_paths import LIB_DIR, MOSS_DIR


MOSS_CACHE = MOSS_DIR / "model-cache"
WHISPER_CACHE = LIB_DIR / "whisper" / "model-cache"
DEFAULT_MOSS_MODEL = "openmoss/MOSS-Transcribe-Diarize"
# 12GB 卡：3 分鐘 chunk 預設一次 3 段（約 3G×3；OOM 自動降 batch）。
DEFAULT_ASR_BATCH_SIZE = 3
DEFAULT_ASR_BACKEND = "moss"
DEFAULT_VOXTRAL_MODEL = "mistralai/voxtral-mini-transcribe"
DEFAULT_GROK_STT_MODEL = "x-ai/grok-stt-1.0"
DEFAULT_WHISPER_MODEL = "large-v3"
OPENROUTER_TRANSCRIPTIONS_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
DEFAULT_MOSS_PROMPT = (
    "請將音訊轉寫為文字，每一段需以起始時間戳和說話人編號"
    "（[S01]、[S02]、[S03]…）開頭，正文為對應的語音內容，"
    "並在段末標註結束時間戳，以清晰標明該段語音範圍。"
)

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


def asr_batch_size(environment: Mapping[str, str] | None = None) -> int:
    """一次並行的 ASR 分段數（HF batch）。預設 3；可用 MOSS_ASR_BATCH_SIZE 覆寫。"""
    environment = os.environ if environment is None else environment
    try:
        value = int(environment.get("MOSS_ASR_BATCH_SIZE", str(DEFAULT_ASR_BATCH_SIZE)))
    except ValueError:
        value = DEFAULT_ASR_BATCH_SIZE
    return max(1, value)


def moss_max_new_tokens(environment: Mapping[str, str] | None = None) -> int:
    environment = os.environ if environment is None else environment
    try:
        # 預設對齊 3 分鐘 chunk：官方 CLI 常用 2048，3 分密講話約 2k–4k。
        value = int(environment.get("MOSS_MAX_NEW_TOKENS", "4096"))
    except ValueError as exc:
        raise ValueError("MOSS_MAX_NEW_TOKENS 必須是整數。") from exc
    if value <= 0:
        raise ValueError("MOSS_MAX_NEW_TOKENS 必須大於 0。")
    return value


class WhisperBackend:
    """歷史 faster-whisper backend（git c4967e0 / d09353a）。

    環境變數：
      WHISPER_MODEL=large-v3
      WHISPER_DEVICE=cuda
      WHISPER_COMPUTE_TYPE=float16
      WHISPER_LANGUAGE=en（可選）
    """

    name = "whisper"
    display_name = "faster-whisper"

    def __init__(self) -> None:
        self.model: Any | None = None

    def load(self) -> "WhisperBackend":
        from faster_whisper import WhisperModel

        model_name = os.getenv("WHISPER_MODEL", DEFAULT_WHISPER_MODEL)
        device = os.getenv("WHISPER_DEVICE", "cuda")
        compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "float16")
        WHISPER_CACHE.mkdir(parents=True, exist_ok=True)
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
                        f"{srt_time(segment.start)} --> "
                        f"{srt_time(segment.end)}"
                    ),
                    "text": text,
                }
            )
        return cues, str(getattr(info, "language", "unknown") or "unknown")

    def transcribe_batch(
        self,
        videos: Sequence[Path],
    ) -> list[tuple[list[dict[str, Any]], str]]:
        # faster-whisper 單模型串行即可；外部仍可一次丟多段
        return [self.transcribe(path) for path in videos]

    def release_transient_memory(self) -> None:
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


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
                "MOSS 需要 NVIDIA CUDA，不會自動退回 CPU。"
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

    def _ensure_ready(self) -> None:
        if (
            self.model is None
            or self.processor is None
            or self._build_messages is None
            or self._generate is None
            or self._parse is None
        ):
            raise RuntimeError("MOSS backend 尚未載入。")

    def transcribe(self, video: Path) -> tuple[list[dict[str, Any]], str]:
        return self.transcribe_batch([video])[0]

    def transcribe_batch(
        self,
        videos: Sequence[Path],
    ) -> list[tuple[list[dict[str, Any]], str]]:
        """一次 generate 多段音訊（真正的 HF batch，非多進程）。

        processor 支援 list[text]+list[audio]；解碼端用左 padding 以利 batched generate。
        """
        self._ensure_ready()
        paths = [Path(item) for item in videos]
        if not paths:
            return []
        if len(paths) == 1:
            messages = self._build_messages(paths[0], prompt=build_moss_prompt())
            result = self._generate(
                self.model,
                self.processor,
                messages,
                max_new_tokens=moss_max_new_tokens(),
                do_sample=False,
                device=self.device,
                dtype=self.dtype,
            )
            segments = list(self._parse(result["text"]))
            return [(moss_segments_to_cues(segments), "multilingual")]

        return self._transcribe_batch_many(paths)

    def _transcribe_batch_many(
        self,
        paths: list[Path],
    ) -> list[tuple[list[dict[str, Any]], str]]:
        """Batched prepare + generate；輸出順序與 paths 相同。"""
        from moss_transcribe_diarize.inference_utils import process_audio_info

        assert self.model is not None
        assert self.processor is not None
        assert self._parse is not None
        torch = self._torch
        assert torch is not None

        max_new_tokens = moss_max_new_tokens()
        prompt = build_moss_prompt()
        sampling_rate = int(self.processor.feature_extractor.sampling_rate)
        texts: list[str] = []
        audios: list[Any] = []
        for path in paths:
            messages = self._build_messages(path, prompt=prompt)
            text = self.processor.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            loaded = process_audio_info(messages, sampling_rate=sampling_rate)
            if not loaded:
                raise RuntimeError(f"無法載入音訊：{path}")
            texts.append(text)
            audios.append(loaded[0])

        device = self.device
        dtype = self.dtype
        context = (
            torch.amp.autocast("cuda", dtype=dtype)
            if device is not None
            and device.type == "cuda"
            and dtype in (torch.float16, torch.bfloat16)
            else torch.no_grad()
        )
        with context:
            # 先用 processor 右側 pad 建 features，再改成左側 pad 以利 decoder batch
            raw = self.processor(
                text=texts,
                audio=audios,
                max_length=131072,
                return_tensors="pt",
            )
            input_ids = raw["input_ids"]
            attention_mask = raw["attention_mask"]
            # 轉左 padding
            batch_size, seq_len = input_ids.shape
            pad_id = self.processor.tokenizer.pad_token_id
            if pad_id is None:
                pad_id = self.processor.tokenizer.eos_token_id or 0
            left_ids = input_ids.new_full((batch_size, seq_len), int(pad_id))
            left_mask = attention_mask.new_zeros((batch_size, seq_len))
            for index in range(batch_size):
                valid = int(attention_mask[index].sum().item())
                if valid <= 0:
                    continue
                left_ids[index, -valid:] = input_ids[index, :valid]
                left_mask[index, -valid:] = 1
            inputs = {
                "input_ids": left_ids.to(device),
                "attention_mask": left_mask.to(device),
                "input_features": raw["input_features"].to(device),
                "audio_feature_lengths": raw["audio_feature_lengths"].to(device),
                "audio_chunk_mapping": raw["audio_chunk_mapping"].to(device),
            }

        generation_config = copy.deepcopy(self.model.generation_config)
        generation_config.max_new_tokens = max_new_tokens
        generation_config.do_sample = False

        with torch.inference_mode(), (
            torch.amp.autocast("cuda", dtype=dtype)
            if device is not None
            and device.type == "cuda"
            and dtype in (torch.float16, torch.bfloat16)
            else torch.no_grad()
        ):
            outputs = self.model.generate(
                input_ids=inputs["input_ids"],
                attention_mask=inputs["attention_mask"],
                input_features=inputs["input_features"],
                audio_feature_lengths=inputs["audio_feature_lengths"],
                audio_chunk_mapping=inputs["audio_chunk_mapping"],
                generation_config=generation_config,
            )

        prompt_width = int(inputs["input_ids"].shape[1])
        results: list[tuple[list[dict[str, Any]], str]] = []
        for index in range(len(paths)):
            generated = outputs[index][prompt_width:]
            text = self.processor.tokenizer.decode(
                generated,
                skip_special_tokens=True,
            ).strip()
            segments = list(self._parse(text))
            results.append((moss_segments_to_cues(segments), "multilingual"))
        return results

    def release_transient_memory(self) -> None:
        """保留模型但釋放每個 ASR 分段產生的暫存 CPU／CUDA cache。"""
        gc.collect()
        if self._torch is not None and self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()


def _probe_audio_duration(path: Path) -> float:
    """用 ffprobe 取音訊長度；失敗時回傳 0。"""
    try:
        result = subprocess.run(
            [
                os.getenv("FFPROBE_EXE", "ffprobe"),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        value = float((result.stdout or "").strip())
        return value if value > 0 else 0.0
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        return 0.0


def plain_transcript_to_cues(
    text: str,
    duration: float,
) -> list[dict[str, Any]]:
    """把無時間軸的轉寫切成 cues（不需 [S01]）。

    依句號／換行切段，再依字元比例攤時間；單句則整段 0→duration。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    duration = max(0.5, float(duration) if duration and duration > 0 else 0.5)
    parts = [
        piece.strip()
        for piece in re.split(r"(?<=[.!?。！？…])\s+|\n+", cleaned)
        if piece and piece.strip()
    ]
    if not parts:
        parts = [cleaned]
    if len(parts) == 1:
        return [
            {
                "id": 1,
                "time": f"{srt_time(0.0)} --> {srt_time(duration)}",
                "text": parts[0],
            }
        ]
    total_chars = sum(max(1, len(part)) for part in parts)
    cues: list[dict[str, Any]] = []
    cursor = 0.0
    for index, part in enumerate(parts, start=1):
        share = max(1, len(part)) / total_chars
        seg = max(0.4, duration * share)
        end = duration if index == len(parts) else min(duration, cursor + seg)
        if end <= cursor:
            end = min(duration, cursor + 0.4)
        cues.append(
            {
                "id": index,
                "time": f"{srt_time(cursor)} --> {srt_time(end)}",
                "text": part,
            }
        )
        cursor = end
    return cues


def segments_json_to_cues(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI verbose_json segments → 共用 cue 格式（無 speaker）。"""
    cues: list[dict[str, Any]] = []
    for index, segment in enumerate(segments, start=1):
        try:
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))
        except (TypeError, ValueError):
            continue
        text = str(segment.get("text") or "").strip()
        if not text or end <= start:
            continue
        cues.append(
            {
                "id": index,
                "time": f"{srt_time(start)} --> {srt_time(end)}",
                "text": text,
            }
        )
    return cues


def words_to_cues(
    words: list[dict[str, Any]],
    *,
    pause_hard: float = 0.50,
    pause_soft: float = 0.30,
    max_dur: float = 6.0,
    max_chars: int = 72,
    max_words: int = 14,
    min_chars: int = 24,
    min_dur: float = 0.9,
) -> list[dict[str, Any]]:
    """字級時間軸 → SRT cues（停頓 + 標點 + 長度上限）。

    優先使用 Grok STT / Whisper 的 words[]；segments 過粗時不要用。
    """
    normalized: list[dict[str, float | str]] = []
    for item in words:
        if not isinstance(item, Mapping):
            continue
        token = str(item.get("word") or item.get("text") or "").strip()
        if not token:
            continue
        try:
            start = float(item.get("start", 0.0))
            end = float(item.get("end", start))
        except (TypeError, ValueError):
            continue
        if end < start:
            end = start
        normalized.append({"word": token, "start": start, "end": end})

    if not normalized:
        return []

    cues: list[dict[str, Any]] = []
    buf: list[dict[str, float | str]] = []

    def _buf_chars() -> int:
        if not buf:
            return 0
        return sum(len(str(w["word"])) for w in buf) + max(0, len(buf) - 1)

    def _flush() -> None:
        if not buf:
            return
        text = " ".join(str(w["word"]) for w in buf).strip()
        start = float(buf[0]["start"])
        end = float(buf[-1]["end"])
        if end - start < min_dur:
            end = start + min_dur
        if end <= start:
            end = start + 0.4
        cues.append(
            {
                "id": len(cues) + 1,
                "time": f"{srt_time(start)} --> {srt_time(end)}",
                "text": text,
            }
        )
        buf.clear()

    def _should_break(prev: dict[str, float | str], cur: dict[str, float | str]) -> bool:
        gap = float(cur["start"]) - float(prev["end"])
        text_len = _buf_chars()
        n = len(buf)
        dur = float(prev["end"]) - float(buf[0]["start"])
        ends = str(prev["word"]).rstrip()

        if gap >= pause_hard:
            return True
        if ends.endswith((".", "?", "!", "…", "。", "？", "！")) and text_len >= min_chars:
            return True
        if (
            ends.endswith((",", ";", ":", "，", "；", "："))
            and text_len >= min_chars
            and gap >= pause_soft
        ):
            return True
        if n >= max_words or text_len >= max_chars or dur >= max_dur:
            return True
        if (
            gap >= pause_soft
            and text_len >= min_chars
            and (
                ends.endswith((",", ";", ":", "，", "；", "："))
                or n >= 8
            )
        ):
            return True
        return False

    for word in normalized:
        if not buf:
            buf.append(word)
            continue
        if _should_break(buf[-1], word):
            _flush()
        buf.append(word)
    _flush()

    # 修正相鄰 cue 重疊（min_dur padding 可能造成）
    for index in range(len(cues) - 1):
        left = cues[index]
        right = cues[index + 1]
        try:
            left_start_s, left_end_s = left["time"].split(" --> ")
            right_start_s, right_end_s = right["time"].split(" --> ")
        except ValueError:
            continue
        # 僅在需要時用秒數重算；用簡單字串比對不夠，改解析
        def _parse(ts: str) -> float:
            hh, mm, rest = ts.split(":")
            ss, ms = rest.split(",")
            return (
                int(hh) * 3600
                + int(mm) * 60
                + int(ss)
                + int(ms) / 1000.0
            )

        left_end = _parse(left_end_s)
        right_start = _parse(right_start_s)
        left_start = _parse(left_start_s)
        if left_end > right_start - 0.02:
            left_end = max(left_start + 0.2, right_start - 0.02)
            left["time"] = f"{srt_time(left_start)} --> {srt_time(left_end)}"

    return cues


class VoxtralOpenRouterBackend:
    """OpenRouter `/audio/transcriptions` + Voxtral Mini Transcribe。

    格式：
      POST https://openrouter.ai/api/v1/audio/transcriptions
      {
        "model": "mistralai/voxtral-mini-transcribe",
        "input_audio": {"data": "<base64>", "format": "wav"},
        "language": "en"  # 可選
      }
    回傳：{"text": "...", "usage": {...}}
    不需 [S01]/[S02]；時間軸優先 verbose_json.segments，否則依句字比例攤。
    """

    name = "voxtral"
    display_name = "Voxtral Mini Transcribe (OpenRouter)"

    def __init__(self) -> None:
        self.api_key: str | None = None
        self.model: str = DEFAULT_VOXTRAL_MODEL
        self.language: str | None = None

    def load(self) -> "VoxtralOpenRouterBackend":
        self.api_key = (
            os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
        )
        if not self.api_key:
            raise RuntimeError(
                "Voxtral ASR 需要 OPENROUTER_API_KEY 環境變數。"
            )
        self.model = os.getenv("VOXTRAL_MODEL", DEFAULT_VOXTRAL_MODEL).strip()
        language = os.getenv("ASR_LANGUAGE", "").strip()
        self.language = language or None
        print(
            f"載入 ASR：{self.display_name}，model={self.model}",
            flush=True,
        )
        return self

    def _request_transcription(self, audio_path: Path) -> dict[str, Any]:
        import requests

        if not self.api_key:
            raise RuntimeError("Voxtral backend 尚未 load。")
        raw = audio_path.read_bytes()
        if not raw:
            raise RuntimeError(f"音訊檔是空的：{audio_path}")
        # 25MB multipart 上限；JSON base64 可更大，但仍避免過大
        if len(raw) > 20 * 1024 * 1024:
            raise RuntimeError(
                f"音訊過大（{len(raw) / 1024 / 1024:.1f} MB），請縮短 ASR 切段。"
            )
        suffix = audio_path.suffix.lstrip(".").lower() or "wav"
        payload: dict[str, Any] = {
            "model": self.model,
            "input_audio": {
                "data": base64.b64encode(raw).decode("ascii"),
                "format": suffix,
            },
        }
        if self.language:
            payload["language"] = self.language
        # 若 provider 支援 verbose_json 可拿到 segments；Mistral 可能 400，下面會降級
        payload["response_format"] = "verbose_json"

        response = requests.post(
            OPENROUTER_TRANSCRIPTIONS_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/local/pornhub-pipeline",
                "X-Title": "pornhub-subtitle-pipeline",
            },
            data=json.dumps(payload),
            timeout=int(os.getenv("VOXTRAL_TIMEOUT_SECONDS", "120")),
        )
        # Mistral 等非 OpenAI 相容 provider 可能拒 verbose_json → 降級純 text
        if (
            response.status_code >= 400
            and payload.get("response_format") == "verbose_json"
        ):
            payload.pop("response_format", None)
            response = requests.post(
                OPENROUTER_TRANSCRIPTIONS_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/local/pornhub-pipeline",
                    "X-Title": "pornhub-subtitle-pipeline",
                },
                data=json.dumps(payload),
                timeout=int(os.getenv("VOXTRAL_TIMEOUT_SECONDS", "120")),
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenRouter 轉寫失敗 HTTP {response.status_code}："
                f"{(response.text or '')[:500]}"
            )
        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError(
                f"OpenRouter 回傳非 JSON：{(response.text or '')[:300]}"
            ) from exc

    def _result_to_cues(
        self,
        payload: dict[str, Any],
        audio_path: Path,
    ) -> tuple[list[dict[str, Any]], str]:
        language = str(payload.get("language") or "multilingual")
        segments = payload.get("segments")
        if isinstance(segments, list) and segments:
            cues = segments_json_to_cues(segments)
            if cues:
                return cues, language
        text = str(payload.get("text") or "").strip()
        duration = float(payload.get("duration") or 0.0)
        if duration <= 0:
            usage = payload.get("usage") or {}
            try:
                duration = float(usage.get("seconds") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
        if duration <= 0:
            duration = _probe_audio_duration(audio_path)
        return plain_transcript_to_cues(text, duration), language

    def transcribe(self, video: Path) -> tuple[list[dict[str, Any]], str]:
        path = Path(video)
        print(f"  [Voxtral] 轉寫 {path.name}", flush=True)
        payload = self._request_transcription(path)
        return self._result_to_cues(payload, path)

    def transcribe_batch(
        self,
        videos: Sequence[Path],
    ) -> list[tuple[list[dict[str, Any]], str]]:
        """雲端並行（HTTP），預設同時 3 段。"""
        paths = [Path(item) for item in videos]
        if not paths:
            return []
        if len(paths) == 1:
            return [self.transcribe(paths[0])]

        workers = min(len(paths), asr_batch_size())
        print(
            f"  [Voxtral] 並行轉寫 {len(paths)} 段（workers={workers}）",
            flush=True,
        )
        results: list[tuple[list[dict[str, Any]], str] | None] = [
            None
        ] * len(paths)

        def _one(index: int, path: Path) -> tuple[int, list[dict[str, Any]], str]:
            cues, language = self.transcribe(path)
            return index, cues, language

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_one, index, path)
                for index, path in enumerate(paths)
            ]
            for future in concurrent.futures.as_completed(futures):
                index, cues, language = future.result()
                results[index] = (cues, language)

        ordered: list[tuple[list[dict[str, Any]], str]] = []
        for index, item in enumerate(results):
            if item is None:
                raise RuntimeError(f"Voxtral batch 缺第 {index} 段結果")
            ordered.append(item)
        return ordered

    def release_transient_memory(self) -> None:
        return None


class GrokSTTOpenRouterBackend:
    """OpenRouter `/audio/transcriptions` + x-ai/grok-stt-1.0。

    verbose_json 會給 words[]（字級時間軸）與粗 segments[]。
    字幕 cue 優先 words_to_cues；無 words 時才退回 segments / 全文比例攤。
    """

    name = "grok-stt"
    display_name = "Grok STT 1.0 (OpenRouter)"

    def __init__(self) -> None:
        self.api_key: str | None = None
        self.model: str = DEFAULT_GROK_STT_MODEL
        self.language: str | None = None

    def load(self) -> "GrokSTTOpenRouterBackend":
        self.api_key = (
            os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
        )
        if not self.api_key:
            raise RuntimeError(
                "Grok STT 需要 OPENROUTER_API_KEY 環境變數。"
            )
        self.model = os.getenv("GROK_STT_MODEL", DEFAULT_GROK_STT_MODEL).strip()
        language = os.getenv("ASR_LANGUAGE", "").strip()
        self.language = language or None
        print(
            f"載入 ASR：{self.display_name}，model={self.model}",
            flush=True,
        )
        return self

    def _request_transcription(self, audio_path: Path) -> dict[str, Any]:
        import requests

        if not self.api_key:
            raise RuntimeError("Grok STT backend 尚未 load。")
        raw = audio_path.read_bytes()
        if not raw:
            raise RuntimeError(f"音訊檔是空的：{audio_path}")
        if len(raw) > 20 * 1024 * 1024:
            raise RuntimeError(
                f"音訊過大（{len(raw) / 1024 / 1024:.1f} MB），請縮短 ASR 切段。"
            )
        suffix = audio_path.suffix.lstrip(".").lower() or "wav"
        payload: dict[str, Any] = {
            "model": self.model,
            "input_audio": {
                "data": base64.b64encode(raw).decode("ascii"),
                "format": suffix,
            },
            "response_format": "verbose_json",
            "timestamp_granularities": ["word", "segment"],
        }
        if self.language:
            payload["language"] = self.language

        response = requests.post(
            OPENROUTER_TRANSCRIPTIONS_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/local/pornhub-pipeline",
                "X-Title": "pornhub-subtitle-pipeline",
            },
            data=json.dumps(payload),
            timeout=int(os.getenv("GROK_STT_TIMEOUT_SECONDS", "180")),
        )
        if (
            response.status_code >= 400
            and payload.get("response_format") == "verbose_json"
        ):
            # 降級：去掉 granularities 再試
            payload.pop("timestamp_granularities", None)
            response = requests.post(
                OPENROUTER_TRANSCRIPTIONS_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/local/pornhub-pipeline",
                    "X-Title": "pornhub-subtitle-pipeline",
                },
                data=json.dumps(payload),
                timeout=int(os.getenv("GROK_STT_TIMEOUT_SECONDS", "180")),
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenRouter Grok STT 失敗 HTTP {response.status_code}："
                f"{(response.text or '')[:500]}"
            )
        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError(
                f"OpenRouter 回傳非 JSON：{(response.text or '')[:300]}"
            ) from exc

    def _result_to_cues(
        self,
        payload: dict[str, Any],
        audio_path: Path,
    ) -> tuple[list[dict[str, Any]], str]:
        language = str(payload.get("language") or "multilingual")
        words = payload.get("words")
        if isinstance(words, list) and words:
            cues = words_to_cues(words)
            if cues:
                return cues, language
        segments = payload.get("segments")
        if isinstance(segments, list) and segments:
            cues = segments_json_to_cues(segments)
            if cues:
                return cues, language
        text = str(payload.get("text") or "").strip()
        duration = float(payload.get("duration") or 0.0)
        if duration <= 0:
            usage = payload.get("usage") or {}
            try:
                duration = float(usage.get("seconds") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
        if duration <= 0:
            duration = _probe_audio_duration(audio_path)
        return plain_transcript_to_cues(text, duration), language

    def transcribe(self, video: Path) -> tuple[list[dict[str, Any]], str]:
        path = Path(video)
        print(f"  [Grok STT] 轉寫 {path.name}", flush=True)
        payload = self._request_transcription(path)
        return self._result_to_cues(payload, path)

    def transcribe_batch(
        self,
        videos: Sequence[Path],
    ) -> list[tuple[list[dict[str, Any]], str]]:
        paths = [Path(item) for item in videos]
        if not paths:
            return []
        if len(paths) == 1:
            return [self.transcribe(paths[0])]

        workers = min(len(paths), asr_batch_size())
        print(
            f"  [Grok STT] 並行轉寫 {len(paths)} 段（workers={workers}）",
            flush=True,
        )
        results: list[tuple[list[dict[str, Any]], str] | None] = [
            None
        ] * len(paths)

        def _one(index: int, path: Path) -> tuple[int, list[dict[str, Any]], str]:
            cues, language = self.transcribe(path)
            return index, cues, language

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_one, index, path)
                for index, path in enumerate(paths)
            ]
            for future in concurrent.futures.as_completed(futures):
                index, cues, language = future.result()
                results[index] = (cues, language)

        ordered: list[tuple[list[dict[str, Any]], str]] = []
        for index, item in enumerate(results):
            if item is None:
                raise RuntimeError(f"Grok STT batch 缺第 {index} 段結果")
            ordered.append(item)
        return ordered

    def release_transient_memory(self) -> None:
        return None


def selected_asr_backend_name(
    environment: Mapping[str, str] | None = None,
) -> str:
    environment = os.environ if environment is None else environment
    name = (
        environment.get("ASR_BACKEND")
        or environment.get("SUBTITLE_ASR_BACKEND")
        or DEFAULT_ASR_BACKEND
    ).strip().casefold()
    if name in {
        "grok-stt",
        "grok_stt",
        "grokstt",
        "grok-stt-1.0",
        "x-ai/grok-stt-1.0",
    }:
        return "grok-stt"
    if name in {"voxtral", "openrouter", "openrouter-voxtral", "stt"}:
        return "voxtral"
    if name in {"whisper", "faster-whisper", "faster_whisper"}:
        return "whisper"
    if name in {"moss", "moss-transcribe", "local"}:
        return "moss"
    raise ValueError(
        f"未知 ASR_BACKEND={name!r}，請用 whisper、voxtral、grok-stt 或 moss。"
    )


def create_backend():
    """依 ASR_BACKEND 建立 backend（預設 moss）。"""
    name = selected_asr_backend_name()
    if name == "voxtral":
        return VoxtralOpenRouterBackend()
    if name == "grok-stt":
        return GrokSTTOpenRouterBackend()
    if name == "whisper":
        return WhisperBackend()
    return MossBackend()

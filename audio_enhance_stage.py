"""字幕前的音訊內容判斷與 ASMR Enhancer 暫存處理。"""

from __future__ import annotations

import gc
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parent
DEFAULT_CLASSIFIER_MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"
DEFAULT_CLASSIFIER_REVISION = "f826b80d28226b62986cc218e5cec390b1096902"
DEFAULT_ENHANCER_SCRIPT = ROOT / "moss" / "asmr-enhancer" / "asmr_enhancer.py"
DEFAULT_MODEL_CACHE = ROOT / "moss" / "audio-model-cache"
DEFAULT_REPORT = ROOT / "tasks" / "audio-enhance-latest.json"
DEFAULT_STAGE_PYTHON = ROOT / "moss" / ".venv" / "Scripts" / "python.exe"
ENHANCE_MARKER = "ASMR Enhancer auto v1"
MUSIC_TITLE_PATTERN = re.compile(r"\b(pmv|dance|music|song|mv)\b", re.IGNORECASE)


@dataclass
class AudioMetrics:
    rms_dbfs: float
    crest_db: float
    stability_db: float


@dataclass
class AudioAnalysis:
    video: str
    decision: str
    category: str
    reason: str
    metrics: AudioMetrics | None = None
    music_score: float | None = None
    speech_score: float | None = None


@dataclass
class PreparedMedia:
    source: Path
    media_input: Path
    enhanced: bool
    analysis: AudioAnalysis

    def cleanup(self) -> None:
        if self.media_input != self.source:
            self.media_input.unlink(missing_ok=True)


def auto_enhance_enabled(environment: dict[str, str] | None = None) -> bool:
    environment = os.environ if environment is None else environment
    value = environment.get("AUDIO_AUTO_ENHANCE", "1").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("AUDIO_AUTO_ENHANCE 只允許 1/0、true/false、yes/no、on/off。")


def decode_audio(video: Path, sample_rate: int = 16_000) -> np.ndarray:
    ffmpeg = os.getenv("FFMPEG_EXE", "ffmpeg")
    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "f32le",
            "-",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        details = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg 音訊分析解碼失敗：{details[-1000:]}")
    audio = np.frombuffer(result.stdout, dtype="<f4").copy()
    if not audio.size:
        raise RuntimeError("影片沒有可分析的音訊。")
    return audio


def has_enhance_marker(video: Path) -> bool:
    ffprobe = os.getenv("FFPROBE_EXE", "ffprobe")
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format_tags=comment",
                "-of",
                "default=nw=1:nk=1",
                str(video),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0 and ENHANCE_MARKER in result.stdout


def middle_clips(
    audio: np.ndarray,
    sample_rate: int = 16_000,
    clip_seconds: float = 4.0,
) -> list[np.ndarray]:
    """避開固定片頭，從 25%、50%、75% 各取一段。"""
    clip_size = round(clip_seconds * sample_rate)
    if audio.size <= clip_size:
        return [np.pad(audio, (0, max(0, clip_size - audio.size)))]
    clips: list[np.ndarray] = []
    for ratio in (0.25, 0.50, 0.75):
        center = round(audio.size * ratio)
        start = max(0, min(audio.size - clip_size, center - clip_size // 2))
        clips.append(audio[start : start + clip_size])
    return clips


def calculate_metrics(
    clips: list[np.ndarray],
    sample_rate: int = 16_000,
) -> AudioMetrics:
    combined = np.concatenate(clips)
    rms = math.sqrt(float(np.mean(np.square(combined), dtype=np.float64)) + 1e-18)
    peak = float(np.max(np.abs(combined))) + 1e-12
    rms_dbfs = 20 * math.log10(rms + 1e-12)
    crest_db = 20 * math.log10(peak / (rms + 1e-12))
    frame_size = sample_rate // 2
    usable = combined[: combined.size // frame_size * frame_size]
    if usable.size:
        frames = usable.reshape(-1, frame_size)
        frame_rms = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-18)
        stability_db = float(np.std(20 * np.log10(frame_rms + 1e-12)))
    else:
        stability_db = 0.0
    return AudioMetrics(
        rms_dbfs=round(rms_dbfs, 3),
        crest_db=round(crest_db, 3),
        stability_db=round(stability_db, 3),
    )


def decide_audio(
    video: Path,
    metrics: AudioMetrics,
    music_score: float,
    speech_score: float,
) -> AudioAnalysis:
    title_music = bool(MUSIC_TITLE_PATTERN.search(video.name))
    if (
        metrics.rms_dbfs >= -18.0
        or music_score >= 0.60
        or (title_music and music_score >= 0.25)
    ):
        return AudioAnalysis(
            video=str(video),
            decision="pass",
            category="pass",
            reason="音量已高或音樂主導",
            metrics=metrics,
            music_score=round(music_score, 4),
            speech_score=round(speech_score, 4),
        )
    if (
        metrics.rms_dbfs <= -22.0
        and metrics.crest_db >= 18.0
        and metrics.stability_db <= 10.0
        and music_score < 0.35
    ):
        category = "enhance"
        reason = "安靜、平穩、高峰均比且非音樂主導"
    else:
        category = "uncertain"
        reason = "未達 pass 門檻，依設定自動增強"
    return AudioAnalysis(
        video=str(video),
        decision="enhance",
        category=category,
        reason=reason,
        metrics=metrics,
        music_score=round(music_score, 4),
        speech_score=round(speech_score, 4),
    )


class AudioClassifier:
    def __init__(self) -> None:
        import torch
        from transformers import AutoFeatureExtractor, AutoModelForAudioClassification

        self.torch = torch
        model_id = os.getenv("AUDIO_CLASSIFIER_MODEL", DEFAULT_CLASSIFIER_MODEL)
        revision = os.getenv(
            "AUDIO_CLASSIFIER_REVISION",
            DEFAULT_CLASSIFIER_REVISION,
        )
        cache_dir = Path(
            os.getenv("AUDIO_CLASSIFIER_CACHE", str(DEFAULT_MODEL_CACHE))
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        if torch.cuda.is_available():
            free_bytes, _ = torch.cuda.mem_get_info()
            reserve_mb = int(os.getenv("AUDIO_GPU_RESERVE_MB", "2048"))
            use_cuda = free_bytes >= (reserve_mb + 1024) * 1024 * 1024
        else:
            use_cuda = False
        self.device = torch.device("cuda" if use_cuda else "cpu")
        print(f"音訊分類器使用：{self.device}", flush=True)
        self.extractor = AutoFeatureExtractor.from_pretrained(
            model_id,
            revision=revision,
            cache_dir=str(cache_dir),
        )
        self.model = AutoModelForAudioClassification.from_pretrained(
            model_id,
            revision=revision,
            cache_dir=str(cache_dir),
        ).to(self.device).eval()
        labels = {
            int(index): label for index, label in self.model.config.id2label.items()
        }
        self.music_ids = [
            index
            for index, label in labels.items()
            if any(
                key in label.lower()
                for key in ("music", "singing", "musical", "instrument")
            )
        ]
        self.speech_ids = [
            index
            for index, label in labels.items()
            if any(
                key in label.lower()
                for key in ("speech", "conversation", "narration", "whispering")
            )
        ]

    def classify(self, clips: list[np.ndarray]) -> tuple[float, float]:
        inputs = self.extractor(
            clips,
            sampling_rate=16_000,
            return_tensors="pt",
            padding=True,
        )
        inputs = {
            name: tensor.to(self.device) for name, tensor in inputs.items()
        }
        with self.torch.inference_mode():
            probabilities = self.torch.sigmoid(
                self.model(**inputs).logits
            ).cpu()
        music = float(np.median([
            float(probability[self.music_ids].max())
            for probability in probabilities
        ]))
        speech = float(np.median([
            float(probability[self.speech_ids].max())
            for probability in probabilities
        ]))
        return music, speech

    def close(self) -> None:
        del self.model
        gc.collect()
        if self.torch.cuda.is_available():
            self.torch.cuda.empty_cache()


def _enhancer_script() -> Path:
    script = Path(
        os.getenv("ASMR_ENHANCER_SCRIPT", str(DEFAULT_ENHANCER_SCRIPT))
    )
    if not script.is_file():
        raise RuntimeError(
            f"找不到 ASMR Enhancer：{script}。請重新執行 install_moss.bat。"
        )
    return script


def _load_enhancer() -> Any:
    script = _enhancer_script()
    spec = importlib.util.spec_from_file_location("_asmr_enhancer_runtime", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"無法載入 ASMR Enhancer：{script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _temporary_output(video: Path) -> Path:
    return video.with_name(f".{video.stem}.audio-enhance.tmp{video.suffix}")


def _write_report(analyses: list[AudioAnalysis]) -> None:
    report_path = Path(os.getenv("AUDIO_ENHANCE_REPORT", str(DEFAULT_REPORT)))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            [asdict(analysis) for analysis in analyses],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _prepare_audio_media_local(videos: list[Path]) -> dict[Path, PreparedMedia]:
    """先分類全部影片，釋放分類器後再增強，最後交給 ASR。"""
    if not videos:
        return {}
    raw: list[tuple[Path, list[np.ndarray], AudioMetrics]] = []
    analyses: list[AudioAnalysis] = []
    prepared: dict[Path, PreparedMedia] = {}
    for index, video in enumerate(videos, 1):
        print(f"音訊分析 {index}/{len(videos)}：{video.name}", flush=True)
        if has_enhance_marker(video):
            analysis = AudioAnalysis(
                video=str(video),
                decision="pass",
                category="already_enhanced",
                reason="影片已有 ASMR Enhancer 標記，避免重複增強",
            )
            analyses.append(analysis)
            prepared[video] = PreparedMedia(video, video, False, analysis)
            continue
        try:
            clips = middle_clips(decode_audio(video))
            raw.append((video, clips, calculate_metrics(clips)))
        except Exception as exc:
            analysis = AudioAnalysis(
                video=str(video),
                decision="pass",
                category="error",
                reason=f"分析失敗，保留原音軌：{exc}",
            )
            analyses.append(analysis)
            prepared[video] = PreparedMedia(video, video, False, analysis)

    if raw:
        classifier: AudioClassifier | None = None
        try:
            classifier = AudioClassifier()
            for video, clips, metrics in raw:
                music_score, speech_score = classifier.classify(clips)
                analysis = decide_audio(
                    video,
                    metrics,
                    music_score,
                    speech_score,
                )
                analyses.append(analysis)
                prepared[video] = PreparedMedia(video, video, False, analysis)
                print(
                    f"  決策：{analysis.decision}；分類：{analysis.category}；"
                    f"RMS={metrics.rms_dbfs:.1f} dBFS；"
                    f"music={music_score:.2f}",
                    flush=True,
                )
        except Exception as exc:
            print(f"音訊分類器失敗，改用保守 DSP 規則：{exc}", flush=True)
            for video, _, metrics in raw:
                if video in prepared:
                    continue
                title_music = bool(MUSIC_TITLE_PATTERN.search(video.name))
                should_pass = metrics.rms_dbfs >= -18.0 or (
                    title_music and metrics.rms_dbfs >= -24.0
                )
                analysis = AudioAnalysis(
                    video=str(video),
                    decision="pass" if should_pass else "enhance",
                    category="fallback",
                    reason="分類器不可用，使用響度與檔名保守判斷",
                    metrics=metrics,
                )
                analyses.append(analysis)
                prepared[video] = PreparedMedia(video, video, False, analysis)
        finally:
            if classifier is not None:
                classifier.close()

    targets = [
        video
        for video in videos
        if prepared[video].analysis.decision == "enhance"
    ]
    if targets:
        enhancer = _load_enhancer()
        settings = enhancer.Settings(
            device=os.getenv("ASMR_ENHANCER_DEVICE", "auto")
        )
        for index, video in enumerate(targets, 1):
            temporary = _temporary_output(video)
            print(f"音訊增強 {index}/{len(targets)}：{video.name}", flush=True)
            try:
                enhancer.process_file(str(video), str(temporary), settings)
                prepared[video].media_input = temporary
                prepared[video].enhanced = True
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                prepared[video].analysis.category = "enhance_failed"
                prepared[video].analysis.reason = (
                    f"增強失敗，保留原音軌：{exc}"
                )
                print(prepared[video].analysis.reason, flush=True)
    _write_report(analyses)
    return prepared


def _analysis_from_dict(data: dict[str, Any]) -> AudioAnalysis:
    metrics_data = data.get("metrics")
    metrics = AudioMetrics(**metrics_data) if metrics_data else None
    return AudioAnalysis(
        video=data["video"],
        decision=data["decision"],
        category=data["category"],
        reason=data["reason"],
        metrics=metrics,
        music_score=data.get("music_score"),
        speech_score=data.get("speech_score"),
    )


def prepare_audio_media(videos: list[Path]) -> dict[Path, PreparedMedia]:
    """在獨立 MOSS venv 程序完成前處理，退出時徹底釋放 CUDA。"""
    if not videos:
        return {}
    python = Path(os.getenv("AUDIO_STAGE_PYTHON", str(DEFAULT_STAGE_PYTHON)))
    if not python.is_file():
        raise RuntimeError(
            f"找不到字幕音訊處理環境：{python}。請先執行 install_moss.bat。"
        )
    task_directory = ROOT / "tasks" / "audio-stage"
    task_directory.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    manifest = task_directory / f"{token}.input.json"
    result_path = task_directory / f"{token}.result.json"
    manifest.write_text(
        json.dumps([str(video) for video in videos], ensure_ascii=False),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    try:
        result = subprocess.run(
            [
                str(python),
                str(Path(__file__).resolve()),
                "--prepare-manifest",
                str(manifest),
                "--result",
                str(result_path),
            ],
            cwd=str(ROOT),
            env=environment,
            check=False,
        )
        if result.returncode != 0 or not result_path.is_file():
            raise RuntimeError(
                f"字幕音訊處理子程序失敗，ExitCode={result.returncode}。"
            )
        entries = json.loads(result_path.read_text(encoding="utf-8"))
        prepared: dict[Path, PreparedMedia] = {}
        for entry in entries:
            source = Path(entry["source"])
            prepared[source] = PreparedMedia(
                source=source,
                media_input=Path(entry["media_input"]),
                enhanced=bool(entry["enhanced"]),
                analysis=_analysis_from_dict(entry["analysis"]),
            )
        return prepared
    finally:
        manifest.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)


def _run_manifest(manifest: Path, result_path: Path) -> int:
    videos = [
        Path(value)
        for value in json.loads(manifest.read_text(encoding="utf-8"))
    ]
    prepared = _prepare_audio_media_local(videos)
    result_path.write_text(
        json.dumps(
            [
                {
                    "source": str(item.source),
                    "media_input": str(item.media_input),
                    "enhanced": item.enhanced,
                    "analysis": asdict(item.analysis),
                }
                for item in prepared.values()
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-manifest", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    return _run_manifest(args.prepare_manifest, args.result)


if __name__ == "__main__":
    raise SystemExit(main())

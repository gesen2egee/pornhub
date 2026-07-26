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
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from project_paths import LIB_DIR, MOSS_DIR, MOSS_VENV_DIR, TASKS_DIR


ROOT = LIB_DIR
DEFAULT_CLASSIFIER_MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"
DEFAULT_CLASSIFIER_REVISION = "f826b80d28226b62986cc218e5cec390b1096902"
DEFAULT_ENHANCER_SCRIPT = MOSS_DIR / "asmr-enhancer" / "asmr_enhancer.py"
DEFAULT_MODEL_CACHE = MOSS_DIR / "audio-model-cache"
DEFAULT_REPORT = TASKS_DIR / "audio-enhance-latest.json"
DEFAULT_STAGE_PYTHON = MOSS_VENV_DIR / "Scripts" / "python.exe"
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
        timeout=300,
    )
    if result.returncode != 0:
        details = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg 音訊分析解碼失敗：{details[-1000:]}")
    audio = np.frombuffer(result.stdout, dtype="<f4").copy()
    if not audio.size:
        raise RuntimeError("影片沒有可分析的音訊。")
    return audio


def probe_duration(video: Path) -> float:
    ffprobe = os.getenv("FFPROBE_EXE", "ffprobe")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(video),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("無法取得影片長度，不能快速抽樣音訊。") from exc
    if result.returncode != 0 or duration <= 0:
        raise RuntimeError("無法取得影片長度，不能快速抽樣音訊。")
    return duration


def decode_audio_range(
    video: Path,
    start: float,
    duration: float = 4.0,
    sample_rate: int = 16_000,
) -> np.ndarray:
    """直接 Seek 解碼短音訊，不讀取整支影片。"""
    ffmpeg = os.getenv("FFMPEG_EXE", "ffmpeg")
    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            str(max(0.0, start)),
            "-i",
            str(video),
            "-t",
            str(duration),
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
        timeout=60,
    )
    if result.returncode != 0:
        details = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg 音訊抽樣失敗：{details[-1000:]}")
    audio = np.frombuffer(result.stdout, dtype="<f4").copy()
    clip_size = round(duration * sample_rate)
    if not audio.size:
        raise RuntimeError("影片抽樣區間沒有音訊。")
    return np.pad(audio[:clip_size], (0, max(0, clip_size - audio.size)))


def sample_middle_clips(
    video: Path,
    clip_seconds: float = 4.0,
) -> list[np.ndarray]:
    """只解碼 25%、50%、75% 三段，避免為分析讀完整音軌。"""
    duration = probe_duration(video)
    starts = [
        max(0.0, min(duration - clip_seconds, duration * ratio - clip_seconds / 2))
        for ratio in (0.25, 0.50, 0.75)
    ]
    return [
        decode_audio_range(video, start, clip_seconds)
        for start in starts
    ]


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
            f"找不到 ASMR Enhancer：{script}。請重新執行 00_setup_or_update.bat。"
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


class AudioEnhanceSession:
    """在整個下載任務內重用音訊分類器與 ASMR Enhancer 權重。"""

    def __init__(self) -> None:
        self.classifier: AudioClassifier | None = None
        self.enhancer: Any | None = None
        self.settings: Any | None = None

    def prepare(self, videos: list[Path]) -> dict[Path, PreparedMedia]:
        return _prepare_audio_media_local(videos, session=self)

    def close(self) -> None:
        if self.classifier is not None:
            self.classifier.close()
            self.classifier = None
        self.enhancer = None
        self.settings = None


def _prepare_audio_media_local(
    videos: list[Path],
    *,
    session: AudioEnhanceSession | None = None,
) -> dict[Path, PreparedMedia]:
    """分析並增強；提供 session 時權重會保留給下一批影片。"""
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
            clips = sample_middle_clips(video)
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
        classifier = session.classifier if session is not None else None
        try:
            if classifier is None:
                classifier = AudioClassifier()
                if session is not None:
                    session.classifier = classifier
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
            if classifier is not None and session is None:
                classifier.close()

    targets = [
        video
        for video in videos
        if prepared[video].analysis.decision == "enhance"
    ]
    if targets:
        enhancer = session.enhancer if session is not None else None
        settings = session.settings if session is not None else None
        if enhancer is None or settings is None:
            enhancer = _load_enhancer()
            settings = enhancer.Settings(
                device=os.getenv("ASMR_ENHANCER_DEVICE", "auto")
            )
            if session is not None:
                session.enhancer = enhancer
                session.settings = settings
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


def _prepared_entries(prepared: dict[Path, PreparedMedia]) -> list[dict[str, Any]]:
    return [
        {
            "source": str(item.source),
            "media_input": str(item.media_input),
            "enhanced": item.enhanced,
            "analysis": asdict(item.analysis),
        }
        for item in prepared.values()
    ]


def _prepared_from_entries(entries: list[dict[str, Any]]) -> dict[Path, PreparedMedia]:
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


class AudioEnhanceWorker:
    """常駐音訊 worker；整次任務僅載入一次分類器／Enhancer。"""

    def __init__(self) -> None:
        python = Path(os.getenv("AUDIO_STAGE_PYTHON", str(DEFAULT_STAGE_PYTHON)))
        if not python.is_file():
            raise RuntimeError(
                f"找不到字幕音訊處理環境：{python}。請先執行 00_setup_or_update.bat。"
            )
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        self._process = subprocess.Popen(
            [str(python), str(Path(__file__).resolve()), "--prepare-worker"],
            cwd=str(ROOT),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._closed = False
        print("[音訊 Enhance 常駐] 已啟動；權重將在首次使用後保留", flush=True)

    def prepare(self, videos: list[Path]) -> dict[Path, PreparedMedia]:
        if self._closed or self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("音訊 Enhance 常駐程序已關閉")
        request = {"videos": [str(Path(video).resolve()) for video in videos]}
        try:
            self._process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
            line = self._process.stdout.readline()
        except OSError as exc:
            raise RuntimeError("音訊 Enhance 常駐程序通訊失敗") from exc
        if not line:
            raise RuntimeError(
                f"音訊 Enhance 常駐程序提早結束，ExitCode={self._process.poll()}"
            )
        try:
            reply = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"音訊 Enhance 常駐程序回傳無效 JSON：{line[:160]}") from exc
        if reply.get("error"):
            raise RuntimeError(f"音訊 Enhance 常駐程序失敗：{reply['error']}")
        entries = reply.get("prepared")
        if not isinstance(entries, list):
            raise RuntimeError("音訊 Enhance 常駐程序回傳格式不符。")
        return _prepared_from_entries(entries)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.stdin is not None and self._process.poll() is None:
                self._process.stdin.write('{"command":"shutdown"}\n')
                self._process.stdin.flush()
                self._process.stdin.close()
            self._process.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            self._process.kill()
            self._process.wait(timeout=15)


@contextmanager
def audio_enhance_session():
    """建立任務範圍的常駐音訊 Enhance worker。"""
    worker = AudioEnhanceWorker()
    try:
        yield worker
    finally:
        worker.close()


def prepare_audio_media(videos: list[Path]) -> dict[Path, PreparedMedia]:
    """在獨立 MOSS venv 程序完成前處理，退出時徹底釋放 CUDA。"""
    if not videos:
        return {}
    python = Path(os.getenv("AUDIO_STAGE_PYTHON", str(DEFAULT_STAGE_PYTHON)))
    if not python.is_file():
        raise RuntimeError(
            f"找不到字幕音訊處理環境：{python}。請先執行 00_setup_or_update.bat。"
        )
    task_directory = TASKS_DIR / "audio-stage"
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
        return _prepared_from_entries(json.loads(result_path.read_text(encoding="utf-8")))
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
            _prepared_entries(prepared),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


def _run_prepare_worker() -> int:
    """stdin/stdout JSON worker；日誌改走 stderr，避免干擾協定。"""
    protocol_stdout = sys.stdout
    session = AudioEnhanceSession()
    try:
        for raw in sys.stdin:
            try:
                request = json.loads(raw)
                if request.get("command") == "shutdown":
                    return 0
                videos = request.get("videos")
                if not isinstance(videos, list) or not videos:
                    raise ValueError("videos 必須是非空陣列")
                with redirect_stdout(sys.stderr):
                    prepared = session.prepare([Path(value) for value in videos])
                reply: dict[str, Any] = {"prepared": _prepared_entries(prepared)}
            except Exception as exc:
                reply = {"error": str(exc)}
            protocol_stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            protocol_stdout.flush()
    finally:
        session.close()
    return 0


def analyze_and_enhance_segments(
    video: Path,
    segments: list[tuple[float, float]],
    sample_rate: int = 16_000,
) -> list[dict]:
    """
    分段獨立偵測音訊是否符合增強條件。
    傳回每個 segment 的獨立增強決策與檢測數據。
    """
    results = []
    for idx, (start, end) in enumerate(segments):
        dur = end - start
        if dur <= 0:
            continue
        try:
            sample_start = start + max(0.0, (dur - 4.0) / 2.0)
            sample_dur = min(dur, 4.0)
            audio = decode_audio_range(video, sample_start, sample_dur, sample_rate)
            metrics = calculate_metrics([audio], sample_rate)

            should_enhance = (metrics.rms_dbfs < -20.0 or metrics.stability_db < 8.0)
            results.append({
                "segment_index": idx,
                "start": start,
                "end": end,
                "duration": round(dur, 2),
                "should_enhance": should_enhance,
                "rms_dbfs": round(metrics.rms_dbfs, 2),
                "reason": "音量低或細節需修飾，建議 Enhance" if should_enhance else "音量穩定充沛，Pass 免 Enhance"
            })
        except Exception as e:
            results.append({
                "segment_index": idx,
                "start": start,
                "end": end,
                "duration": round(dur, 2),
                "should_enhance": False,
                "reason": f"分段分析跳過: {str(e)}"
            })
    return results


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-manifest", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--prepare-worker", action="store_true")
    args = parser.parse_args()
    if args.prepare_worker:
        if args.prepare_manifest or args.result:
            parser.error("--prepare-worker 不可與 manifest/result 同時使用")
        return _run_prepare_worker()
    if not args.prepare_manifest or not args.result:
        parser.error("--prepare-manifest 與 --result 必須同時提供")
    return _run_manifest(args.prepare_manifest, args.result)


if __name__ == "__main__":
    raise SystemExit(main())

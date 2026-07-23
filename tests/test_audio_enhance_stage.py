from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import audio_enhance_stage
from audio_enhance_stage import (
    AudioMetrics,
    PreparedMedia,
    auto_enhance_enabled,
    calculate_metrics,
    decide_audio,
    middle_clips,
    prepare_audio_media,
)


def test_middle_clips_uses_quarter_middle_and_three_quarters():
    audio = np.arange(160, dtype=np.float32)

    clips = middle_clips(audio, sample_rate=10, clip_seconds=2)

    assert [clip.tolist() for clip in clips] == [
        np.arange(30, 50, dtype=np.float32).tolist(),
        np.arange(70, 90, dtype=np.float32).tolist(),
        np.arange(110, 130, dtype=np.float32).tolist(),
    ]


def test_calculate_metrics_reports_quiet_high_crest_audio():
    audio = np.zeros(160, dtype=np.float32)
    audio[80] = 0.1

    metrics = calculate_metrics([audio], sample_rate=10)

    assert metrics.rms_dbfs < -40
    assert metrics.crest_db > 20


def test_decide_audio_passes_loud_or_music_dominant_video(tmp_path):
    video = tmp_path / "sample.mp4"
    metrics = AudioMetrics(rms_dbfs=-12, crest_db=12, stability_db=2)

    result = decide_audio(video, metrics, music_score=0.1, speech_score=0.8)

    assert result.decision == "pass"
    assert result.category == "pass"


def test_decide_audio_enhances_quiet_stable_video(tmp_path):
    video = tmp_path / "sample.mp4"
    metrics = AudioMetrics(rms_dbfs=-32, crest_db=24, stability_db=4)

    result = decide_audio(video, metrics, music_score=0.05, speech_score=0.8)

    assert result.decision == "enhance"
    assert result.category == "enhance"


def test_decide_audio_maps_uncertain_to_enhance(tmp_path):
    video = tmp_path / "sample.mp4"
    metrics = AudioMetrics(rms_dbfs=-20, crest_db=15, stability_db=12)

    result = decide_audio(video, metrics, music_score=0.2, speech_score=0.4)

    assert result.decision == "enhance"
    assert result.category == "uncertain"


def test_auto_enhance_enabled_defaults_on_and_validates_value():
    assert auto_enhance_enabled({}) is True
    assert auto_enhance_enabled({"AUDIO_AUTO_ENHANCE": "off"}) is False
    with pytest.raises(ValueError, match="AUDIO_AUTO_ENHANCE"):
        auto_enhance_enabled({"AUDIO_AUTO_ENHANCE": "sometimes"})


def test_prepared_media_cleanup_only_removes_temporary_input(tmp_path):
    source = tmp_path / "source.mp4"
    temporary = tmp_path / ".source.audio-enhance.tmp.mp4"
    source.write_bytes(b"source")
    temporary.write_bytes(b"temporary")
    analysis = decide_audio(
        source,
        AudioMetrics(rms_dbfs=-32, crest_db=24, stability_db=4),
        music_score=0.05,
        speech_score=0.8,
    )

    PreparedMedia(source, temporary, True, analysis).cleanup()

    assert source.exists()
    assert not temporary.exists()


def test_prepare_audio_media_uses_isolated_stage_python(
    tmp_path,
    monkeypatch,
):
    python = tmp_path / "python.exe"
    video = tmp_path / "sample.mp4"
    temporary = tmp_path / ".sample.audio-enhance.tmp.mp4"
    python.write_bytes(b"python")
    video.write_bytes(b"video")
    monkeypatch.setenv("AUDIO_STAGE_PYTHON", str(python))
    monkeypatch.setattr(audio_enhance_stage, "ROOT", tmp_path)

    def fake_run(command, **kwargs):
        result_path = Path(command[command.index("--result") + 1])
        result_path.write_text(
            """[
              {
                "source": "%s",
                "media_input": "%s",
                "enhanced": true,
                "analysis": {
                  "video": "%s",
                  "decision": "enhance",
                  "category": "enhance",
                  "reason": "test",
                  "metrics": {
                    "rms_dbfs": -32.0,
                    "crest_db": 24.0,
                    "stability_db": 4.0
                  },
                  "music_score": 0.1,
                  "speech_score": 0.8
                }
              }
            ]"""
            % (
                str(video).replace("\\", "\\\\"),
                str(temporary).replace("\\", "\\\\"),
                str(video).replace("\\", "\\\\"),
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(audio_enhance_stage.subprocess, "run", fake_run)

    result = prepare_audio_media([video])

    assert result[video].media_input == temporary
    assert result[video].enhanced is True
    assert result[video].analysis.metrics.rms_dbfs == -32.0
    assert not list((tmp_path / "tasks" / "audio-stage").glob("*.json"))

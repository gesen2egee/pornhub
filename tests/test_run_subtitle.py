from pathlib import Path

import pytest

import run_subtitle


class FakeBackend:
    display_name = "Fake ASR"

    def __init__(self, cues=None, language="en"):
        self.cues = [] if cues is None else cues
        self.language = language
        self.videos: list[Path] = []

    def transcribe(self, video: Path):
        self.videos.append(video)
        return self.cues, self.language


def test_process_video_uses_selected_backend(tmp_path, monkeypatch):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"video")
    backend = FakeBackend(
        cues=[
            {
                "id": 1,
                "time": "00:00:00,000 --> 00:00:01,000",
                "text": "[S01] Hi",
            }
        ]
    )
    monkeypatch.setattr(run_subtitle, "translate_cues", lambda cues, *_: cues)
    monkeypatch.setattr(
        run_subtitle,
        "_embed_soft_subtitle",
        lambda video, subtitle, output, force: output,
    )

    output = run_subtitle.process_video(
        video,
        backend,
        "key",
        "model",
        True,
    )

    assert backend.videos == [video]
    assert output == video
    assert (
        video.with_suffix(".srt").read_text(encoding="utf-8-sig").strip()
        == "1\n00:00:00,000 --> 00:00:01,000\nHi"
    )


def test_process_video_skips_backend_when_srt_exists(tmp_path, monkeypatch):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"video")
    video.with_suffix(".srt").write_text("existing", encoding="utf-8")
    backend = FakeBackend()
    monkeypatch.setattr(
        run_subtitle,
        "_embed_soft_subtitle",
        lambda video, subtitle, output, force: output,
    )

    run_subtitle.process_video(video, backend, None, "model", False)

    assert backend.videos == []


def test_process_video_rejects_empty_asr_result(tmp_path, monkeypatch):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"video")
    backend = FakeBackend()
    monkeypatch.setattr(run_subtitle, "translate_cues", lambda cues, *_: cues)

    with pytest.raises(RuntimeError, match="ASR 沒有產生有效字幕"):
        run_subtitle.process_video(video, backend, "key", "model", True)

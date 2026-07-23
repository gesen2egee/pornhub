from pathlib import Path

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
    burn_calls = []
    monkeypatch.setattr(
        run_subtitle,
        "_burn_hard_subtitle",
        lambda source, subtitle, output, force, **kwargs: burn_calls.append(
            (source, subtitle, output)
        ) or output,
    )
    meta_calls = []
    monkeypatch.setattr(
        run_subtitle.video_meta,
        "merge_write_mp4_meta",
        lambda path, **kwargs: meta_calls.append((path, kwargs)),
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
    assert not video.with_suffix(".srt").exists()
    assert burn_calls[0][0] == video
    assert burn_calls[0][1].parent == run_subtitle.SUBTITLE_TEMP
    assert not burn_calls[0][1].exists()
    assert "[S01] Hi" in meta_calls[0][1]["original_srt"]
    assert "[S01] Hi" in meta_calls[0][1]["translated_srt"]
    assert "base_comment" in meta_calls[0][1]


def test_legacy_srt_is_considered_complete(tmp_path):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"video")
    video.with_suffix(".srt").write_text("existing", encoding="utf-8")
    assert run_subtitle._subtitle_complete(video)


def test_empty_embedded_subtitle_meta_is_considered_complete(tmp_path, monkeypatch):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        run_subtitle,
        "_read_video_meta",
        lambda path: {
            "translated_srt": "",
            "translated_srt_present": True,
        },
    )
    assert run_subtitle._subtitle_complete(video)


def test_process_video_stores_empty_subtitle_meta(tmp_path, monkeypatch):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"video")
    backend = FakeBackend()
    meta_calls = []
    burn_calls = []
    monkeypatch.setattr(
        run_subtitle.video_meta,
        "merge_write_mp4_meta",
        lambda path, **kwargs: meta_calls.append(kwargs),
    )
    monkeypatch.setattr(
        run_subtitle,
        "_burn_hard_subtitle",
        lambda *args, **kwargs: burn_calls.append(args),
    )

    run_subtitle.process_video(video, backend, "key", "model", True)

    assert burn_calls == []
    assert meta_calls[0]["original_srt"] == ""
    assert meta_calls[0]["translated_srt"] == ""


def test_process_video_uses_enhanced_media_for_asr_and_packaging(
    tmp_path,
    monkeypatch,
):
    video = tmp_path / "sample.mp4"
    enhanced = tmp_path / ".sample.audio-enhance.tmp.mp4"
    video.write_bytes(b"video")
    enhanced.write_bytes(b"enhanced")
    backend = FakeBackend(
        cues=[
            {
                "id": 1,
                "time": "00:00:00,000 --> 00:00:01,000",
                "text": "Hi",
            }
        ]
    )
    calls = []
    monkeypatch.setattr(run_subtitle, "translate_cues", lambda cues, *_: cues)
    monkeypatch.setattr(
        run_subtitle,
        "_burn_hard_subtitle",
        lambda source, subtitle, output, force, **kwargs: calls.append(
            (source, output, kwargs["mark_audio_enhanced"])
        )
        or output,
    )

    run_subtitle.process_video(
        video,
        backend,
        "key",
        "model",
        True,
        media_input=enhanced,
        audio_enhanced=True,
    )

    assert backend.videos == [enhanced]
    assert calls == [(enhanced, video, True)]

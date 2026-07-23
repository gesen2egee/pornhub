from pathlib import Path

import pytest

import subtitle_worker


class FakeMedia:
    def __init__(self, video: Path):
        self.media_input = video
        self.enhanced = False
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True


def make_runtime():
    runtime = subtitle_worker.SubtitleRuntime.__new__(
        subtitle_worker.SubtitleRuntime
    )
    runtime.backend = object()
    runtime.api_key = "key"
    runtime.model_name = "model"
    runtime.use_audio_enhance = True
    runtime.configured_max_tokens = None
    runtime._load_backend = lambda: runtime.backend
    return runtime


def test_grid_moves_only_after_full_subtitle_meta(tmp_path, monkeypatch):
    video = tmp_path / "videos" / "sample.mp4"
    grid = video.with_suffix(".jpg")
    archive = tmp_path / "downloads"
    video.parent.mkdir()
    video.write_bytes(b"video")
    grid.write_bytes(b"grid")
    media = FakeMedia(video)
    states = iter([False, True])
    monkeypatch.setattr(
        subtitle_worker.run_subtitle,
        "_subtitle_complete",
        lambda path: next(states),
    )
    monkeypatch.setattr(
        subtitle_worker.run_subtitle,
        "_read_video_meta",
        lambda path: {
            "original_srt_present": True,
            "translated_srt_present": True,
        },
    )
    monkeypatch.setattr(
        subtitle_worker,
        "prepare_audio_media",
        lambda videos: {video.resolve(): media},
    )
    monkeypatch.setattr(
        subtitle_worker.run_subtitle,
        "process_video",
        lambda *args, **kwargs: video,
    )

    make_runtime().process({
        "video": str(video),
        "grid": str(grid),
        "archive_dir": str(archive),
    })

    assert not grid.exists()
    assert (archive / grid.name).exists()
    assert media.cleaned


def test_grid_stays_when_subtitle_meta_is_missing(tmp_path, monkeypatch):
    video = tmp_path / "sample.mp4"
    grid = tmp_path / "sample.jpg"
    video.write_bytes(b"video")
    grid.write_bytes(b"grid")
    monkeypatch.setattr(
        subtitle_worker.run_subtitle,
        "_subtitle_complete",
        lambda path: False,
    )
    monkeypatch.setattr(
        subtitle_worker.run_subtitle,
        "_read_video_meta",
        lambda path: {},
    )
    monkeypatch.setattr(subtitle_worker, "prepare_audio_media", lambda videos: {})
    monkeypatch.setattr(
        subtitle_worker.run_subtitle,
        "process_video",
        lambda *args, **kwargs: video,
    )

    with pytest.raises(RuntimeError, match="沒有完整雙字幕 Meta"):
        make_runtime().process({
            "video": str(video),
            "grid": str(grid),
            "archive_dir": str(tmp_path / "downloads"),
        })

    assert grid.exists()

from pathlib import Path

import subtitle_worker


class FakeMedia:
    def __init__(self, video: Path):
        self.source = video
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
    video = tmp_path / "temp" / "sample.mp4"
    final_video = tmp_path / "videos" / "sample.mp4"
    grid = tmp_path / "videos" / "sample.jpg"
    archive = tmp_path / "downloaded"
    video.parent.mkdir()
    grid.parent.mkdir()
    video.write_bytes(b"video")
    grid.write_bytes(b"grid")
    media = FakeMedia(video)
    monkeypatch.setattr(
        subtitle_worker.run_subtitle,
        "_subtitle_complete",
        lambda path: False,
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
        "final_video": str(final_video),
        "grid": str(grid),
        "archive_dir": str(archive),
        "archive_grid": True,
    })

    assert not video.exists()
    assert final_video.exists()
    assert not grid.exists()
    assert (archive / grid.name).exists()
    assert media.cleaned


def test_subtitle_failure_finalizes_original_video(tmp_path, monkeypatch):
    video = tmp_path / "sample.mp4"
    final_video = tmp_path / "videos" / "sample.mp4"
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
    meta_calls = []
    monkeypatch.setattr(
        subtitle_worker.run_subtitle.video_meta,
        "merge_write_mp4_meta",
        lambda path, **kwargs: meta_calls.append(kwargs),
    )

    archive = tmp_path / "downloaded"
    make_runtime().process({
        "video": str(video),
        "final_video": str(final_video),
        "grid": str(grid),
        "archive_dir": str(archive),
        "archive_grid": True,
    })

    assert not video.exists()
    assert final_video.read_bytes() == b"video"
    assert not grid.exists()
    assert (archive / grid.name).exists()
    assert meta_calls[0]["original_srt"] == ""
    assert meta_calls[0]["translated_srt"] == ""
    assert meta_calls[0]["subtitle_status"]["outcome"] == "failed"
    assert meta_calls[0]["subtitle_status"]["audio_enhanced"] is False


def test_low_video_finalizes_but_keeps_grid(tmp_path, monkeypatch):
    video = tmp_path / "temp" / "sample.mp4"
    final_video = tmp_path / "low_videos" / "sample.mp4"
    grid = tmp_path / "low_videos" / "sample.jpg"
    video.parent.mkdir()
    grid.parent.mkdir()
    video.write_bytes(b"video")
    grid.write_bytes(b"grid")
    monkeypatch.setattr(
        subtitle_worker.run_subtitle,
        "_subtitle_complete",
        lambda path: True,
    )

    runtime = make_runtime()
    runtime.api_key = None
    runtime.process({
        "video": str(video),
        "final_video": str(final_video),
        "grid": str(grid),
        "archive_dir": str(tmp_path / "downloaded"),
        "archive_grid": False,
        "is_low_quality": True,
    })

    assert not video.exists()
    assert final_video.exists()
    assert grid.exists()


def test_subtitle_failure_finalizes_enhanced_video(tmp_path, monkeypatch):
    video = tmp_path / "temp" / "sample.mp4"
    enhanced = tmp_path / "temp" / ".sample.audio-enhance.tmp.mp4"
    final_video = tmp_path / "videos" / "sample.mp4"
    grid = tmp_path / "videos" / "sample.jpg"
    video.parent.mkdir()
    grid.parent.mkdir()
    video.write_bytes(b"original")
    enhanced.write_bytes(b"enhanced")
    grid.write_bytes(b"grid")
    media = FakeMedia(video)
    media.media_input = enhanced
    media.enhanced = True
    monkeypatch.setattr(
        subtitle_worker.run_subtitle,
        "_subtitle_complete",
        lambda path: False,
    )
    monkeypatch.setattr(
        subtitle_worker.run_subtitle,
        "_read_video_meta",
        lambda path: {"raw_comment": "", "web_meta": {"title": "T"}},
    )
    monkeypatch.setattr(
        subtitle_worker,
        "prepare_audio_media",
        lambda videos: {video.resolve(): media},
    )
    monkeypatch.setattr(
        subtitle_worker.run_subtitle,
        "process_video",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("ASR failed")
        ),
    )
    meta_calls = []
    monkeypatch.setattr(
        subtitle_worker.run_subtitle.video_meta,
        "merge_write_mp4_meta",
        lambda path, **kwargs: meta_calls.append(kwargs),
    )

    make_runtime().process({
        "video": str(video),
        "final_video": str(final_video),
        "grid": str(grid),
        "archive_dir": str(tmp_path / "downloaded"),
        "archive_grid": True,
    })

    assert final_video.read_bytes() == b"enhanced"
    assert not video.exists()
    assert not enhanced.exists()
    assert media.cleaned
    assert meta_calls[0]["subtitle_status"]["outcome"] == "failed"
    assert meta_calls[0]["subtitle_status"]["audio_enhanced"] is True


def test_low_job_has_shorter_bounded_timeout(monkeypatch):
    monkeypatch.setenv("SUBTITLE_LOW_JOB_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("SUBTITLE_JOB_TIMEOUT_SECONDS", "34")
    assert subtitle_worker.subtitle_job_timeout({"is_low_quality": True}) == 12
    assert subtitle_worker.subtitle_job_timeout({"is_low_quality": False}) == 34


def test_high_video_defaults_to_8192_tokens(tmp_path, monkeypatch):
    video = tmp_path / "sample.mp4"
    final_video = tmp_path / "videos" / "sample.mp4"
    grid = tmp_path / "sample.jpg"
    video.write_bytes(b"video")
    grid.write_bytes(b"grid")
    monkeypatch.delenv("MOSS_MAX_NEW_TOKENS", raising=False)
    monkeypatch.setattr(
        subtitle_worker.run_subtitle,
        "_subtitle_complete",
        lambda path: True,
    )
    runtime = make_runtime()
    runtime.configured_max_tokens = None

    runtime.process({
        "video": str(video),
        "final_video": str(final_video),
        "grid": str(grid),
        "archive_dir": str(tmp_path / "downloaded"),
        "archive_grid": False,
        "is_low_quality": False,
    })

    assert (
        subtitle_worker.os.environ["MOSS_MAX_NEW_TOKENS"]
        == str(subtitle_worker.DEFAULT_HIGH_MAX_TOKENS)
    )


def test_invalid_timeout_uses_default(monkeypatch):
    monkeypatch.setenv("SUBTITLE_LOW_JOB_TIMEOUT_SECONDS", "invalid")
    assert (
        subtitle_worker.subtitle_job_timeout({"is_low_quality": True})
        == subtitle_worker.DEFAULT_LOW_JOB_TIMEOUT
    )

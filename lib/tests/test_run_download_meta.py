from pathlib import Path

import pytest
from PIL import Image

import run_download


@pytest.fixture(autouse=True)
def isolate_pipeline_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(
        run_download,
        "WORK_TEMP_DIR",
        str(tmp_path / "temp"),
    )
    monkeypatch.setattr(
        run_download,
        "ARCHIVE_DIR",
        str(tmp_path / "downloaded"),
    )


def test_1080p_limit_accepts_landscape_and_portrait():
    assert run_download.HIGH_VIDEO_FORMAT == "bestvideo*+bestaudio/best"
    assert run_download.HIGH_VIDEO_FORMAT_SORT == ["res:1080"]
    assert run_download.is_within_1080p_dimensions(1920, 1080)
    assert run_download.is_within_1080p_dimensions(1080, 1920)
    assert run_download.is_within_1080p_dimensions(810, 1440)
    assert not run_download.is_within_1080p_dimensions(3840, 2160)
    assert not run_download.is_within_1080p_dimensions(2160, 3840)
    assert not run_download.is_within_1080p_dimensions(2560, 1080)


def test_pornhub_html_fallback_helpers_removed():
    assert not hasattr(run_download, "select_pornhub_mp4_url")
    assert not hasattr(run_download, "direct_fetch_pornhub_mp4_stream")
    assert not hasattr(run_download, "is_pornhub_url")


def test_upgrade_writes_same_web_meta_to_video_and_grid(tmp_path, monkeypatch):
    jpg = tmp_path / "grid.jpg"
    mp4 = tmp_path / "video.mp4"
    image = Image.new("RGB", (16, 16), "black")
    exif = image.getexif()
    exif[0x010E] = "https://example.com/v"
    image.save(jpg, exif=exif)
    mp4.write_bytes(b"video")
    calls = {}
    monkeypatch.setattr(
        run_download.video_meta,
        "merge_write_mp4_meta",
        lambda path, **kwargs: calls.setdefault("mp4", kwargs["web_meta"]),
    )
    run_download.upgrade_media_web_meta(
        jpg,
        mp4,
        "https://example.com/v",
        info={"title": "T"},
    )
    grid = run_download.video_meta.read_grid_jpg_meta(jpg)
    assert calls["mp4"]["title"] == "T"
    assert grid["web_meta"]["title"] == "T"


def test_existing_video_is_queued_without_moving_grid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "videos"
    target.mkdir()
    jpg = target / "0001-sample.jpg"
    mp4 = target / "sample.mp4"
    image = Image.new("RGB", (16, 16), "black")
    exif = image.getexif()
    exif[0x010E] = "https://example.com/v"
    image.save(jpg, exif=exif)
    mp4.write_bytes(b"video")
    monkeypatch.setattr(
        run_download,
        "upgrade_media_web_meta",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        run_download,
        "has_completed_subtitle",
        lambda path: True,
    )
    monkeypatch.setattr(run_download, "has_video_stream", lambda path: True)

    class Worker:
        calls = []

        def enqueue(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    worker = Worker()
    run_download.process_single_directory(
        "videos",
        is_low_quality=False,
        subtitle_worker=worker,
    )

    assert jpg.exists()
    assert len(worker.calls) == 1
    args, kwargs = worker.calls[0]
    assert Path(args[0]).resolve() == mp4
    assert Path(args[1]).resolve() == mp4
    assert Path(args[2]).resolve() == jpg
    assert kwargs == {"is_low_quality": False}


def test_incomplete_existing_official_video_stays_published_for_subtitles(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "videos"
    target.mkdir()
    jpg = target / "0001-sample.jpg"
    final_video = target / "sample.mp4"
    image = Image.new("RGB", (16, 16), "black")
    exif = image.getexif()
    exif[0x010E] = "https://example.com/v"
    image.save(jpg, exif=exif)
    final_video.write_bytes(b"incomplete")
    monkeypatch.setattr(
        run_download,
        "upgrade_media_web_meta",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        run_download,
        "has_completed_subtitle",
        lambda path: False,
    )
    monkeypatch.setattr(run_download, "has_video_stream", lambda path: True)

    class Worker:
        calls = []

        def enqueue(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    worker = Worker()
    run_download.process_single_directory(
        "videos",
        is_low_quality=False,
        subtitle_worker=worker,
    )

    assert final_video.read_bytes() == b"incomplete"
    assert jpg.exists()
    args, _ = worker.calls[0]
    assert Path(args[0]).resolve() == final_video
    assert Path(args[1]).resolve() == final_video


def test_invalid_empty_mp4_is_removed_for_redownload(tmp_path, monkeypatch):
    video = tmp_path / "empty.mp4"
    video.write_bytes(b"metadata-only")
    monkeypatch.setattr(
        run_download,
        "has_video_stream",
        lambda path: False,
    )

    assert run_download.remove_invalid_video(video, "測試影片")
    assert not video.exists()


def test_probe_unavailable_does_not_delete_video(tmp_path, monkeypatch):
    video = tmp_path / "unknown.mp4"
    video.write_bytes(b"keep")
    monkeypatch.setattr(
        run_download,
        "has_video_stream",
        lambda path: None,
    )

    assert not run_download.remove_invalid_video(video, "測試影片")
    assert video.exists()


def test_failed_meta_is_not_complete(monkeypatch):
    monkeypatch.setattr(
        run_download.video_meta,
        "read_mp4_meta",
        lambda path: {
            "subtitle_status": {"outcome": "failed"},
            "original_srt_present": True,
            "translated_srt_present": True,
        },
    )
    assert not run_download.has_completed_subtitle("sample.mp4")


def test_official_failed_video_stays_published_while_retrying_subtitles(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "videos"
    target.mkdir()
    video = target / "sample.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        run_download,
        "needs_subtitle_retry",
        lambda path: True,
    )

    class Worker:
        calls = []

        def enqueue(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    worker = Worker()
    count = run_download.enqueue_official_subtitle_retries(
        "videos",
        False,
        worker,
    )

    assert count == 1
    assert video.exists()
    args, kwargs = worker.calls[0]
    assert Path(args[0]).resolve() == video
    assert Path(args[1]).resolve() == video
    assert kwargs["archive_grid"] is False


def test_staged_official_video_is_published_before_subtitle_queue(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "videos"
    target.mkdir()
    staged_dir = tmp_path / "temp" / "pipeline" / "videos"
    staged_dir.mkdir(parents=True)
    staged = staged_dir / "sample.mp4"
    staged.write_bytes(b"downloaded")

    class Worker:
        calls = []

        def enqueue(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    worker = Worker()
    count = run_download.enqueue_staged_subtitle_retries(
        "videos",
        False,
        worker,
    )

    final_video = target / "sample.mp4"
    assert count == 1
    assert final_video.read_bytes() == b"downloaded"
    assert not staged.exists()
    args, _ = worker.calls[0]
    assert Path(args[0]).resolve() == final_video
    assert Path(args[1]).resolve() == final_video

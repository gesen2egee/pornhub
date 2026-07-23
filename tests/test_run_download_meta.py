from PIL import Image

import run_download


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
    assert worker.calls == [
        (("videos\\sample.mp4", "videos\\0001-sample.jpg"), {
            "is_low_quality": False,
        })
    ]

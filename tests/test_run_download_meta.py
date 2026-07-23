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

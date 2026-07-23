import json

from PIL import Image

import video_meta


def test_build_web_meta_has_fixed_schema_and_time_fields():
    meta = video_meta.build_web_meta({
        "title": "測試",
        "duration": 1531,
        "webpage_url": "https://example.com/v",
    })
    assert meta["schema"] == "web_meta_v1"
    assert meta["duration_string"] == "25:31"
    assert meta["tags"] == []
    assert meta["categories"] == []
    assert meta["cast"] == []
    assert meta["upload_date"] is None
    assert meta["timestamp"] is None
    assert meta["meta_written_at"]


def test_comment_merge_preserves_other_sections():
    original = "1\n00:00:00,000 --> 00:00:01,000\n[S01] Hello\n"
    translated = "1\n00:00:00,000 --> 00:00:01,000\n[S01] 你好\n"
    comment = "ASMR Enhancer auto v1\n===FUTURE_DATA===\n保留我\n"
    merged = video_meta.merge_comment(
        comment,
        web_meta={"schema": "web_meta_v1", "title": "T"},
        original_srt=original,
        translated_srt=translated,
    )
    prefix, sections = video_meta.parse_sections(merged)
    assert prefix == "ASMR Enhancer auto v1"
    assert json.loads(sections["WEB_META_V1"])["title"] == "T"
    assert sections["ORIGINAL_SRT"] == original.rstrip()
    assert sections["TRANSLATED_SRT"] == translated.rstrip()
    assert sections["FUTURE_DATA"] == "保留我"


def test_base_comment_can_restore_sections_after_remux(monkeypatch):
    merged = video_meta.merge_comment(
        "===WEB_META_V1===\n{\"title\":\"保留\"}\n",
        translated_srt="1\n00:00:00,000 --> 00:00:01,000\n[S01] 翻譯\n",
    )
    _, sections = video_meta.parse_sections(merged)
    assert json.loads(sections["WEB_META_V1"])["title"] == "保留"
    assert "[S01] 翻譯" in sections["TRANSLATED_SRT"]


def test_legacy_grid_round_trip(tmp_path):
    path = tmp_path / "grid.jpg"
    image = Image.new("RGB", (16, 16), "black")
    exif = image.getexif()
    exif[0x010E] = "https://example.com/v"
    image.save(path, exif=exif)
    assert video_meta.is_legacy_grid_jpg(path)

    web = video_meta.build_web_meta({
        "title": "T",
        "webpage_url": "https://example.com/v",
    })
    video_meta.write_grid_jpg_web_meta(path, web)
    result = video_meta.read_grid_jpg_meta(path)
    assert not video_meta.is_legacy_grid_jpg(path)
    assert result["url"] == "https://example.com/v"
    assert result["web_meta"]["title"] == "T"

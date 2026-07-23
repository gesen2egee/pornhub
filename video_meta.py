"""統一讀寫九宮格與影片內嵌 metadata。"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mutagen.mp4 import MP4
from PIL import Image

WEB_SECTION = "WEB_META_V1"
ORIGINAL_SECTION = "ORIGINAL_SRT"
TRANSLATED_SECTION = "TRANSLATED_SRT"
SECTION_PATTERN = re.compile(r"(?m)^===([A-Z0-9_]+)===\s*\n?")
WEB_FIELDS = (
    "extractor", "id", "title", "description", "uploader", "uploader_id",
    "tags", "categories", "cast", "view_count", "like_count", "comment_count",
    "average_rating", "duration", "duration_string", "upload_date", "timestamp",
    "thumbnail", "webpage_url", "age_limit",
)
LIST_FIELDS = {"tags", "categories", "cast"}


def _first(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value) if value not in (None, "") else None


def _duration_string(value: Any) -> str | None:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return None
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def build_web_meta(info: dict[str, Any]) -> dict[str, Any]:
    """將各 extractor 結果收斂成固定 WEB_META_V1 schema。"""
    web: dict[str, Any] = {"schema": "web_meta_v1"}
    for field in WEB_FIELDS:
        value = info.get(field)
        if field in LIST_FIELDS:
            if value is None:
                value = []
            elif not isinstance(value, list):
                value = [value]
            value = [item for item in value if item not in (None, "")]
        web[field] = value
    if not web["duration_string"]:
        web["duration_string"] = _duration_string(web["duration"])
    web["meta_written_at"] = datetime.now(timezone.utc).isoformat()
    return web


def parse_sections(comment: str | None) -> tuple[str, dict[str, str]]:
    text = comment or ""
    matches = list(SECTION_PATTERN.finditer(text))
    if not matches:
        return text.strip(), {}
    prefix = text[:matches[0].start()].strip()
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[match.end():end].strip("\r\n")
    return prefix, sections


def serialize_sections(prefix: str, sections: dict[str, str]) -> str:
    chunks = [prefix.strip()] if prefix.strip() else []
    preferred = (WEB_SECTION, ORIGINAL_SECTION, TRANSLATED_SECTION)
    names = [name for name in preferred if name in sections]
    names.extend(name for name in sections if name not in preferred)
    chunks.extend(
        f"==={name}===\n{sections[name].rstrip()}"
        for name in names if sections[name] is not None
    )
    return "\n".join(chunks).rstrip() + ("\n" if chunks else "")


def merge_comment(
    comment: str | None, *, web_meta: dict[str, Any] | None = None,
    original_srt: str | None = None, translated_srt: str | None = None,
) -> str:
    """只更新非 None 區段，並保留未知區段與既有一般 comment。"""
    prefix, sections = parse_sections(comment)
    if web_meta is not None:
        sections[WEB_SECTION] = json.dumps(
            web_meta, ensure_ascii=False, separators=(",", ":")
        )
    if original_srt is not None:
        sections[ORIGINAL_SECTION] = original_srt.rstrip()
    if translated_srt is not None:
        sections[TRANSLATED_SECTION] = translated_srt.rstrip()
    return serialize_sections(prefix, sections)


def _web_from_sections(sections: dict[str, str]) -> dict[str, Any] | None:
    try:
        value = json.loads(sections.get(WEB_SECTION, ""))
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def read_mp4_meta(path: str | Path) -> dict[str, Any]:
    media = MP4(str(path))
    tags = media.tags or {}
    raw = _first(tags.get("\xa9cmt"))
    _, sections = parse_sections(raw)
    return {
        "title": _first(tags.get("\xa9nam")),
        "artist": _first(tags.get("\xa9ART")),
        "date": _first(tags.get("\xa9day")),
        "web_meta": _web_from_sections(sections),
        "original_srt": sections.get(ORIGINAL_SECTION),
        "translated_srt": sections.get(TRANSLATED_SECTION),
        "raw_comment": raw,
    }


def merge_write_mp4_meta(
    path: str | Path, *, web_meta: dict[str, Any] | None = None,
    original_srt: str | None = None, translated_srt: str | None = None,
    base_comment: str | None = None,
) -> None:
    media = MP4(str(path))
    if media.tags is None:
        media.add_tags()
    tags = media.tags
    tags["\xa9cmt"] = [merge_comment(
        _first(tags.get("\xa9cmt")) if base_comment is None else base_comment,
        web_meta=web_meta,
        original_srt=original_srt, translated_srt=translated_srt,
    )]
    if web_meta is not None:
        if web_meta.get("title"):
            tags["\xa9nam"] = [str(web_meta["title"])]
        artist = web_meta.get("uploader")
        if not artist and web_meta.get("cast"):
            artist = web_meta["cast"][0]
        if artist:
            tags["\xa9ART"] = [str(artist)]
        if web_meta.get("upload_date"):
            date = str(web_meta["upload_date"])
            if len(date) == 8 and date.isdigit():
                date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
            tags["\xa9day"] = [date]
    media.save()


def _decode_user_comment(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        for prefix in (b"ASCII\x00\x00\x00", b"UNICODE\x00"):
            if value.startswith(prefix):
                value = value[len(prefix):]
                break
        return value.decode("utf-8", errors="replace").rstrip("\x00")
    return str(value)


def read_grid_jpg_meta(path: str | Path) -> dict[str, Any]:
    with Image.open(path) as image:
        exif = image.getexif()
        url = exif.get(0x010E)
        raw = _decode_user_comment(exif.get(0x9286))
    _, sections = parse_sections(raw)
    return {
        "url": str(url).strip() if url else None,
        "web_meta": _web_from_sections(sections),
        "raw_user_comment": raw or None,
    }


def is_legacy_grid_jpg(path: str | Path) -> bool:
    meta = read_grid_jpg_meta(path)
    return bool(meta["url"]) and meta["web_meta"] is None


def write_grid_jpg_web_meta(
    path: str | Path, web_meta: dict[str, Any], *, url: str | None = None,
) -> None:
    path = Path(path)
    with Image.open(path) as source:
        image = source.copy()
        exif = source.getexif()
    final_url = url or exif.get(0x010E)
    if final_url:
        exif[0x010E] = str(final_url)
    exif[0x9286] = merge_comment(
        _decode_user_comment(exif.get(0x9286)), web_meta=web_meta
    )
    image.save(path, quality=95, exif=exif)


def main() -> None:
    parser = argparse.ArgumentParser(description="讀取或匯出統一影片 metadata")
    subparsers = parser.add_subparsers(dest="command", required=True)
    show = subparsers.add_parser("show")
    show.add_argument("path", type=Path)
    export = subparsers.add_parser("export")
    export.add_argument("path", type=Path)
    export.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "show":
        value = (
            read_grid_jpg_meta(args.path)
            if args.path.suffix.lower() in {".jpg", ".jpeg"}
            else read_mp4_meta(args.path)
        )
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    output_dir = args.out_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    value = read_mp4_meta(args.path)
    if value["web_meta"]:
        (output_dir / f"{args.path.stem}.web_meta.json").write_text(
            json.dumps(value["web_meta"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    for key, suffix in (("original_srt", "orig.srt"), ("translated_srt", "translated.srt")):
        if value[key]:
            (output_dir / f"{args.path.stem}.{suffix}").write_text(value[key], encoding="utf-8")


if __name__ == "__main__":
    main()

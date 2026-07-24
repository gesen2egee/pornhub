"""Headless HTML → WEB_META-compatible info dict (same fields as yt-dlp build_web_meta)."""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from sites.http_util import fetch_html_best


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _meta_content(html: str, *names: str) -> str | None:
    for name in names:
        patterns = [
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(name)}["\']',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.I)
            if m:
                return _clean(m.group(1))
    return None


def _json_ld_blocks(html: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, list):
            blocks.extend(x for x in data if isinstance(x, dict))
        elif isinstance(data, dict):
            # @graph
            if isinstance(data.get("@graph"), list):
                blocks.extend(x for x in data["@graph"] if isinstance(x, dict))
            else:
                blocks.append(data)
    return blocks


def _duration_to_seconds(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    # ISO 8601 PT1H2M3S
    m = re.match(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
        s,
        re.I,
    )
    if m:
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        sec = int(m.group(3) or 0)
        return h * 3600 + mi * 60 + sec
    # mm:ss or hh:mm:ss
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
        parts = [int(x) for x in s.split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if s.isdigit():
        return int(s)
    return None


def _upload_date(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}"
    m = re.search(r"(\d{4})(\d{2})(\d{2})", value)
    if m:
        return m.group(0)
    return None


def scrape_info_from_html(html: str, webpage_url: str) -> dict[str, Any]:
    """Parse common page signals into a yt-dlp-like info dict."""
    info: dict[str, Any] = {
        "webpage_url": webpage_url,
        "age_limit": 18,
    }
    host = (urlsplit(webpage_url).hostname or "").lower()
    if host:
        info["extractor"] = host.split(".")[-2] if host.count(".") >= 1 else host

    title = _meta_content(html, "og:title", "twitter:title")
    if not title:
        m = re.search(r"<title>([^<]+)</title>", html, re.I)
        if m:
            title = _clean(m.group(1))
    if title:
        # strip common site suffixes
        title = re.sub(r"\s*[\|\-–]\s*(Jable\.TV|MissAV|HypnoTube|hanime\.tv).*$", "", title, flags=re.I)
        info["title"] = _clean(title)

    desc = _meta_content(html, "og:description", "description", "twitter:description")
    if desc:
        info["description"] = desc

    thumb = _meta_content(html, "og:image", "twitter:image")
    if thumb:
        info["thumbnail"] = thumb

    # JSON-LD VideoObject
    for block in _json_ld_blocks(html):
        t = str(block.get("@type") or "")
        if "Video" not in t and "Movie" not in t:
            continue
        if not info.get("title") and block.get("name"):
            info["title"] = _clean(str(block["name"]))
        if not info.get("description") and block.get("description"):
            info["description"] = _clean(str(block["description"]))
        if not info.get("thumbnail"):
            th = block.get("thumbnailUrl") or block.get("thumbnail")
            if isinstance(th, list) and th:
                th = th[0]
            if th:
                info["thumbnail"] = str(th)
        dur = _duration_to_seconds(block.get("duration"))
        if dur:
            info["duration"] = dur
        if block.get("uploadDate"):
            info["upload_date"] = _upload_date(str(block["uploadDate"]))
        if block.get("interactionStatistic"):
            pass
        author = block.get("author")
        if isinstance(author, dict) and author.get("name"):
            info.setdefault("uploader", _clean(str(author["name"])))
        elif isinstance(author, str):
            info.setdefault("uploader", _clean(author))

    # duration hints in page text / meta
    if not info.get("duration"):
        for pat in [
            r'itemprop=["\']duration["\'][^>]+content=["\']([^"\']+)["\']',
            r'"duration"\s*:\s*"?(PT[^"\',}\s]+|\d+)"?',
            r'data-duration=["\'](\d+)["\']',
            r'class=["\'][^"\']*duration[^"\']*["\'][^>]*>\s*([\d:]+)',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                dur = _duration_to_seconds(m.group(1))
                if dur:
                    info["duration"] = dur
                    break

    # tags / categories from common patterns
    tags = re.findall(
        r'href=["\'][^"\']*(?:/tags?/|/categories?/|/genres?/|/pornstars?/)([^"\'/?#]+)',
        html,
        re.I,
    )
    tags = [_clean(unquote(t.replace("-", " "))) for t in tags]
    tags = [t for t in tags if t and len(t) < 60]
    # dedupe preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for t in tags:
        key = t.casefold()
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    if uniq:
        info["tags"] = uniq[:40]

    # views
    m = re.search(
        r'(?:view[s]?|觀看|播放)[^\d]{0,12}([\d,\.]+)\s*([kKmM萬]?)',
        html,
    )
    if m:
        num = m.group(1).replace(",", "")
        try:
            val = float(num)
            unit = m.group(2)
            if unit in {"k", "K"}:
                val *= 1000
            elif unit in {"m", "M"}:
                val *= 1_000_000
            elif unit == "萬":
                val *= 10000
            info["view_count"] = int(val)
        except ValueError:
            pass

    # id from URL
    path = urlsplit(webpage_url).path or ""
    id_m = re.search(r"/videos?/([^/]+)/?", path) or re.search(r"/video[_-]?(\d+)", path, re.I)
    if id_m:
        info["id"] = id_m.group(1)

    # stream hints (for resolve_stream consumers)
    m3u8 = re.search(r"https?://[^\"'\s]+\.m3u8[^\"'\s]*", html, re.I)
    if m3u8:
        info["url"] = m3u8.group(0).replace("\\/", "/")

    return info


def scrape_page_info(webpage_url: str, timeout: int = 20) -> dict[str, Any]:
    """Fetch page (impersonate preferred) and parse meta."""
    html, method = fetch_html_best(webpage_url, timeout=timeout)
    info = scrape_info_from_html(html, webpage_url)
    info["_scrape_method"] = method
    info["_html_len"] = len(html or "")
    return info

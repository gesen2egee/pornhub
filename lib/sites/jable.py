"""Jable.tv adapter."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from sites.base import SiteAdapter
from sites.http_util import DEFAULT_UA


class JableAdapter(SiteAdapter):
    name = "jable"
    domains = ("jable.tv",)

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://jable.tv/search/{q}/"

    def is_single_video_url(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path or ""
        return bool(re.match(r"^/videos/[^/]+/?", path))

    def ydl_opts(self, purpose: str) -> dict[str, Any]:
        del purpose
        return {
            "http_headers": {
                "User-Agent": DEFAULT_UA,
                "Referer": "https://jable.tv/",
            },
        }

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html = self.fetch_html(page_url)
        except Exception:
            return []
        paths = re.findall(r'href="(https?://jable\.tv/videos/[^"]+/)"', html)
        paths += re.findall(r'href="(/videos/[^"]+/)"', html)
        seen: set[str] = set()
        urls: list[str] = []
        for path in paths:
            full = path if path.startswith("http") else self.absolute_url("https://jable.tv/", path)
            if full not in seen:
                seen.add(full)
                urls.append(full)
        return urls

    def resolve_stream(
        self,
        video_url: str,
        prefer_lowest: bool = False,
    ) -> dict[str, Any] | None:
        """yt-dlp may refuse jable as piracy; scrape HLS from page when present."""
        del prefer_lowest
        try:
            html = self.fetch_html(video_url)
        except Exception:
            return None
        m = re.search(
            r"https?://[^\"'\s]+\.m3u8[^\"'\s]*",
            html,
            re.I,
        )
        if not m:
            return None
        stream = m.group(0).replace("\\/", "/")
        title_m = re.search(r"<title>([^<]+)</title>", html, re.I)
        title = title_m.group(1).strip() if title_m else None
        return {
            "url": stream,
            "http_headers": {
                "User-Agent": DEFAULT_UA,
                "Referer": "https://jable.tv/",
            },
            "info": {
                "title": title,
                "webpage_url": video_url,
                "url": stream,
            },
        }

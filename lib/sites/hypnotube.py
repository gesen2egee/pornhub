"""HypnoTube site adapter."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from sites.base import SiteAdapter
from sites.http_util import DEFAULT_UA, fetch_html_best
from sites.scrape_meta import scrape_info_from_html


class HypnoTubeAdapter(SiteAdapter):
    name = "hypnotube"
    domains = ("hypnotube.com",)

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://hypnotube.com/search/{q}/"

    def is_single_video_url(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path or ""
        # /video/slug-name-12345.html
        return bool(re.search(r"/video/[^/]+-\d+\.html", path, re.I))

    def ydl_opts(self, purpose: str) -> dict[str, Any]:
        del purpose
        return {
            "http_headers": {
                "User-Agent": DEFAULT_UA,
                "Referer": "https://hypnotube.com/",
            },
        }

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html, _ = fetch_html_best(page_url)
        except Exception:
            return []
        paths = re.findall(
            r'href="(https?://(?:www\.)?hypnotube\.com/video/[^"]+-\d+\.html)"',
            html,
            re.I,
        )
        paths += re.findall(r'href="(/video/[^"]+-\d+\.html)"', html, re.I)
        seen: set[str] = set()
        urls: list[str] = []
        for path in paths:
            full = path if path.startswith("http") else self.absolute_url("https://hypnotube.com/", path)
            full = full.split("#")[0]
            if full not in seen:
                seen.add(full)
                urls.append(full)
        return urls

    def extract_info(self, video_url: str, purpose: str) -> dict[str, Any] | None:
        del purpose
        try:
            html, method = fetch_html_best(video_url)
        except Exception:
            return None
        info = scrape_info_from_html(html, video_url)
        info["extractor"] = "HypnoTube"
        info["_scrape_method"] = method
        return info if info.get("title") or info.get("url") else None

    def resolve_stream(
        self,
        video_url: str,
        prefer_lowest: bool = False,
    ) -> dict[str, Any] | None:
        del prefer_lowest
        info = self.extract_info(video_url, "info")
        if not info or not info.get("url"):
            return None
        return {
            "url": info["url"],
            "http_headers": {
                "User-Agent": DEFAULT_UA,
                "Referer": "https://hypnotube.com/",
            },
            "info": info,
        }

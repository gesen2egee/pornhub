"""Tube8 site adapter."""

from __future__ import annotations

import re
import urllib.parse

from sites.base import SiteAdapter


class Tube8Adapter(SiteAdapter):
    name = "tube8"
    domains = ("tube8.com",)

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://www.tube8.com/search.html?q={q}"

    def is_single_video_url(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path or ""
        return bool(re.search(r"/porn-video/\d+", path)) or bool(
            re.search(r"/videos?/[^/]+/\d+", path)
        )

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html = self.fetch_html(page_url)
        except Exception:
            return []
        ids = re.findall(r'data-video-id="(\d+)"', html)
        paths = re.findall(r'href="(/porn-video/\d+/[^"]*)"', html)
        if not paths and ids:
            paths = [f"/porn-video/{vid}/" for vid in ids]
        if not paths:
            paths = re.findall(
                r'href="((?:https://www\.tube8\.com)?/(?:[^"]+/)?videos?/[^"]+/\d+/)"',
                html,
                re.I,
            )
        seen: set[str] = set()
        urls: list[str] = []
        base = "https://www.tube8.com"
        for path in paths:
            full = path if path.startswith("http") else self.absolute_url(base + "/", path)
            if "/search" in full or full in seen:
                continue
            seen.add(full)
            urls.append(full)
        return urls

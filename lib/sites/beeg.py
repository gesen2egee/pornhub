"""Beeg site adapter."""

from __future__ import annotations

import re
import urllib.parse

from sites.base import SiteAdapter


class BeegAdapter(SiteAdapter):
    name = "beeg"
    domains = ("beeg.com",)

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://beeg.com/search?q={q}"

    def is_single_video_url(self, url: str) -> bool:
        path = (urllib.parse.urlsplit(url).path or "").strip("/")
        return bool(re.fullmatch(r"\d+", path))

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html = self.fetch_html(page_url)
        except Exception:
            return []
        ids = re.findall(r'href="/(?:video/)?(\d{5,})"', html)
        if not ids:
            ids = re.findall(r'"id"\s*:\s*(\d{5,})', html)
        seen: set[str] = set()
        urls: list[str] = []
        for vid in ids:
            if vid in seen:
                continue
            seen.add(vid)
            urls.append(f"https://beeg.com/{vid}")
        return urls

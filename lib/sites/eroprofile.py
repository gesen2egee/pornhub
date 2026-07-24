"""EroProfile site adapter."""

from __future__ import annotations

import re
import urllib.parse

from sites.base import SiteAdapter


class EroProfileAdapter(SiteAdapter):
    name = "eroprofile"
    domains = ("eroprofile.com",)

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://www.eroprofile.com/m/videos/search?text={q}"

    def is_single_video_url(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path or ""
        return bool(re.search(r"/m/videos/view/", path))

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html = self.fetch_html(page_url)
        except Exception:
            return []
        paths = re.findall(r'href="(/m/videos/view/[^"?#]+)"', html)
        seen: set[str] = set()
        urls: list[str] = []
        base = "https://www.eroprofile.com"
        for path in paths:
            full = self.absolute_url(base + "/", path)
            if full not in seen:
                seen.add(full)
                urls.append(full)
        return urls

"""YouPorn site adapter."""

from __future__ import annotations

import re
import urllib.parse

from sites.base import SiteAdapter


class YouPornAdapter(SiteAdapter):
    name = "youporn"
    domains = ("youporn.com",)

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://www.youporn.com/search/?query={q}"

    def is_single_video_url(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path or ""
        return bool(re.search(r"/watch/\d+", path))

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html = self.fetch_html(page_url)
        except Exception:
            return []
        paths = re.findall(r'href="(/watch/\d+/[^"]*)"', html)
        seen: set[str] = set()
        urls: list[str] = []
        base = "https://www.youporn.com"
        for path in paths:
            full = self.absolute_url(base + "/", path)
            if full not in seen:
                seen.add(full)
                urls.append(full)
        return urls

"""XNXX site adapter."""

from __future__ import annotations

import re
import urllib.parse

from sites.base import SiteAdapter


class XNXXAdapter(SiteAdapter):
    name = "xnxx"
    domains = ("xnxx.com",)

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://www.xnxx.com/search/{q}"

    def is_single_video_url(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path or ""
        return bool(re.search(r"/video[-.]", path))

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html = self.fetch_html(page_url)
        except Exception:
            return []
        paths = re.findall(r'href="(/video-[^"]+)"', html)
        if not paths:
            paths = re.findall(r'(/video\.[a-z0-9]+/[^"\'\s<>]+)', html, re.I)
        if not paths:
            return []
        seen: set[str] = set()
        urls: list[str] = []
        base = f"{urllib.parse.urlsplit(page_url).scheme}://{urllib.parse.urlsplit(page_url).netloc}"
        for path in paths:
            path = path.split("#")[0]
            if path in seen:
                continue
            seen.add(path)
            urls.append(self.absolute_url(base + "/", path))
        return urls

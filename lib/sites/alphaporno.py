"""AlphaPorno site adapter."""

from __future__ import annotations

import re
import urllib.parse

from sites.base import SiteAdapter


class AlphaPornoAdapter(SiteAdapter):
    name = "alphaporno"
    domains = ("alphaporno.com",)

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://www.alphaporno.com/search/{q}/"

    def get_start_page(self, url: str) -> int:
        m = re.search(r"/(\d+)/?$", urllib.parse.urlsplit(url or "").path or "")
        if m:
            return int(m.group(1))
        return super().get_start_page(url)

    def build_page_url(self, url: str, page_num: int) -> str:
        parsed = urllib.parse.urlsplit(url)
        segments = [s for s in parsed.path.split("/") if s]
        if segments and segments[-1].isdigit():
            if page_num <= 1:
                segments = segments[:-1]
            else:
                segments[-1] = str(page_num)
        elif page_num > 1:
            segments.append(str(page_num))
        path = "/" + "/".join(segments) + ("/" if segments else "")
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
        )

    def is_single_video_url(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path or ""
        return bool(re.search(r"/videos/\d+", path)) or bool(
            re.search(r"/[a-z0-9-]+-\d+\.html$", path, re.I)
        )

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html = self.fetch_html(page_url)
        except Exception:
            return []
        paths = re.findall(r'href="(/videos/\d+/[^"]*)"', html)
        if not paths:
            paths = re.findall(r'href="(/[a-z0-9-]+-\d+\.html)"', html, re.I)
        seen: set[str] = set()
        urls: list[str] = []
        base = "https://www.alphaporno.com"
        for path in paths:
            full = self.absolute_url(base + "/", path)
            if full not in seen:
                seen.add(full)
                urls.append(full)
        return urls

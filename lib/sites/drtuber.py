"""DrTuber site adapter."""

from __future__ import annotations

import re
import urllib.parse

from sites.base import SiteAdapter


class DrTuberAdapter(SiteAdapter):
    name = "drtuber"
    domains = ("drtuber.com",)

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://www.drtuber.com/search/videos/{q}"

    def get_start_page(self, url: str) -> int:
        path = (urllib.parse.urlsplit(url or "").path or "").strip("/")
        if path.isdigit():
            return int(path)
        return super().get_start_page(url)

    def build_page_url(self, url: str, page_num: int) -> str:
        """Homepage paging is path /2, /3 (not ?page=)."""
        parsed = urllib.parse.urlsplit(url)
        segments = [s for s in parsed.path.split("/") if s]
        # category lists may use trailing page number
        if segments and segments[-1].isdigit():
            if page_num <= 1:
                segments = segments[:-1]
            else:
                segments[-1] = str(page_num)
        elif page_num > 1:
            segments.append(str(page_num))
        path = "/" + "/".join(segments)
        if page_num > 1 or path != "/":
            if not path.endswith("/") and not path.endswith(str(page_num)):
                pass
            if page_num > 1 and not path.endswith("/"):
                path = path  # /2 style without trailing slash
        if page_num <= 1 and not segments:
            path = "/"
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, path or "/", parsed.query, parsed.fragment)
        )

    def is_single_video_url(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path or ""
        return bool(re.search(r"/video/\d+", path))

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html = self.fetch_html(page_url)
        except Exception:
            return []
        paths = re.findall(r'href="(/video/\d+/[^"]*)"', html)
        if not paths:
            paths = re.findall(r'href="(/video/\d+)"', html)
        seen: set[str] = set()
        urls: list[str] = []
        base = "https://www.drtuber.com"
        for path in paths:
            full = self.absolute_url(base + "/", path)
            if full not in seen:
                seen.add(full)
                urls.append(full)
        return urls

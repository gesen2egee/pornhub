"""Eporner site adapter (default keyword search)."""

from __future__ import annotations

import re
import urllib.parse

from sites.base import SiteAdapter

DEFAULT_URL = "https://www.eporner.com/country-top/tw/"


class EpornerAdapter(SiteAdapter):
    name = "eporner"
    domains = ("eporner.com",)

    def search_url(self, keyword: str) -> str:
        encoded = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://www.eporner.com/tag/{encoded}/top-rated/"

    def get_start_page(self, url: str) -> int:
        m = re.search(r"[?&]page=(\d+)", url or "")
        if m:
            return int(m.group(1))
        parsed = urllib.parse.urlsplit(url or "")
        segments = [s for s in parsed.path.split("/") if s]
        if len(segments) >= 3 and segments[-1].isdigit():
            return int(segments[-1])
        if len(segments) >= 4 and segments[-2].isdigit():
            return int(segments[-2])
        return 1

    def build_page_url(self, url: str, page_num: int) -> str:
        parsed = urllib.parse.urlsplit(url)
        segments = [s for s in parsed.path.split("/") if s]
        page_index = None
        if len(segments) >= 3 and segments[-1].isdigit():
            page_index = len(segments) - 1
        elif len(segments) >= 4 and segments[-2].isdigit():
            page_index = len(segments) - 2

        if page_index is not None:
            if page_num <= 1:
                segments.pop(page_index)
            else:
                segments[page_index] = str(page_num)
        elif page_num > 1:
            if len(segments) >= 3:
                segments.insert(len(segments) - 1, str(page_num))
            else:
                segments.append(str(page_num))

        path = "/" + "/".join(segments) + "/"
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
        )

    def is_single_video_url(self, url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        return bool(re.match(r"^/(?:video-|hd-porn/|embed/)", parsed.path or ""))

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html = self.fetch_html(page_url)
        except Exception:
            return []
        paths = re.findall(
            r'href=["\'](/(?:video-[^"\'?#]+|hd-porn/[^"\'?#]+))',
            html,
            re.IGNORECASE,
        )
        if not paths:
            return []
        seen: set[str] = set()
        urls: list[str] = []
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            urls.append(self.absolute_url("https://www.eporner.com/", path))
        return urls

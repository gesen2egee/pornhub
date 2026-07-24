"""EMPFlix site adapter."""

from __future__ import annotations

import re
import urllib.parse

from sites.base import SiteAdapter


class EMPFlixAdapter(SiteAdapter):
    name = "empflix"
    domains = ("empflix.com",)

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://www.empflix.com/search.php?what={q}"

    def is_single_video_url(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path or ""
        return bool(re.search(r"/video\d+", path, re.I)) or bool(
            re.search(r"/videos/\d+", path)
        )

    def build_page_url(self, url: str, page_num: int) -> str:
        # /new/2 style
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

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html = self.fetch_html(page_url)
        except Exception:
            return []
        paths = re.findall(
            r'href="((?:https://www\.empflix\.com)?/[^"]+/video\d+)"',
            html,
            re.I,
        )
        if not paths:
            paths = re.findall(r'href="(/videos/\d+/[^"]*)"', html)
        seen: set[str] = set()
        urls: list[str] = []
        base = "https://www.empflix.com"
        for path in paths:
            full = path if path.startswith("http") else self.absolute_url(base + "/", path)
            if full not in seen:
                seen.add(full)
                urls.append(full)
        return urls

"""xHamster site adapter."""

from __future__ import annotations

import re
import urllib.parse

from sites.base import SiteAdapter


class XHamsterAdapter(SiteAdapter):
    name = "xhamster"
    domains = ("xhamster.com", "xhamster.desi", "xhamster2.com", "xhamster3.com")

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://xhamster.com/search/{q}"

    def is_single_video_url(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path or ""
        # Only real video pages under /videos/<slug>, not /creators/videos/...
        return bool(re.match(r"^/videos/[^/]+/?$", path))

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html = self.fetch_html(page_url)
        except Exception:
            return []
        paths = re.findall(r'href="(https?://[^"]+/videos/[^"]+)"', html)
        if not paths:
            paths = re.findall(r'href="(/videos/[^"]+)"', html)
        seen: set[str] = set()
        urls: list[str] = []
        base = f"{urllib.parse.urlsplit(page_url).scheme}://{urllib.parse.urlsplit(page_url).netloc}"
        for path in paths:
            full = path if path.startswith("http") else self.absolute_url(base + "/", path)
            if not self.is_single_video_url(full):
                # Allow absolute with host
                p = urllib.parse.urlsplit(full).path or ""
                if not re.match(r"^/videos/[^/]+/?$", p):
                    continue
            if full not in seen:
                seen.add(full)
                urls.append(full)
        return urls

"""SpankBang site adapter."""

from __future__ import annotations

import re
import urllib.parse

from sites.base import SiteAdapter


class SpankBangAdapter(SiteAdapter):
    name = "spankbang"
    domains = ("spankbang.com",)

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://spankbang.com/s/{q}/"

    def is_single_video_url(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path or ""
        # /ab12cd/video-title
        return bool(re.match(r"^/[a-z0-9]+/[^/]+", path, re.I)) and "/s/" not in path

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html = self.fetch_html(page_url)
        except Exception:
            # Cloudflare often blocks bare urllib; fall through to flat.
            return []
        paths = re.findall(r'href="(/[a-z0-9]+/[^"?#]+)"', html, re.I)
        seen: set[str] = set()
        urls: list[str] = []
        base = f"{urllib.parse.urlsplit(page_url).scheme}://{urllib.parse.urlsplit(page_url).netloc}"
        skip_prefixes = ("/s/", "/tags/", "/categories/", "/users/", "/playlist/")
        for path in paths:
            low = path.lower()
            if any(low.startswith(p) for p in skip_prefixes):
                continue
            if path.count("/") < 2:
                continue
            if path in seen:
                continue
            seen.add(path)
            urls.append(self.absolute_url(base + "/", path))
        return urls

"""91porn adapter (optional Netscape cookies via SITE_91PORN_COOKIES)."""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any

from sites.base import SiteAdapter
from sites.http_util import DEFAULT_UA


class Porn91Adapter(SiteAdapter):
    name = "91porn"
    domains = ("91porn.com", "91porna.com")

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://91porn.com/search_result.php?search_id={q}"

    def is_single_video_url(self, url: str) -> bool:
        return "view_video.php" in url or "viewkey=" in url

    def ydl_opts(self, purpose: str) -> dict[str, Any]:
        del purpose
        opts: dict[str, Any] = {
            "http_headers": {
                "User-Agent": DEFAULT_UA,
                "Referer": "https://91porn.com/",
            },
        }
        cookies = os.environ.get("SITE_91PORN_COOKIES", "").strip()
        if cookies and os.path.isfile(cookies):
            opts["cookiefile"] = cookies
        return opts

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html = self.fetch_html(page_url)
        except Exception:
            return []
        keys = re.findall(r"viewkey=([a-zA-Z0-9]+)", html)
        seen: set[str] = set()
        urls: list[str] = []
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            urls.append(f"https://91porn.com/view_video.php?viewkey={key}")
        return urls

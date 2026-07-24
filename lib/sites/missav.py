"""MissAV adapter — list HTML + yt-dlp with referer headers."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from sites.base import SiteAdapter
from sites.http_util import DEFAULT_UA, fetch_html_best
from sites.scrape_meta import scrape_info_from_html


class MissAVAdapter(SiteAdapter):
    name = "missav"
    domains = ("missav.com", "missav.ws", "missav.ai", "missav.live")

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://missav.ai/search/{q}"

    def is_single_video_url(self, url: str) -> bool:
        path = (urllib.parse.urlsplit(url).path or "").strip("/")
        if not path:
            return False
        low = path.lower()
        if any(
            low.startswith(p)
            for p in (
                "search",
                "dm",
                "fonts/",
                "css/",
                "js/",
                "images/",
                "genres",
                "actresses",
                "makers",
                "login",
            )
        ):
            return False
        if any(low.endswith(ext) for ext in (".woff", ".woff2", ".css", ".js", ".png", ".jpg", ".svg")):
            return False
        # typical: /en/abc-123 or /dm14/en/code-001
        parts = path.split("/")
        slug = parts[-1]
        return bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", slug, re.I))

    def ydl_opts(self, purpose: str) -> dict[str, Any]:
        del purpose
        return {
            "http_headers": {
                "User-Agent": DEFAULT_UA,
                "Referer": "https://missav.ai/",
            },
            "extractor_args": {"generic": {"impersonate": ["chrome"]}},
        }

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html, _ = fetch_html_best(page_url)
        except Exception:
            return []
        hrefs = re.findall(r'href="(https?://[^"]*missav[^"]+)"', html, re.I)
        hrefs += re.findall(r'href="(/[^"]+)"', html)
        seen: set[str] = set()
        urls: list[str] = []
        base = f"{urllib.parse.urlsplit(page_url).scheme}://{urllib.parse.urlsplit(page_url).netloc}"
        for href in hrefs:
            full = href if href.startswith("http") else self.absolute_url(base + "/", href)
            if not self.match_url(full):
                continue
            if not self.is_single_video_url(full):
                continue
            if full not in seen:
                seen.add(full)
                urls.append(full)
        return urls

    def extract_info(self, video_url: str, purpose: str) -> dict[str, Any] | None:
        del purpose
        try:
            html, method = fetch_html_best(video_url)
        except Exception:
            return None
        info = scrape_info_from_html(html, video_url)
        info["extractor"] = "MissAV"
        info["_scrape_method"] = method
        return info if info.get("title") or info.get("url") else None

    def resolve_stream(
        self,
        video_url: str,
        prefer_lowest: bool = False,
    ) -> dict[str, Any] | None:
        del prefer_lowest
        info = self.extract_info(video_url, "info")
        if not info or not info.get("url"):
            return None
        return {
            "url": info["url"],
            "http_headers": {
                "User-Agent": DEFAULT_UA,
                "Referer": "https://missav.ai/",
            },
            "info": info,
        }

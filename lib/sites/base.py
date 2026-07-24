"""Site adapter base class for multi-site capture/download."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

import yt_dlp

from sites.http_util import fetch_html, host_matches


class SiteAdapter:
    """Thin per-site rules: list, paging, search, optional stream hooks."""

    name: str = "base"
    domains: tuple[str, ...] = ()

    def match_url(self, url: str) -> bool:
        if not url or not isinstance(url, str):
            return False
        return host_matches(url, self.domains)

    def search_url(self, keyword: str) -> str:
        raise NotImplementedError(f"{self.name} does not define search_url")

    def get_start_page(self, url: str) -> int:
        if not url:
            return 1
        m = re.search(r"[?&]page=(\d+)", url)
        if m:
            return int(m.group(1))
        return 1

    def build_page_url(self, url: str, page_num: int) -> str:
        if page_num <= 1 and "page=" not in url:
            return url
        if re.search(r"[?&]page=\d+", url):
            return re.sub(r"([?&]page=)\d+", rf"\g<1>{page_num}", url)
        if "?" in url:
            return f"{url}&page={page_num}"
        return f"{url}?page={page_num}"

    def is_single_video_url(self, url: str) -> bool:
        return False

    def ydl_opts(self, purpose: str) -> dict[str, Any]:
        del purpose
        return {}

    def extract_info(self, video_url: str, purpose: str) -> dict[str, Any] | None:
        del video_url, purpose
        return None

    def resolve_stream(
        self,
        video_url: str,
        prefer_lowest: bool = False,
    ) -> dict[str, Any] | None:
        del video_url, prefer_lowest
        return None

    def extract_list_urls(self, page_url: str) -> list[str]:
        html_urls = self.extract_list_urls_from_html(page_url)
        if html_urls:
            return html_urls
        return self.extract_list_urls_flat(page_url)

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        del page_url
        return []

    def extract_list_urls_flat(self, page_url: str) -> list[str]:
        ydl_opts = {
            "extract_flat": True,
            "quiet": True,
            "no_warnings": True,
            # Helps some Cloudflare-protected list pages.
            "extractor_args": {"generic": {"impersonate": ["chrome"]}},
            **self.ydl_opts("list"),
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                res = ydl.extract_info(page_url, download=False)
        except Exception:
            return []
        if not res:
            return []
        entries = res.get("entries")
        if not entries:
            # Single video page returned as non-playlist
            webpage = res.get("webpage_url") or res.get("url") or page_url
            if webpage and str(webpage).startswith("http"):
                return [str(webpage)]
            return []
        urls: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            if not entry:
                continue
            u = entry.get("webpage_url") or entry.get("url")
            if not u:
                continue
            u = str(u)
            if not u.startswith("http"):
                # Some extractors return ids only
                continue
            if u not in seen:
                seen.add(u)
                urls.append(u)
        return urls

    def fetch_html(self, url: str, timeout: int = 15) -> str:
        return fetch_html(url, timeout=timeout)

    def absolute_url(self, base: str, path: str) -> str:
        return urllib.parse.urljoin(base, path)

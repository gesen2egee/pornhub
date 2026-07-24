"""Pornhub site adapter (list + paging; download via yt-dlp only)."""

from __future__ import annotations

import re
import urllib.parse

from sites.base import SiteAdapter


class PornhubAdapter(SiteAdapter):
    name = "pornhub"
    domains = ("pornhub.com", "pornhub.org", "pornhub.net")

    def search_url(self, keyword: str) -> str:
        q = urllib.parse.quote(keyword.strip(), safe="")
        return f"https://www.pornhub.com/video/search?search={q}&o=mv&t=a"

    def is_single_video_url(self, url: str) -> bool:
        if "viewkey=" not in url:
            return False
        if "video/search" in url or ("video" in url and "o=" in url):
            return False
        return "view_video.php" in url or "pornhub." in url

    def normalize_video_url(self, url: str) -> str | None:
        m = re.search(r"viewkey=([a-zA-Z0-9]+)", url)
        if not m:
            return None
        return f"https://www.pornhub.com/view_video.php?viewkey={m.group(1)}"

    def extract_list_urls_from_html(self, page_url: str) -> list[str]:
        try:
            html = self.fetch_html(page_url)
        except Exception:
            return []
        main_block = html
        m = re.search(
            r'<ul[^>]*id="videoSearchResult"[^>]*>(.*?)</ul>',
            html,
            re.DOTALL,
        )
        if not m:
            m = re.search(
                r'<ul[^>]*class="[^"]*videos[^"]*"[^>]*>(.*?)</ul>',
                html,
                re.DOTALL,
            )
        if m:
            main_block = m.group(1)

        viewkeys = re.findall(
            r'href="/view_video.php\?viewkey=([a-zA-Z0-9]+)"',
            main_block,
        )
        if not viewkeys:
            viewkeys = re.findall(r"viewkey=([a-zA-Z0-9]+)", main_block)
        if not viewkeys:
            return []
        seen: set[str] = set()
        urls: list[str] = []
        for key in viewkeys:
            if key in seen:
                continue
            seen.add(key)
            urls.append(f"https://www.pornhub.com/view_video.php?viewkey={key}")
        return urls

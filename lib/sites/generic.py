"""Fallback adapter for unknown hosts."""

from __future__ import annotations

from sites.base import SiteAdapter


class GenericAdapter(SiteAdapter):
    name = "generic"
    domains = ()

    def match_url(self, url: str) -> bool:
        # Registry uses this only as explicit fallback.
        del url
        return False

    def search_url(self, keyword: str) -> str:
        raise NotImplementedError("generic has no default search")

    def is_single_video_url(self, url: str) -> bool:
        # Unknown hosts: treat as single URL; extract_flat will clarify.
        del url
        return False

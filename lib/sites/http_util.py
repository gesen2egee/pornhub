"""Shared HTTP helpers for site list scraping."""

from __future__ import annotations

import urllib.parse
import urllib.request

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def default_headers(url: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cookie": "age_verified=1; platform=pc; accessAgeDisclaimerPH=1",
    }
    if url:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme and parsed.netloc:
            headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    return headers


def fetch_html(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=default_headers(url))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def hostname_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def host_matches(url: str, domains: tuple[str, ...]) -> bool:
    host = hostname_of(url)
    if not host:
        return False
    for domain in domains:
        d = domain.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False

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
    """Plain urllib fetch (no TLS fingerprint impersonation)."""
    req = urllib.request.Request(url, headers=default_headers(url))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_html_impersonate(url: str, timeout: int = 20, impersonate: str = "chrome") -> str:
    """
    Headless-ish HTML fetch via curl_cffi browser TLS impersonation.
    Falls back to urllib if curl_cffi is unavailable.
    """
    try:
        from curl_cffi import requests as cf_requests
    except ImportError:
        return fetch_html(url, timeout=timeout)

    headers = default_headers(url)
    resp = cf_requests.get(
        url,
        headers=headers,
        timeout=timeout,
        impersonate=impersonate,
        allow_redirects=True,
    )
    resp.raise_for_status()
    text = resp.text or ""
    if isinstance(text, bytes):
        return text.decode("utf-8", errors="ignore")
    return text


def fetch_html_best(url: str, timeout: int = 20) -> tuple[str, str]:
    """
    Try impersonate first, then plain urllib.
    Returns (html, method) where method is 'curl_cffi' or 'urllib'.
    """
    try:
        html = fetch_html_impersonate(url, timeout=timeout)
        if html and len(html) > 500 and "just a moment" not in html.lower():
            return html, "curl_cffi"
    except Exception:
        pass
    html = fetch_html(url, timeout=timeout)
    return html, "urllib"


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

"""Shared playable-info resolution for capture and download."""

from __future__ import annotations

from typing import Any

import yt_dlp

from sites.base import SiteAdapter


def _stream_from_info(info: dict[str, Any] | None) -> tuple[str | None, dict[str, Any]]:
    if not info:
        return None, {}
    url = info.get("url")
    headers = dict(info.get("http_headers") or {})
    if url:
        return str(url), headers
    # requested_formats / formats fallback: first with url
    for key in ("requested_formats", "formats"):
        for fmt in info.get(key) or []:
            if fmt and fmt.get("url"):
                return str(fmt["url"]), dict(fmt.get("http_headers") or headers)
    return None, headers


def _build_format(purpose: str, prefer_lowest: bool) -> str:
    if prefer_lowest or purpose == "download_low":
        return "worstvideo+worstaudio/worst"
    if purpose == "download_full":
        return "bestvideo*+bestaudio/best"
    if purpose == "info":
        return (
            "bestvideo[height<=720][protocol!=m3u8_native]"
            "/best[height<=720][protocol!=m3u8_native]"
            "/bestvideo[height<=720]/best"
        )
    return "best"


def resolve_playable(
    adapter: SiteAdapter,
    video_url: str,
    purpose: str = "info",
    prefer_lowest: bool = False,
    base_ydl_opts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Resolve title/duration/stream for one video.

    Order: adapter.extract_info → adapter.resolve_stream → yt-dlp + ydl_opts.
    No site-specific HTML download fallbacks.
    """
    info: dict[str, Any] | None = None
    stream_url: str | None = None
    http_headers: dict[str, Any] = {}
    source = "none"

    custom = adapter.extract_info(video_url, purpose)
    if custom:
        info = dict(custom)
        info.setdefault("webpage_url", video_url)
        stream_url, http_headers = _stream_from_info(info)
        source = "extract_info"

    if not stream_url:
        resolved = adapter.resolve_stream(video_url, prefer_lowest=prefer_lowest)
        if resolved and resolved.get("url"):
            stream_url = str(resolved["url"])
            http_headers = dict(resolved.get("http_headers") or {})
            if resolved.get("info"):
                merged = dict(resolved["info"])
                if info:
                    merged = {**merged, **{k: v for k, v in info.items() if v is not None}}
                info = merged
                info.setdefault("webpage_url", video_url)
            source = "resolve_stream"

    if not stream_url or info is None:
        fmt = _build_format(purpose, prefer_lowest)
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "format": fmt,
        }
        if purpose == "download_full":
            ydl_opts["format_sort"] = ["res:1080"]
        if base_ydl_opts:
            ydl_opts.update(base_ydl_opts)
        ydl_opts.update(adapter.ydl_opts(purpose))
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                extracted = ydl.extract_info(video_url, download=False)
        except Exception:
            extracted = None
        if extracted:
            extracted.setdefault("webpage_url", video_url)
            if info is None:
                info = extracted
            else:
                for key, value in extracted.items():
                    if info.get(key) is None:
                        info[key] = value
            if not stream_url:
                stream_url, hdrs = _stream_from_info(extracted)
                if stream_url:
                    http_headers = hdrs or http_headers
                    source = "yt_dlp"

    # If yt-dlp failed but adapter can scrape a stream, try again after ytdlp
    # (covers adapters that only implement resolve_stream for blocked extractors).
    if not stream_url:
        resolved = adapter.resolve_stream(video_url, prefer_lowest=prefer_lowest)
        if resolved and resolved.get("url"):
            stream_url = str(resolved["url"])
            http_headers = dict(resolved.get("http_headers") or {})
            if resolved.get("info"):
                merged = dict(resolved["info"])
                if info:
                    for key, value in info.items():
                        if merged.get(key) is None:
                            merged[key] = value
                info = merged
                info.setdefault("webpage_url", video_url)
            source = "resolve_stream"

    if info is None:
        info = {"webpage_url": video_url}
    else:
        info.setdefault("webpage_url", video_url)
    if not info.get("title"):
        # last-resort title from URL path
        path = video_url.rstrip("/").split("/")[-1]
        if path and path not in {"view_video.php", "watch"}:
            info["title"] = path.split("?")[0] or video_url

    return {
        "info": info,
        "stream_url": stream_url,
        "http_headers": http_headers or {},
        "source": source,
        "webpage_url": info.get("webpage_url") or video_url,
    }

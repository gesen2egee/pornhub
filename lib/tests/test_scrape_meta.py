"""Offline tests for headless HTML meta scrape → WEB_META fields."""

from __future__ import annotations

import video_meta
from sites.scrape_meta import scrape_info_from_html


SAMPLE = """
<html><head>
<title>ABC-123 Sample Title | Jable.TV</title>
<meta property="og:title" content="ABC-123 Sample Title" />
<meta property="og:description" content="Hot scene description here" />
<meta property="og:image" content="https://cdn.example/thumb.jpg" />
<script type="application/ld+json">
{"@type":"VideoObject","name":"ABC-123 Sample Title","duration":"PT12M30S",
 "uploadDate":"2026-01-15","author":{"@type":"Person","name":"StudioX"},
 "thumbnailUrl":"https://cdn.example/thumb.jpg"}
</script>
</head><body>
<a href="/tags/beauty/">beauty</a>
<a href="/categories/jav/">jav</a>
<span class="duration">12:30</span>
Views: 12,345
<script>var hls="https://cdn.example/play/index.m3u8";</script>
</body></html>
"""


def test_scrape_info_fills_web_meta_fields():
    info = scrape_info_from_html(SAMPLE, "https://jable.tv/videos/abc-123/")
    web = video_meta.build_web_meta(info)
    assert web["title"] == "ABC-123 Sample Title"
    assert web["webpage_url"].endswith("/abc-123/")
    assert web["duration"] == 12 * 60 + 30
    assert web["upload_date"] == "20260115"
    assert web["uploader"] == "StudioX"
    assert web["thumbnail"]
    assert "beauty" in (web.get("tags") or []) or "jav" in (web.get("tags") or [])
    assert info.get("url") and ".m3u8" in info["url"]

"""Offline unit tests for multi-site registry and adapters."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import sites
from sites.base import SiteAdapter
from sites.eporner import EpornerAdapter
from sites.pornhub import PornhubAdapter
from sites.resolve import resolve_playable
from sites.xvideos import XVideosAdapter


def test_default_adapter_is_eporner():
    assert sites.default_adapter().name == "eporner"


def test_match_known_hosts():
    assert sites.get_adapter_for_url("https://www.pornhub.com/video/search?search=a").name == "pornhub"
    assert sites.get_adapter_for_url("https://www.eporner.com/tag/x/").name == "eporner"
    assert sites.get_adapter_for_url("https://www.xvideos.com/video123/title").name == "xvideos"
    assert sites.get_adapter_for_url("https://xhamster.com/videos/foo").name == "xhamster"
    assert sites.get_adapter_for_url("https://www.xnxx.com/video-abc/t").name == "xnxx"
    assert sites.get_adapter_for_url("https://spankbang.com/ab12/video").name == "spankbang"
    assert sites.get_adapter_for_url("https://missav.ai/en/abc-123").name == "missav"
    assert sites.get_adapter_for_url("https://jable.tv/videos/abc-123/").name == "jable"
    assert sites.get_adapter_for_url("https://91porn.com/view_video.php?viewkey=x").name == "91porn"
    assert sites.get_adapter_for_url("https://hanime.tv/videos/hentai/foo").name == "hanime"
    assert sites.get_adapter_for_url("https://beeg.com/1234567").name == "beeg"
    assert sites.get_adapter_for_url("https://www.drtuber.com/video/1/x").name == "drtuber"
    assert sites.get_adapter_for_url("https://www.redtube.com/12345").name == "redtube"
    assert sites.get_adapter_for_url("https://www.youporn.com/watch/1/x").name == "youporn"
    assert sites.get_adapter_for_url("https://www.tube8.com/x/1/").name == "tube8"
    assert sites.get_adapter_for_url("https://www.alphaporno.com/videos/1/x").name == "alphaporno"
    assert sites.get_adapter_for_url("https://www.empflix.com/videos/1/x").name == "empflix"
    assert sites.get_adapter_for_url(
        "https://www.eroprofile.com/m/videos/view/abc"
    ).name == "eroprofile"
    assert sites.get_adapter_for_url(
        "https://hypnotube.com/video/some-slug-12345.html"
    ).name == "hypnotube"
    assert sites.get_adapter_for_url("https://unknown.example/v/1").name == "generic"


def test_eporner_path_paging():
    ad = EpornerAdapter()
    base = "https://www.eporner.com/country-top/tw/"
    assert ad.get_start_page(base) == 1
    page2 = ad.build_page_url(base, 2)
    assert "/2/" in page2
    assert ad.build_page_url(page2, 1).rstrip("/").endswith("tw")


def test_pornhub_page_query():
    ad = PornhubAdapter()
    u = "https://www.pornhub.com/video/search?search=test"
    assert ad.build_page_url(u, 2).endswith("page=2") or "page=2" in ad.build_page_url(u, 2)
    assert ad.is_single_video_url(
        "https://www.pornhub.com/view_video.php?viewkey=ph123abc"
    )
    assert not ad.is_single_video_url(
        "https://www.pornhub.com/video/search?search=x&o=mv"
    )


def test_pornhub_list_from_html():
    ad = PornhubAdapter()
    html = '''
    <ul id="videoSearchResult">
      <a href="/view_video.php?viewkey=aaa111">a</a>
      <a href="/view_video.php?viewkey=bbb222">b</a>
      <a href="/view_video.php?viewkey=aaa111">dup</a>
    </ul>
    '''
    with patch.object(ad, "fetch_html", return_value=html):
        urls = ad.extract_list_urls_from_html("https://www.pornhub.com/video/search?search=x")
    assert urls == [
        "https://www.pornhub.com/view_video.php?viewkey=aaa111",
        "https://www.pornhub.com/view_video.php?viewkey=bbb222",
    ]


def test_xvideos_zero_based_page():
    ad = XVideosAdapter()
    u = "https://www.xvideos.com/?k=test"
    assert "p=1" in ad.build_page_url(u, 2)


def test_resolve_playable_prefers_extract_info_stream():
    class Fake(SiteAdapter):
        name = "fake"
        domains = ("fake.test",)

        def extract_info(self, video_url, purpose):
            return {
                "title": "T",
                "duration": 12,
                "url": "https://cdn.example/a.mp4",
                "http_headers": {"Referer": "https://fake.test/"},
            }

    result = resolve_playable(Fake(), "https://fake.test/v/1", purpose="info")
    assert result["source"] == "extract_info"
    assert result["stream_url"] == "https://cdn.example/a.mp4"
    assert result["info"]["title"] == "T"


def test_resolve_playable_uses_resolve_stream_when_no_info_url():
    class Fake(SiteAdapter):
        name = "fake"
        domains = ("fake.test",)

        def resolve_stream(self, video_url, prefer_lowest=False):
            return {
                "url": "https://cdn.example/stream.m3u8",
                "http_headers": {"Referer": "https://fake.test/"},
                "info": {"title": "S"},
            }

    result = resolve_playable(Fake(), "https://fake.test/v/1", purpose="info")
    assert result["source"] == "resolve_stream"
    assert "stream.m3u8" in result["stream_url"]


def test_resolve_playable_falls_back_to_ytdlp():
    class Fake(SiteAdapter):
        name = "fake"
        domains = ("fake.test",)

    mock_info = {
        "title": "Y",
        "duration": 30,
        "url": "https://cdn.example/y.mp4",
        "http_headers": {},
    }
    with patch("sites.resolve.yt_dlp.YoutubeDL") as ydl_cls:
        ydl = MagicMock()
        ydl.extract_info.return_value = mock_info
        ydl_cls.return_value.__enter__.return_value = ydl
        result = resolve_playable(Fake(), "https://fake.test/v/1", purpose="info")
    assert result["source"] == "yt_dlp"
    assert result["info"]["title"] == "Y"


def test_keyword_uses_eporner_search():
    ad = sites.get_adapter_by_name("eporner")
    assert ad is not None
    with patch.object(
        ad,
        "extract_list_urls",
        return_value=["https://www.eporner.com/video-x/"],
    ):
        urls = sites.extract_urls_from_target("worship", pages=1)
    assert urls == ["https://www.eporner.com/video-x/"]
    assert "tag/" in ad.search_url("worship")

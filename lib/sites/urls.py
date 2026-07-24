"""High-level URL list extraction used by capture_frames."""

from __future__ import annotations

import os
import re
import urllib.parse

from sites.eporner import DEFAULT_URL as EPORNER_DEFAULT_URL
from sites.registry import default_adapter, get_adapter_for_url


def extract_urls_from_target(target: str | None, pages: int = 1) -> list[str]:
    """Resolve keyword / list URL / video URL / text file into video URLs."""
    if not target:
        target = EPORNER_DEFAULT_URL

    # Local file of URLs
    if os.path.isfile(target):
        with open(target, "r", encoding="utf-8") as f:
            return [
                line.strip()
                for line in f
                if line.strip() and not line.startswith("#")
            ]

    # Keyword → default site search (Eporner)
    if not (target.startswith("http://") or target.startswith("https://")):
        keyword = target.strip()
        adapter = default_adapter()
        target = adapter.search_url(keyword)
        print(f"[*] 檢測到關鍵字 [{keyword}]，改用 {adapter.name} 搜尋: {target}")

    # Pornhub search default sort params (legacy behavior)
    if ("video/search" in target or "search=" in target) and (
        "o=" not in target or "t=" not in target
    ):
        adapter = get_adapter_for_url(target)
        if adapter.name == "pornhub":
            delimiter = "&" if "?" in target else "?"
            if "o=" not in target:
                target += f"{delimiter}o=mv"
                delimiter = "&"
            if "t=" not in target:
                target += f"{delimiter}t=a"
            print(f"[*] 搜尋網址自動補充預設排序與時間篩選參數: {target}")

    adapter = get_adapter_for_url(target)

    if adapter.is_single_video_url(target):
        if adapter.name == "pornhub" and hasattr(adapter, "normalize_video_url"):
            normalized = adapter.normalize_video_url(target)
            return [normalized or target]
        return [target]

    all_urls: list[str] = []
    seen: set[str] = set()
    total_pages = max(1, int(pages))
    start_page = adapter.get_start_page(target)
    end_page = start_page + total_pages - 1

    for idx, p in enumerate(range(start_page, end_page + 1), 1):
        page_target = adapter.build_page_url(target, p)
        if total_pages > 1 or start_page > 1:
            print(
                f"\n[+] 正在連續處理第 [{idx}/{total_pages}] 頁 "
                f"(網頁 page={p}): {page_target}"
            )
        page_urls = adapter.extract_list_urls(page_target)
        for u in page_urls:
            if u not in seen:
                seen.add(u)
                all_urls.append(u)

    if total_pages > 1 or start_page > 1:
        print(
            f"\n[+] 跨頁連續抓取完成！(處理頁碼 page={start_page}~{end_page}) "
            f"共計精確獲得 {len(all_urls)} 部影片！"
        )
    return all_urls


def folder_tag_for_target(target: str | None) -> str:
    """Short tag used in preview output folder names."""
    from sites.eporner import DEFAULT_URL

    if not target or target.strip().rstrip("/") == DEFAULT_URL.rstrip("/"):
        return "eporner"
    if os.path.isfile(target):
        return "file_links"
    if "search=" in target or "query=" in target:
        m = re.search(r"(?:search|query)=([^&]+)", target)
        if m:
            raw_kw = urllib.parse.unquote(m.group(1))
            return "search_" + re.sub(r"[^\w\u4e00-\u9fa5]", "_", raw_kw)
    if not target.startswith("http://") and not target.startswith("https://"):
        return "search_" + re.sub(r"[^\w\u4e00-\u9fa5]", "_", target.strip())
    adapter = get_adapter_for_url(target)
    if adapter.is_single_video_url(target):
        return "video"
    if adapter.name != "generic":
        return adapter.name
    return "list"

"""Web UI 使用的搜尋、媒體庫與字幕服務。"""

from __future__ import annotations

import base64
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import sites
from project_paths import (
    LIB_DIR,
    PREVIEW_VIDEOS_DIR,
    PROJECT_ROOT,
    VIDEOS_DIR,
    ensure_output_directories,
)
from sites.scrape_meta import scrape_page_info


SITE_LABELS = {
    "eporner": ("Eporner", 1),
    "pornhub": ("Pornhub", 1),
    "xvideos": ("XVideos", 1),
    "xhamster": ("xHamster", 1),
    "xnxx": ("XNXX", 1),
    "spankbang": ("SpankBang", 1),
    "beeg": ("Beeg", 2),
    "drtuber": ("DrTuber", 2),
    "redtube": ("RedTube", 2),
    "youporn": ("YouPorn", 2),
    "tube8": ("Tube8", 2),
    "alphaporno": ("AlphaPorno", 2),
    "empflix": ("EMPFlix", 2),
    "eroprofile": ("EroProfile", 2),
    "missav": ("MissAV", 3),
    "jable": ("Jable.tv", 3),
    "91porn": ("91porn", 3),
    "hanime": ("hanime.tv", 3),
    "hypnotube": ("HypnoTube", 3),
}

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}


def encode_media_id(path: Path) -> str:
    raw = str(path.resolve()).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_media_id(media_id: str, allowed_roots: tuple[Path, ...]) -> Path:
    padding = "=" * (-len(media_id) % 4)
    try:
        path = Path(
            base64.urlsafe_b64decode(media_id + padding).decode("utf-8")
        ).resolve()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("無效的媒體識別碼") from exc
    for root in allowed_roots:
        resolved_root = root.resolve()
        try:
            path.relative_to(resolved_root)
            return path
        except ValueError:
            continue
    raise ValueError("媒體路徑不在允許範圍內")


def srt_to_vtt(text: str) -> str:
    """把播放器相容 SRT 轉成瀏覽器可顯示的 WebVTT。"""
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(
        r"(?m)^(\d{1,3}:\d{2}:\d{2}),(\d{3})\s+-->\s+"
        r"(\d{1,3}:\d{2}:\d{2}),(\d{3})(.*)$",
        r"\1.\2 --> \3.\4\5",
        normalized,
    )
    return "WEBVTT\n\n" + normalized.strip() + "\n"


def _safe_title_from_url(url: str) -> str:
    path = urlsplit(url).path.rstrip("/").split("/")[-1]
    path = re.sub(r"[-_]+", " ", path)
    return path[:100] or urlsplit(url).hostname or "Untitled"


def _result_from_info(
    site_name: str,
    url: str,
    info: dict[str, Any],
) -> dict[str, Any]:
    label, tier = SITE_LABELS.get(site_name, (site_name.title(), 3))
    return {
        "id": f"{site_name}:{uuid.uuid5(uuid.NAMESPACE_URL, url).hex[:12]}",
        "site": site_name,
        "siteLabel": label,
        "tier": tier,
        "url": url,
        "title": info.get("title") or _safe_title_from_url(url),
        "description": info.get("description") or "",
        "thumbnail": info.get("thumbnail") or "",
        "duration": info.get("duration"),
        "views": info.get("view_count"),
        "date": info.get("upload_date"),
        "uploader": info.get("uploader") or "",
        "tags": list(info.get("tags") or [])[:10],
    }


def _search_one_site(
    site_name: str,
    keyword: str,
    pages: int,
    per_site: int,
) -> tuple[list[dict[str, Any]], str | None]:
    adapter = sites.get_adapter_by_name(site_name)
    if adapter is None:
        return [], f"{site_name}: unsupported"
    try:
        search_url = adapter.search_url(keyword)
    except (NotImplementedError, ValueError):
        return [], f"{site_name}: search unavailable"

    urls: list[str] = []
    seen: set[str] = set()
    start_page = adapter.get_start_page(search_url)
    try:
        for page_number in range(start_page, start_page + pages):
            page_url = adapter.build_page_url(search_url, page_number)
            for url in adapter.extract_list_urls(page_url):
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
                if len(urls) >= per_site:
                    break
            if len(urls) >= per_site:
                break
    except Exception as exc:
        return [], f"{site_name}: {type(exc).__name__}"

    if not urls:
        return [], f"{site_name}: no results"

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(6, len(urls))) as executor:
        future_map = {
            executor.submit(scrape_page_info, url, 12): url for url in urls
        }
        for future in as_completed(future_map):
            url = future_map[future]
            try:
                info = future.result()
            except Exception:
                info = {}
            results.append(_result_from_info(site_name, url, info))

    order = {url: index for index, url in enumerate(urls)}
    results.sort(key=lambda item: order.get(item["url"], 9999))
    return results, None


def search_sites(
    keyword: str,
    site_names: list[str],
    *,
    pages: int = 1,
    limit: int = 48,
) -> dict[str, Any]:
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("請輸入搜尋關鍵字")
    pages = max(1, min(int(pages), 3))
    limit = max(1, min(int(limit), 96))
    selected = [name for name in site_names if sites.get_adapter_by_name(name)]
    if not selected:
        selected = ["eporner"]
    per_site = max(4, min(18, (limit + len(selected) - 1) // len(selected)))

    results: list[dict[str, Any]] = []
    warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=min(4, len(selected))) as executor:
        futures = {
            executor.submit(
                _search_one_site,
                name,
                keyword,
                pages,
                per_site,
            ): name
            for name in selected
        }
        for future in as_completed(futures):
            found, warning = future.result()
            results.extend(found)
            if warning:
                warnings.append(warning)

    results.sort(
        key=lambda item: (
            selected.index(item["site"]),
            item["title"].casefold(),
        )
    )
    return {
        "query": keyword,
        "sites": selected,
        "results": results[:limit],
        "warnings": warnings,
        "count": min(len(results), limit),
    }


def _probe_duration(path: Path) -> float | None:
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return round(float(proc.stdout.strip()), 2) if proc.returncode == 0 else None
    except (FileNotFoundError, ValueError, subprocess.SubprocessError):
        return None


def scan_library() -> list[dict[str, Any]]:
    ensure_output_directories()
    roots = (
        ("preview", Path(PREVIEW_VIDEOS_DIR)),
        ("library", Path(VIDEOS_DIR)),
    )
    output: list[dict[str, Any]] = []
    for kind, root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.casefold() not in VIDEO_EXTENSIONS:
                continue
            media_id = encode_media_id(path)
            subtitle = path.with_suffix(".srt")
            stat = path.stat()
            output.append(
                {
                    "id": media_id,
                    "kind": kind,
                    "title": path.stem,
                    "filename": path.name,
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime),
                    "duration": _probe_duration(path),
                    "hasSubtitle": subtitle.exists(),
                    "mediaUrl": f"/media/{media_id}",
                    "subtitleUrl": (
                        f"/subtitles/{media_id}.vtt" if subtitle.exists() else ""
                    ),
                }
            )
    output.sort(key=lambda item: item["modified"], reverse=True)
    return output


def list_sites() -> list[dict[str, Any]]:
    output = []
    for adapter in sites.all_adapters():
        label, tier = SITE_LABELS.get(adapter.name, (adapter.name.title(), 3))
        output.append(
            {
                "id": adapter.name,
                "label": label,
                "tier": tier,
                "domains": list(adapter.domains),
                "recommended": tier == 1,
            }
        )
    return output


def resolve_remote(url: str) -> dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        raise ValueError("影片網址格式錯誤")
    adapter = sites.get_adapter_for_url(url)
    resolved = sites.resolve_playable(
        adapter,
        url,
        purpose="info",
        prefer_lowest=True,
    )
    info = dict(resolved.get("info") or {})
    result = _result_from_info(adapter.name, url, info)
    result["streamUrl"] = resolved.get("stream_url") or ""
    result["streamHeadersRequired"] = bool(resolved.get("http_headers"))
    return result


@dataclass
class TaskRecord:
    id: str
    kind: str
    label: str
    state: str = "queued"
    progress: int = 0
    message: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)

    def json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "state": self.state,
            "progress": self.progress,
            "message": self.message,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "log": self.log[-20:],
        }


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            ordered = sorted(
                self._tasks.values(),
                key=lambda task: task.created_at,
                reverse=True,
            )
            return [task.json() for task in ordered[:30]]

    def _update(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            task = self._tasks[task_id]
            for key, value in changes.items():
                setattr(task, key, value)
            task.updated_at = time.time()

    def start_capture(
        self,
        items: list[dict[str, Any]],
        quality: str,
    ) -> dict[str, Any]:
        urls = [str(item.get("url") or "") for item in items]
        urls = [url for url in urls if url.startswith(("http://", "https://"))]
        if not urls:
            raise ValueError("沒有可加入的影片網址")
        if quality not in {"preview", "full"}:
            raise ValueError("未知的下載品質")
        task_id = uuid.uuid4().hex[:12]
        label = "建立動態預覽" if quality == "preview" else "準備完整下載"
        record = TaskRecord(id=task_id, kind="capture", label=label)
        with self._lock:
            self._tasks[task_id] = record
        thread = threading.Thread(
            target=self._run_capture,
            args=(task_id, urls, quality),
            daemon=True,
        )
        thread.start()
        return record.json()

    def _run_capture(
        self,
        task_id: str,
        urls: list[str],
        quality: str,
    ) -> None:
        target_dir = (
            Path(PREVIEW_VIDEOS_DIR) if quality == "preview" else Path(VIDEOS_DIR)
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        list_path = PROJECT_ROOT / "tasks" / "ui-queues" / f"{task_id}.txt"
        list_path.parent.mkdir(parents=True, exist_ok=True)
        list_path.write_text("\n".join(urls), encoding="utf-8")
        capture_root = PROJECT_ROOT / "tasks" / "ui-captures" / task_id
        capture_root.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(Path(LIB_DIR) / "capture_frames.py"),
            str(list_path),
            "--quality",
            "480p" if quality == "preview" else "720p",
            "--output",
            str(capture_root),
            "--max-videos",
            str(len(urls)),
        ]
        self._update(task_id, state="running", progress=8, message="正在建立九宮格")
        try:
            proc = subprocess.run(
                command,
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            log = (proc.stdout + "\n" + proc.stderr).splitlines()
            if proc.returncode:
                self._update(
                    task_id,
                    state="failed",
                    progress=100,
                    message=f"九宮格建立失敗（代碼 {proc.returncode}）",
                    log=log,
                )
                return
            moved = 0
            for source in capture_root.rglob("*.jpg"):
                destination = target_dir / source.name
                if destination.exists():
                    stem, suffix = destination.stem, destination.suffix
                    destination = target_dir / f"{stem}-{task_id[:6]}{suffix}"
                shutil.move(str(source), str(destination))
                moved += 1
            if moved == 0:
                self._update(
                    task_id,
                    state="failed",
                    progress=100,
                    message="沒有產生可下載的九宮格",
                    log=log,
                )
                return
            self._update(
                task_id,
                state="ready",
                progress=100,
                message=f"{moved} 個九宮格已準備完成，可開始下載",
                log=log,
            )
        except Exception as exc:
            self._update(
                task_id,
                state="failed",
                progress=100,
                message=str(exc),
            )

    def start_download(self) -> dict[str, Any]:
        task_id = uuid.uuid4().hex[:12]
        record = TaskRecord(
            id=task_id,
            kind="download",
            label="下載與字幕處理",
        )
        with self._lock:
            if any(
                task.kind == "download" and task.state in {"queued", "running"}
                for task in self._tasks.values()
            ):
                raise ValueError("已有下載工作正在執行")
            self._tasks[task_id] = record
        threading.Thread(
            target=self._run_download,
            args=(task_id,),
            daemon=True,
        ).start()
        return record.json()

    def _run_download(self, task_id: str) -> None:
        self._update(
            task_id,
            state="running",
            progress=10,
            message="下載與字幕管線執行中",
        )
        command = [sys.executable, str(Path(LIB_DIR) / "run_download.py")]
        try:
            proc = subprocess.run(
                command,
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            log = (proc.stdout + "\n" + proc.stderr).splitlines()
            if proc.returncode:
                self._update(
                    task_id,
                    state="failed",
                    progress=100,
                    message=f"處理未完成（代碼 {proc.returncode}）",
                    log=log,
                )
            else:
                self._update(
                    task_id,
                    state="done",
                    progress=100,
                    message="下載與字幕已完成",
                    log=log,
                )
        except Exception as exc:
            self._update(
                task_id,
                state="failed",
                progress=100,
                message=str(exc),
            )


TASKS = TaskManager()

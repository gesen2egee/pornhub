"""Web UI 使用的搜尋、媒體庫與字幕服務。"""

from __future__ import annotations

import base64
import json
import os
import re
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
    PREVIEW_IMAGES_DIR,
    PREVIEW_VIDEOS_DIR,
    PROJECT_ROOT,
    VIDEOS_DIR,
    ensure_output_directories,
)
from sites.scrape_meta import scrape_page_info
from web_app.workspace import (
    CONFIG_DIR,
    get_profile,
    invalidate_catalog_cache,
)


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
    profile_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
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
            "profileId": self.profile_id,
            "payload": self.payload,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "log": self.log[-20:],
            "cancellable": self.state in {"queued", "running"},
        }


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()
        self._pipeline_lock = threading.Lock()
        self._capture_lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._tasks_file = Path(CONFIG_DIR) / "jobs.json"
        self._last_persist = 0.0
        self._load()

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            ordered = sorted(
                self._tasks.values(),
                key=lambda task: task.created_at,
                reverse=True,
            )
            return [task.json() for task in ordered[:100]]

    def _load(self) -> None:
        try:
            raw = json.loads(self._tasks_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, list):
            return
        for item in raw[-100:]:
            if not isinstance(item, dict):
                continue
            state = str(item.get("state") or "failed")
            if state in {"queued", "running"}:
                state = "interrupted"
            record = TaskRecord(
                id=str(item.get("id") or uuid.uuid4().hex[:12]),
                kind=str(item.get("kind") or "unknown"),
                label=str(item.get("label") or "工作"),
                state=state,
                progress=int(item.get("progress") or 0),
                message=(
                    "Muse 上次關閉時工作仍在執行"
                    if state == "interrupted"
                    else str(item.get("message") or "")
                ),
                profile_id=str(item.get("profileId") or ""),
                payload=dict(item.get("payload") or {}),
                created_at=float(item.get("createdAt") or time.time()),
                updated_at=float(item.get("updatedAt") or time.time()),
                log=list(item.get("log") or [])[-200:],
            )
            self._tasks[record.id] = record

    def _persist_locked(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_persist < 1.0:
            return
        self._tasks_file.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(self._tasks.values(), key=lambda task: task.created_at)
        payload = []
        for task in ordered[-100:]:
            item = task.json()
            item["log"] = task.log[-200:]
            payload.append(item)
        temporary = self._tasks_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self._tasks_file)
        self._last_persist = now

    def _update(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            task = self._tasks[task_id]
            for key, value in changes.items():
                setattr(task, key, value)
            task.updated_at = time.time()
            self._persist_locked(force=True)

    def _append_log(self, task_id: str, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        with self._lock:
            task = self._tasks[task_id]
            task.log.append(line)
            task.log = task.log[-400:]
            task.updated_at = time.time()
            lowered = line.casefold()
            progress = task.progress
            if "moss" in lowered or "asr" in lowered:
                progress = max(progress, 42)
            if "翻譯" in line or "translation" in lowered:
                progress = max(progress, 58)
            if "高畫質" in line or "download" in lowered:
                progress = max(progress, 70)
            if "enhance" in lowered or "增強" in line:
                progress = max(progress, 84)
            if "[done]" in lowered or "[success]" in lowered:
                progress = max(progress, 92)
            task.progress = progress
            task.message = line[-160:]
            self._persist_locked()

    def _register(self, record: TaskRecord) -> dict[str, Any]:
        with self._lock:
            self._tasks[record.id] = record
            self._persist_locked(force=True)
        return record.json()

    def start_grid_capture(
        self,
        target: str,
        *,
        pages: int = 1,
        max_videos: int = 20,
        quality: str = "480p",
    ) -> dict[str, Any]:
        target = target.strip()
        if not target:
            raise ValueError("請輸入關鍵字、影片網址或列表網址")
        pages = max(1, min(int(pages), 20))
        max_videos = max(0, min(int(max_videos), 500))
        if quality not in {"360p", "480p", "720p", "1080p", "best"}:
            raise ValueError("未知的擷取畫質")
        task_id = uuid.uuid4().hex[:12]
        record = TaskRecord(
            id=task_id,
            kind="capture",
            label="建立 5×5 宮格備份",
            payload={
                "target": target,
                "pages": pages,
                "maxVideos": max_videos,
                "quality": quality,
            },
            message="等待建立宮格",
        )
        self._register(record)
        threading.Thread(
            target=self._run_grid_capture,
            args=(task_id,),
            daemon=True,
        ).start()
        return record.json()

    def _run_grid_capture(self, task_id: str) -> None:
        with self._capture_lock:
            self._execute_grid_capture(task_id)

    def _execute_grid_capture(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            payload = dict(task.payload)
            if task.state == "cancelled":
                return
        command = [
            sys.executable,
            str(Path(LIB_DIR) / "capture_frames.py"),
            str(payload["target"]),
            "--pages",
            str(payload["pages"]),
            "--quality",
            str(payload["quality"]),
            "--output",
            str(Path(PREVIEW_IMAGES_DIR)),
            "--max-videos",
            str(payload["maxVideos"]),
        ]
        self._run_process(
            task_id,
            command,
            running_message="正在建立可長期保留的宮格備份",
            done_message="宮格已建立，可前往宮格庫挑選",
        )
        invalidate_catalog_cache()

    def start_capture(
        self,
        items: list[dict[str, Any]],
        quality: str,
    ) -> dict[str, Any]:
        """相容舊 UI：多個 URL 一律先建立到宮格庫，不直接送入下載資料夾。"""
        urls = [str(item.get("url") or "") for item in items]
        urls = [url for url in urls if url.startswith(("http://", "https://"))]
        if not urls:
            raise ValueError("沒有可加入的影片網址")
        task_id = uuid.uuid4().hex[:12]
        list_path = PROJECT_ROOT / "tasks" / "ui-queues" / f"{task_id}.txt"
        list_path.parent.mkdir(parents=True, exist_ok=True)
        list_path.write_text("\n".join(urls), encoding="utf-8")
        return self.start_grid_capture(
            str(list_path),
            pages=1,
            max_videos=len(urls),
            quality="480p" if quality == "preview" else "720p",
        )

    def start_profile(self, profile_id: str) -> dict[str, Any]:
        profile = get_profile(profile_id)
        if not profile.get("enabled", True):
            raise ValueError("此資料夾設定已停用")
        task_id = uuid.uuid4().hex[:12]
        record = TaskRecord(
            id=task_id,
            kind="pipeline",
            label=f"{profile['name']}｜下載與處理",
            profile_id=profile_id,
            payload={"profileId": profile_id},
            message="已加入處理佇列",
        )
        self._register(record)
        threading.Thread(
            target=self._run_profile,
            args=(task_id,),
            daemon=True,
        ).start()
        return record.json()

    def start_download(self) -> dict[str, Any]:
        """相容舊入口：執行標準 Video Profile。"""
        return self.start_profile("video")

    def _profile_command(self, profile: dict[str, Any]) -> list[str]:
        if profile.get("config_file"):
            return [
                sys.executable,
                str(Path(LIB_DIR) / "run_download.py"),
                "--configs",
                str(profile["id"]),
            ]
        mode = profile["mode"]
        options = dict(profile["options"])
        command = [
            sys.executable,
            str(Path(LIB_DIR) / "run_download.py"),
            "--stages",
            mode,
            "--source-dir",
            str(profile["inbox_dir"]),
            "--output-dir",
            str(profile["output_dir"]),
            "--archive-dir",
            str(profile["grid_backup_dir"]),
        ]
        boolean_options = {
            "asr": "asr",
            "demucs_asr": "demucs-asr",
            "asr_stream": "asr-stream",
            "subtitles": "subtitles",
            "translation": "translation",
            "dialogue_trim": "dialogue-trim",
            "selective_download": "selective-download",
            "three_phase_selection": "three-phase-selection",
            "edge_padding": "edge-padding",
            "enhance": "enhance",
            "metadata": "metadata",
            "archive": "archive",
            "keep_work": "keep-work",
            "reuse_cache": "reuse-cache",
            "force": "force",
        }
        for key, flag in boolean_options.items():
            command.append(f"--{flag}" if options.get(key) else f"--no-{flag}")
        common_values = {
            "vocal_separator": "vocal-separator",
            "asr_backend": "asr-backend",
            "translation_model": "translation-model",
            "reasoning_effort": "reasoning-effort",
            "trim_threshold": "trim-threshold",
            "segment_gap": "segment-gap",
            "asr_chunk_seconds": "asr-chunk-seconds",
            "asr_batch_size": "asr-batch-size",
        }
        for key, flag in common_values.items():
            command.extend([f"--{flag}", str(options[key])])
        if mode == "preview":
            command.extend(["--preview-seconds", str(options["preview_seconds"])])
        if mode == "video":
            command.extend(["--video-height", str(options["video_height"])])
            command.extend([
                "--video-high-quality-max-seconds",
                str(options["video_high_quality_max_seconds"]),
            ])
        if mode == "chosen":
            command.extend(["--chosen-height", str(options["chosen_height"])])
        return command

    def _run_profile(self, task_id: str) -> None:
        with self._pipeline_lock:
            try:
                with self._lock:
                    task = self._tasks[task_id]
                    if task.state == "cancelled":
                        return
                    profile_id = task.profile_id
                profile = get_profile(profile_id)
                for key in ("inbox_dir", "output_dir", "grid_backup_dir"):
                    Path(profile[key]).mkdir(parents=True, exist_ok=True)
                command = self._profile_command(profile)
                self._run_process(
                    task_id,
                    command,
                    running_message=f"{profile['name']} 正在下載與處理",
                    done_message=f"{profile['name']} 已完成",
                )
                invalidate_catalog_cache()
            except Exception as exc:
                self._update(
                    task_id,
                    state="failed",
                    progress=100,
                    message=str(exc),
                )

    def _run_process(
        self,
        task_id: str,
        command: list[str],
        *,
        running_message: str,
        done_message: str,
    ) -> None:
        self._update(
            task_id,
            state="running",
            progress=5,
            message=running_message,
        )
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            with self._lock:
                self._processes[task_id] = proc
            assert proc.stdout is not None
            for line in proc.stdout:
                self._append_log(task_id, line)
            return_code = proc.wait()
            with self._lock:
                self._processes.pop(task_id, None)
                state = self._tasks[task_id].state
            if state == "cancelled":
                self._update(
                    task_id,
                    progress=100,
                    message="工作已取消",
                )
            elif return_code:
                self._update(
                    task_id,
                    state="failed",
                    progress=100,
                    message=f"處理未完成（代碼 {return_code}）",
                )
            else:
                self._update(
                    task_id,
                    state="done",
                    progress=100,
                    message=done_message,
                )
        except Exception as exc:
            with self._lock:
                self._processes.pop(task_id, None)
            self._update(
                task_id,
                state="failed",
                progress=100,
                message=str(exc),
            )

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ValueError("找不到指定工作")
            if task.state not in {"queued", "running"}:
                raise ValueError("這個工作目前無法取消")
            task.state = "cancelled"
            task.message = "正在取消工作" if task_id in self._processes else "工作已取消"
            process = self._processes.get(task_id)
            self._persist_locked(force=True)
        if process is not None:
            process.terminate()
        return task.json()

    def retry(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ValueError("找不到指定工作")
            kind = task.kind
            payload = dict(task.payload)
            profile_id = task.profile_id
        if kind == "capture":
            return self.start_grid_capture(
                str(payload.get("target") or ""),
                pages=int(payload.get("pages") or 1),
                max_videos=int(payload.get("maxVideos") or 20),
                quality=str(payload.get("quality") or "480p"),
            )
        if kind == "pipeline" and profile_id:
            return self.start_profile(profile_id)
        raise ValueError("這個工作無法重試")


TASKS = TaskManager()

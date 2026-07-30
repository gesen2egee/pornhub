"""Muse 工作區、宮格索引、資料夾 Profile 與媒體檔案操作。"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

import video_meta
from pipeline_control import (
    FOLDER_CONFIG_SETTING_TO_OPTION,
    FeatureSwitches,
    FolderConfig,
    bootstrap_folder_configs,
    load_folder_configs,
    resolve_stage_options,
    validate_folder_paths,
    validate_stage_options,
)
from project_paths import (
    CHOSEN_DIR,
    DOWNLOADED_DIR,
    GOOD_DIR,
    OUTPUT_ROOT,
    PREVIEW_IMAGES_DIR,
    PREVIEW_VIDEOS_DIR,
    PROJECT_ROOT,
    SHORTS_DIR,
    TASKS_DIR,
    VIDEOS_DIR,
    ensure_output_directories,
)


CONFIG_DIR = Path(TASKS_DIR) / "muse"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
CATALOG_CACHE_FILE = CONFIG_DIR / "catalog-cache.json"
ROUTE_HISTORY_FILE = CONFIG_DIR / "route-history.json"
TRASH_HISTORY_FILE = CONFIG_DIR / "trash-history.json"
THUMBNAIL_CACHE_DIR = Path(OUTPUT_ROOT) / "00_temp" / "muse-thumbnails"

GRID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
SIDE_CAR_EXTENSIONS = {".srt", ".vtt", ".json", ".jpg", ".jpeg", ".png", ".webp"}
PROFILE_MODES = {"preview", "shorts", "video", "chosen"}
PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_CACHE_LOCK = threading.RLock()
_SETTINGS_LOCK = threading.RLock()
_THUMBNAIL_SEMAPHORE = threading.BoundedSemaphore(2)
_BACKGROUND_INDEXING: set[str] = set()
_BACKGROUND_INDEX_THRESHOLD = 120


def encode_path_id(path: str | Path) -> str:
    raw = str(Path(path).resolve()).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_path_id(path_id: str, allowed_roots: Iterable[str | Path]) -> Path:
    padding = "=" * (-len(path_id) % 4)
    try:
        path = Path(
            base64.urlsafe_b64decode(path_id + padding).decode("utf-8")
        ).resolve()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("無效的檔案識別碼") from exc
    for root in allowed_roots:
        resolved_root = Path(root).resolve()
        try:
            path.relative_to(resolved_root)
            return path
        except ValueError:
            continue
    raise ValueError("檔案路徑不在允許範圍內")


def _resolve_path(value: Any, fallback: str | Path | None = None) -> Path:
    raw = str(value or fallback or "").strip()
    if not raw:
        raise ValueError("資料夾路徑不可留空")
    expanded = os.path.expandvars(os.path.expanduser(raw))
    path = Path(expanded)
    if not path.is_absolute():
        path = Path(PROJECT_ROOT) / path
    return path.resolve()


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return fallback
    return value


def default_pipeline_options(mode: str) -> dict[str, Any]:
    resolved = resolve_stage_options(mode, FeatureSwitches())
    values = dict(vars(resolved))
    values["archive"] = values.pop("archive_grid")
    return values


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    return bool(default)


def _profile(
    profile_id: str,
    name: str,
    mode: str,
    inbox: str | Path,
    output: str | Path,
    archive: str | Path,
    *,
    system: bool = True,
    color: str = "#d6ff3f",
) -> dict[str, Any]:
    return {
        "id": profile_id,
        "name": name,
        "mode": mode,
        "enabled": True,
        "system": system,
        "color": color,
        "inbox_dir": str(Path(inbox).resolve()),
        "output_dir": str(Path(output).resolve()),
        "grid_backup_dir": str(Path(archive).resolve()),
        "route_mode": "copy",
        "auto_run": False,
        "options": default_pipeline_options(mode),
    }


def _profile_from_folder_config(config: FolderConfig) -> dict[str, Any]:
    profile = _profile(
        config.id,
        config.name,
        config.stage_name,
        config.source_dir,
        config.output_dir,
        config.archive_dir,
        system=config.id in {"preview", "shorts", "video", "chosen"},
        color=config.color,
    )
    options = resolve_stage_options(config.stage_name, config.options)
    profile.update(
        {
            "enabled": config.enabled,
            "description": config.description,
            "config_file": str(config.config_path),
            "route_mode": config.route_mode,
            "options": {
                **dict(vars(options)),
                "archive": options.archive_grid,
            },
        }
    )
    profile["options"].pop("archive_grid", None)
    return profile


def _default_profiles() -> list[dict[str, Any]]:
    bootstrap_folder_configs(OUTPUT_ROOT)
    configs = load_folder_configs(OUTPUT_ROOT)
    if configs:
        return [_profile_from_folder_config(config) for config in configs]
    # 相容尚未建立 JSON 的舊工作區；儲存後會遷移成獨立設定檔。
    return [
        _profile(
            "preview",
            "快速預覽",
            "preview",
            PREVIEW_VIDEOS_DIR,
            PREVIEW_VIDEOS_DIR,
            DOWNLOADED_DIR,
            color="#38d6c7",
        ),
        _profile(
            "shorts",
            "精準短片",
            "shorts",
            SHORTS_DIR,
            SHORTS_DIR,
            DOWNLOADED_DIR,
            color="#ffbb4d",
        ),
        _profile(
            "video",
            "標準影片",
            "video",
            VIDEOS_DIR,
            VIDEOS_DIR,
            DOWNLOADED_DIR,
            color="#7c8cff",
        ),
        _profile(
            "chosen",
            "高畫質精選",
            "chosen",
            CHOSEN_DIR,
            GOOD_DIR,
            DOWNLOADED_DIR,
            color="#ff6f91",
        ),
    ]


def default_settings() -> dict[str, Any]:
    ensure_output_directories()
    return {
        "version": 2,
        "output_root": str(Path(OUTPUT_ROOT).resolve()),
        "profiles": _default_profiles(),
        "favorite_folders": [
            {
                "id": "favorites",
                "name": "喜愛收藏",
                "path": str((Path(OUTPUT_ROOT) / "07_favorites").resolve()),
            }
        ],
        "trash_dir": str((Path(OUTPUT_ROOT) / "99_trash").resolve()),
        "capture": {
            "quality": "480p",
            "pages": 1,
            "max_videos": 20,
        },
        "privacy": {
            "blur_thumbnails": False,
            "remember_progress": True,
            "auto_subtitles": True,
        },
    }


def _merge_profile(raw: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    mode = str(raw.get("mode") or (base or {}).get("mode") or "video").casefold()
    if mode not in PROFILE_MODES:
        raise ValueError(f"未知的 Profile 模式：{mode}")
    profile_id = str(raw.get("id") or (base or {}).get("id") or "").casefold()
    if not PROFILE_ID_PATTERN.fullmatch(profile_id):
        profile_id = f"profile-{uuid.uuid4().hex[:10]}"
    defaults = base or _profile(
        profile_id,
        "自訂流程",
        mode,
        Path(OUTPUT_ROOT) / profile_id / "inbox",
        Path(OUTPUT_ROOT) / profile_id / "videos",
        DOWNLOADED_DIR,
        system=False,
    )
    options = default_pipeline_options(mode)
    options.update(dict(defaults.get("options") or {}))
    options.update(dict(raw.get("options") or {}))
    merged = {
        **defaults,
        **raw,
        "id": profile_id,
        "mode": mode,
        "name": str(raw.get("name") or defaults.get("name") or profile_id)[:80],
        "enabled": _coerce_bool(
            raw.get("enabled"),
            bool(defaults.get("enabled", True)),
        ),
        "system": _coerce_bool(
            defaults.get("system"),
            bool(raw.get("system", False)),
        ),
        "inbox_dir": str(_resolve_path(raw.get("inbox_dir"), defaults["inbox_dir"])),
        "output_dir": str(_resolve_path(raw.get("output_dir"), defaults["output_dir"])),
        "grid_backup_dir": str(
            _resolve_path(raw.get("grid_backup_dir"), defaults["grid_backup_dir"])
        ),
        "route_mode": (
            str(raw.get("route_mode") or defaults.get("route_mode") or "copy")
            if str(raw.get("route_mode") or defaults.get("route_mode") or "copy")
            in {"copy", "move"}
            else "copy"
        ),
        "auto_run": _coerce_bool(
            raw.get("auto_run"),
            bool(defaults.get("auto_run", False)),
        ),
        "options": _validate_options(options, mode),
    }
    return merged


def _validate_options(options: dict[str, Any], mode: str) -> dict[str, Any]:
    defaults = default_pipeline_options(mode)
    result: dict[str, Any] = {}
    boolean_fields = {
        "asr",
        "demucs_asr",
        "asr_stream",
        "subtitles",
        "translation",
        "dialogue_trim",
        "selective_download",
        "three_phase_selection",
        "edge_padding",
        "enhance",
        "metadata",
        "archive",
        "keep_work",
        "reuse_cache",
        "force",
    }
    positive_ints = {
        "preview_seconds",
        "video_height",
        "chosen_height",
        "asr_chunk_seconds",
        "asr_batch_size",
    }
    positive_floats = {"trim_threshold"}
    for name, default in defaults.items():
        value = options.get(name, default)
        if name in boolean_fields:
            if isinstance(value, bool):
                result[name] = value
            elif isinstance(value, str) and value.strip().casefold() in {
                "true",
                "1",
                "yes",
                "on",
            }:
                result[name] = True
            elif isinstance(value, str) and value.strip().casefold() in {
                "false",
                "0",
                "no",
                "off",
            }:
                result[name] = False
            else:
                result[name] = default
        elif name in positive_ints:
            try:
                result[name] = max(1, int(value))
            except (OverflowError, TypeError, ValueError):
                result[name] = default
        elif name in positive_floats:
            try:
                parsed = float(value)
                result[name] = (
                    max(0.1, parsed)
                    if math.isfinite(parsed)
                    else default
                )
            except (TypeError, ValueError):
                result[name] = default
        elif name == "segment_gap":
            try:
                parsed = float(value)
                result[name] = (
                    max(0.0, parsed)
                    if math.isfinite(parsed)
                    else default
                )
            except (TypeError, ValueError):
                result[name] = default
        else:
            result[name] = str(value or default)
    if result["asr_backend"] not in {"whisper", "moss", "voxtral", "grok-stt"}:
        result["asr_backend"] = "moss"
    if result["reasoning_effort"] not in {"none", "minimal", "low", "medium", "high"}:
        result["reasoning_effort"] = "minimal"
    if mode not in {"shorts", "video", "chosen"}:
        result["three_phase_selection"] = False
    return result


def get_settings() -> dict[str, Any]:
    with _SETTINGS_LOCK:
        defaults = default_settings()
        raw = _read_json(SETTINGS_FILE, {})
        if not isinstance(raw, dict):
            raw = {}
        config_defaults = {item["id"]: item for item in defaults["profiles"]}
        raw_profiles = {
            str(item.get("id") or "").casefold(): item
            for item in list(raw.get("profiles") or [])
            if isinstance(item, dict)
        }
        profiles: list[dict[str, Any]] = []
        seen: set[str] = set()
        for profile_id, profile in config_defaults.items():
            # Pipeline、路徑與開關以 output JSON 為唯一來源；tasks 只留 UI 偏好。
            ui = raw_profiles.pop(profile_id, {})
            merged = dict(profile)
            for key in ("auto_run",):
                if key in ui:
                    merged[key] = _coerce_bool(
                        ui[key],
                        bool(merged.get(key, False)),
                    )
            profiles.append(merged)
            seen.add(profile_id)
        # 相容舊版尚未遷移的自訂 Profile；下次儲存會建立 output JSON。
        for item in raw_profiles.values():
            if "mode" not in item and "inbox_dir" not in item:
                continue
            merged = _merge_profile(item)
            if merged["id"] not in seen:
                profiles.append(merged)
                seen.add(merged["id"])
        favorites = []
        for item in list(raw.get("favorite_folders") or defaults["favorite_folders"]):
            if not isinstance(item, dict):
                continue
            folder_id = str(item.get("id") or f"favorite-{uuid.uuid4().hex[:8]}")
            favorites.append(
                {
                    "id": folder_id,
                    "name": str(item.get("name") or "收藏資料夾")[:80],
                    "path": str(_resolve_path(item.get("path"), GOOD_DIR)),
                }
            )
        settings = {
            **defaults,
            **raw,
            "version": 2,
            "profiles": profiles,
            "favorite_folders": favorites or defaults["favorite_folders"],
            "trash_dir": str(_resolve_path(raw.get("trash_dir"), defaults["trash_dir"])),
        }
        settings["capture"] = {**defaults["capture"], **dict(raw.get("capture") or {})}
        settings["privacy"] = {**defaults["privacy"], **dict(raw.get("privacy") or {})}
        return settings


def _portable_output_path(value: str | Path) -> str:
    path = Path(value).resolve()
    try:
        return path.relative_to(Path(OUTPUT_ROOT).resolve()).as_posix()
    except ValueError:
        return str(path)


def _next_folder_config_path(
    existing: Iterable[Path],
    profile_id: str,
) -> Path:
    paths = list(existing)
    used = {
        int(path.name[:2])
        for path in paths
        if re.fullmatch(r"\d{2}_.+\.json", path.name, flags=re.IGNORECASE)
    }
    start = max(used, default=1) + 1
    for order in list(range(start, 100)) + list(range(2, start)):
        if order not in used:
            return Path(OUTPUT_ROOT) / f"{order:02d}_{profile_id}.json"
    raise ValueError("資料夾設定順序 02–99 已用完")


def _profile_feature_switches(profile: dict[str, Any]) -> FeatureSwitches:
    options = dict(profile["options"])
    values = {
        option_name: options[config_name]
        for config_name, option_name in FOLDER_CONFIG_SETTING_TO_OPTION.items()
        if config_name in options
    }
    return FeatureSwitches(**values)


def _folder_config_payload(
    profile: dict[str, Any],
    existing: FolderConfig | None = None,
) -> dict[str, Any]:
    options = dict(profile["options"])
    defaults = resolve_stage_options(profile["mode"], FeatureSwitches())
    settings: dict[str, Any] = {}
    for config_name, option_name in FOLDER_CONFIG_SETTING_TO_OPTION.items():
        if config_name not in options:
            continue
        original = (
            getattr(existing.options, option_name)
            if existing is not None and existing.stage_name == profile["mode"]
            else None
        )
        if (
            existing is not None
            and original is None
            and options[config_name] == getattr(defaults, option_name)
        ):
            continue
        settings[config_name] = options[config_name]
    paths = {
        "source_dir": _portable_output_path(profile["inbox_dir"]),
        "output_dir": _portable_output_path(profile["output_dir"]),
        "archive_dir": _portable_output_path(profile["grid_backup_dir"]),
    }
    if existing is not None:
        raw_existing = _read_json(existing.config_path, {})
        if isinstance(raw_existing, dict):
            for config_name, profile_name, attribute_name in (
                ("source_dir", "inbox_dir", "source_dir"),
                ("output_dir", "output_dir", "output_dir"),
                ("archive_dir", "grid_backup_dir", "archive_dir"),
            ):
                raw_path = raw_existing.get(config_name)
                if not isinstance(raw_path, str):
                    continue
                profile_path = Path(profile[profile_name]).resolve()
                existing_path = getattr(existing, attribute_name).resolve()
                if os.path.normcase(str(profile_path)) == os.path.normcase(
                    str(existing_path)
                ):
                    # 未在 Muse 改路徑時保留 ~、%ENV% 與原相對寫法。
                    paths[config_name] = raw_path
    return {
        "version": 1,
        "id": profile["id"],
        "name": profile["name"],
        "description": str(profile.get("description") or ""),
        "color": str(profile.get("color") or "#d6ff3f"),
        "enabled": bool(profile["enabled"]),
        "pipeline": profile["mode"],
        "route_mode": profile.get("route_mode", "copy"),
        **paths,
        "settings": settings,
    }


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("設定格式錯誤")
    with _SETTINGS_LOCK:
        current = get_settings()
        stored = _read_json(SETTINGS_FILE, {})
        if not isinstance(stored, dict):
            stored = {}
        merged = {**current, **payload}
        if "profiles" in payload:
            profiles = payload["profiles"]
            if not isinstance(profiles, list) or not profiles:
                raise ValueError("至少需要一個處理資料夾 Profile")
            if len(profiles) > 98:
                raise ValueError("處理資料夾 Profile 最多 98 個")
            system_defaults = {
                item["id"]: item for item in default_settings()["profiles"]
            }
            normalized_profiles = []
            seen = set()
            for raw in profiles:
                if not isinstance(raw, dict):
                    raise ValueError("Profile 必須是 JSON 物件")
                profile_id = str(raw.get("id") or "").casefold()
                normalized = _merge_profile(raw, system_defaults.get(profile_id))
                if normalized["id"] in seen:
                    raise ValueError(f"Profile ID 重複：{normalized['id']}")
                route_mode = normalized.get("route_mode", "copy")
                if route_mode not in {"copy", "move"}:
                    raise ValueError(
                        f"{normalized['name']}：route_mode 必須是 copy 或 move"
                    )
                color = str(normalized.get("color") or "")
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    raise ValueError(
                        f"{normalized['name']}：color 必須是 #RRGGBB 色碼"
                    )
                normalized["color"] = color.casefold()
                seen.add(normalized["id"])
                normalized_profiles.append(normalized)

            enabled_sources: dict[str, dict[str, Any]] = {}
            for profile in normalized_profiles:
                if not profile["enabled"]:
                    continue
                source_key = os.path.normcase(
                    str(Path(profile["inbox_dir"]).resolve())
                )
                previous = enabled_sources.get(source_key)
                if previous is not None:
                    raise ValueError(
                        "啟用的 Profile 不可共用 Inbox："
                        f"{previous['name']}、{profile['name']}"
                    )
                enabled_sources[source_key] = profile

            existing_configs = load_folder_configs(OUTPUT_ROOT)
            existing_by_id = {config.id: config for config in existing_configs}
            config_paths = [config.config_path for config in existing_configs]
            pending_config_writes: list[tuple[Path, dict[str, Any]]] = []
            for profile in normalized_profiles:
                options = resolve_stage_options(
                    profile["mode"],
                    _profile_feature_switches(profile),
                )
                try:
                    validate_stage_options(profile["mode"], options)
                except ValueError as exc:
                    raise ValueError(f"{profile['name']}：{exc}") from exc
                validate_folder_paths(
                    profile["name"],
                    profile["inbox_dir"],
                    profile["output_dir"],
                    profile["grid_backup_dir"],
                    options,
                )
                existing = existing_by_id.get(profile["id"])
                if existing is not None:
                    config_path = existing.config_path
                else:
                    config_path = _next_folder_config_path(
                        config_paths,
                        profile["id"],
                    )
                    config_paths.append(config_path)
                profile["config_file"] = str(config_path)
                config_payload = _folder_config_payload(
                    profile,
                    existing=existing,
                )
                if _read_json(config_path, None) != config_payload:
                    pending_config_writes.append((config_path, config_payload))

            for config_path, config_payload in pending_config_writes:
                _atomic_json_write(config_path, config_payload)
            for config in existing_configs:
                if (
                    config.id not in seen
                    and config.id not in {"preview", "shorts", "video", "chosen"}
                ):
                    config.config_path.unlink(missing_ok=True)
            # Pipeline 細節已寫入 output JSON；tasks 只保留 UI 偏好。
            merged["profiles"] = [
                {
                    "id": profile["id"],
                    "auto_run": bool(profile.get("auto_run", False)),
                }
                for profile in normalized_profiles
            ]
        else:
            # 隱私、收藏等局部儲存不可改寫或鎖住 Pipeline JSON。
            merged["profiles"] = list(stored.get("profiles") or [])
        favorites = payload.get("favorite_folders", current["favorite_folders"])
        if not isinstance(favorites, list) or len(favorites) > 24:
            raise ValueError("收藏資料夾設定錯誤")
        merged["favorite_folders"] = [
            {
                "id": str(item.get("id") or f"favorite-{uuid.uuid4().hex[:8]}"),
                "name": str(item.get("name") or "收藏資料夾")[:80],
                "path": str(_resolve_path(item.get("path"), GOOD_DIR)),
            }
            for item in favorites
            if isinstance(item, dict)
        ]
        merged["trash_dir"] = str(
            _resolve_path(payload.get("trash_dir"), current["trash_dir"])
        )
        merged["version"] = 2
        _atomic_json_write(SETTINGS_FILE, merged)
        return get_settings()


def get_profile(profile_id: str) -> dict[str, Any]:
    for profile in get_settings()["profiles"]:
        if profile["id"] == profile_id:
            return profile
    raise ValueError("找不到指定的處理資料夾 Profile")


def allowed_roots() -> tuple[Path, ...]:
    settings = get_settings()
    roots = {
        Path(OUTPUT_ROOT).resolve(),
        Path(PREVIEW_IMAGES_DIR).resolve(),
        Path(DOWNLOADED_DIR).resolve(),
    }
    for profile in settings["profiles"]:
        roots.update(
            {
                Path(profile["inbox_dir"]).resolve(),
                Path(profile["output_dir"]).resolve(),
                Path(profile["grid_backup_dir"]).resolve(),
            }
        )
    for favorite in settings["favorite_folders"]:
        roots.add(Path(favorite["path"]).resolve())
    roots.add(Path(settings["trash_dir"]).resolve())
    return tuple(sorted(roots, key=str))


def _load_catalog_cache() -> dict[str, Any]:
    value = _read_json(CATALOG_CACHE_FILE, {"grids": {}, "videos": {}})
    if not isinstance(value, dict):
        return {"grids": {}, "videos": {}}
    value.setdefault("grids", {})
    value.setdefault("videos", {})
    return value


def _cache_signature(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _walk_files(roots: Iterable[Path], extensions: set[str]) -> list[Path]:
    output: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        try:
            iterator = root.rglob("*")
            for path in iterator:
                if path.is_file() and path.suffix.casefold() in extensions:
                    output[str(path.resolve()).casefold()] = path.resolve()
        except OSError:
            continue
    return sorted(output.values(), key=lambda item: item.stat().st_mtime, reverse=True)


def _grid_roots() -> list[Path]:
    roots = [Path(PREVIEW_IMAGES_DIR), Path(DOWNLOADED_DIR)]
    roots.extend(Path(item["inbox_dir"]) for item in get_settings()["profiles"])
    return roots


def _grid_location(path: Path, settings: dict[str, Any]) -> tuple[str, str]:
    candidates = [
        ("grid-library", "宮格庫", Path(PREVIEW_IMAGES_DIR)),
        ("archive", "宮格備份", Path(DOWNLOADED_DIR)),
    ]
    candidates.extend(
        (profile["id"], profile["name"], Path(profile["inbox_dir"]))
        for profile in settings["profiles"]
    )
    for location_id, name, root in candidates:
        try:
            path.relative_to(root.resolve())
            return location_id, name
        except ValueError:
            continue
    return "external", "外部資料夾"


def _read_grid_record(path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    try:
        meta = video_meta.read_grid_jpg_meta(path)
    except Exception:
        meta = {}
    web = dict(meta.get("web_meta") or {})
    raw = str(meta.get("raw_user_comment") or "")
    _, sections = video_meta.parse_sections(raw)
    try:
        tagger = json.loads(sections.get("TAGGER_V1", "{}"))
    except (json.JSONDecodeError, TypeError):
        tagger = {}
    frames = list(tagger.get("frames") or [])
    counts: Counter[str] = Counter()
    rating_counts: Counter[str] = Counter()
    for frame in frames:
        tags = [
            item.strip()
            for item in str((frame or {}).get("tags") or "").split(",")
            if item.strip()
        ]
        if tags and tags[0] in {"general", "sensitive", "questionable", "explicit"}:
            rating_counts[tags[0]] += 1
        counts.update(tags)
    location_id, location_name = _grid_location(path, settings)
    path_id = encode_path_id(path)
    stat = path.stat()
    return {
        "id": path_id,
        "type": "grid",
        "title": str(web.get("title") or re.sub(r"^\d{4}-", "", path.stem)),
        "filename": path.name,
        "path": str(path),
        "folder": str(path.parent),
        "locationId": location_id,
        "locationName": location_name,
        "size": stat.st_size,
        "modified": int(stat.st_mtime),
        "url": meta.get("url") or web.get("webpage_url") or "",
        "source": web.get("extractor") or "",
        "duration": web.get("duration"),
        "views": web.get("view_count"),
        "date": web.get("upload_date"),
        "description": web.get("description") or "",
        "tags": [name for name, _ in counts.most_common(40)],
        "tagCounts": dict(counts.most_common(80)),
        "ratingCounts": dict(rating_counts),
        "frameCount": len(frames),
        "generalCount": tagger.get("general_count", 0),
        "smileCount": tagger.get("smile_count", 0),
        "assetUrl": f"/asset/{path_id}",
        "thumbnailUrl": f"/thumbnail/{path_id}",
    }


def _minimal_grid_record(path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    location_id, location_name = _grid_location(path, settings)
    path_id = encode_path_id(path)
    stat = path.stat()
    return {
        "id": path_id,
        "type": "grid",
        "title": re.sub(r"^\d{4}-", "", path.stem),
        "filename": path.name,
        "path": str(path),
        "folder": str(path.parent),
        "locationId": location_id,
        "locationName": location_name,
        "size": stat.st_size,
        "modified": int(stat.st_mtime),
        "url": "",
        "source": "",
        "duration": None,
        "views": None,
        "date": "",
        "description": "",
        "tags": [],
        "tagCounts": {},
        "ratingCounts": {},
        "frameCount": 25,
        "generalCount": 0,
        "smileCount": 0,
        "assetUrl": f"/asset/{path_id}",
        "thumbnailUrl": f"/thumbnail/{path_id}",
    }


def _background_refresh_records(
    cache_name: str,
    paths: list[Path],
    reader,
) -> None:
    try:
        with _CACHE_LOCK:
            cache = _load_catalog_cache()
            section = dict(cache.get(cache_name) or {})
        workers = min(8, max(1, len(paths)))

        def read_one(path: Path) -> tuple[str, dict[str, Any]] | None:
            try:
                record = reader(path)
                record["_signature"] = _cache_signature(path)
                return str(path), record
            except (OSError, ValueError):
                return None

        with ThreadPoolExecutor(max_workers=workers) as executor:
            for result in executor.map(read_one, paths):
                if result is not None:
                    key, record = result
                    section[key] = record
        live_paths = {str(path) for path in paths}
        section = {key: value for key, value in section.items() if key in live_paths}
        with _CACHE_LOCK:
            cache = _load_catalog_cache()
            cache[cache_name] = section
            _atomic_json_write(CATALOG_CACHE_FILE, cache)
    finally:
        with _CACHE_LOCK:
            _BACKGROUND_INDEXING.discard(cache_name)


def _refresh_records(
    cache_name: str,
    paths: list[Path],
    reader,
    fast_reader=None,
) -> tuple[list[dict[str, Any]], bool]:
    with _CACHE_LOCK:
        cache = _load_catalog_cache()
        section = dict(cache.get(cache_name) or {})
    fresh: dict[str, dict[str, Any]] = {}
    missing: list[Path] = []
    for path in paths:
        key = str(path)
        signature = _cache_signature(path)
        cached = section.get(key)
        if isinstance(cached, dict) and cached.get("_signature") == signature:
            fresh[key] = cached
        else:
            missing.append(path)
    indexing = False
    if missing and fast_reader is not None and len(missing) > _BACKGROUND_INDEX_THRESHOLD:
        indexing = True
        for path in missing:
            try:
                fresh[str(path)] = fast_reader(path)
            except OSError:
                continue
        with _CACHE_LOCK:
            if cache_name not in _BACKGROUND_INDEXING:
                _BACKGROUND_INDEXING.add(cache_name)
                threading.Thread(
                    target=_background_refresh_records,
                    args=(cache_name, paths, reader),
                    name=f"muse-{cache_name}-index",
                    daemon=True,
                ).start()
    elif missing:
        workers = min(8, max(1, len(missing)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for path, record in zip(missing, executor.map(reader, missing)):
                record["_signature"] = _cache_signature(path)
                fresh[str(path)] = record
    if not indexing:
        with _CACHE_LOCK:
            cache = _load_catalog_cache()
            cache[cache_name] = fresh
            _atomic_json_write(CATALOG_CACHE_FILE, cache)
    return [fresh[str(path)] for path in paths if str(path) in fresh], indexing


def _route_history() -> dict[str, Any]:
    value = _read_json(ROUTE_HISTORY_FILE, {})
    return value if isinstance(value, dict) else {}


def list_grids(
    *,
    query: str = "",
    location: str = "all",
    include_tags: Iterable[str] = (),
    exclude_tags: Iterable[str] = (),
    page: int = 1,
    page_size: int = 36,
) -> dict[str, Any]:
    settings = get_settings()
    paths = _walk_files(_grid_roots(), GRID_EXTENSIONS)
    records, indexing = _refresh_records(
        "grids",
        paths,
        lambda path: _read_grid_record(path, settings),
        lambda path: _minimal_grid_record(path, settings),
    )
    history = _route_history()
    normalized_query = query.strip().casefold()
    include = {item.strip().casefold() for item in include_tags if item.strip()}
    exclude = {item.strip().casefold() for item in exclude_tags if item.strip()}
    output = []
    for record in records:
        tags = {str(tag).casefold() for tag in record.get("tags") or []}
        haystack = " ".join(
            [
                str(record.get("title") or ""),
                str(record.get("description") or ""),
                str(record.get("source") or ""),
                " ".join(tags),
            ]
        ).casefold()
        if normalized_query and normalized_query not in haystack:
            continue
        if location != "all" and record.get("locationId") != location:
            continue
        if include and not include.issubset(tags):
            continue
        if exclude and tags.intersection(exclude):
            continue
        record = {key: value for key, value in record.items() if not key.startswith("_")}
        record["routes"] = list(history.get(record["path"]) or [])
        output.append(record)
    page = max(1, int(page))
    page_size = max(12, min(int(page_size), 96))
    start = (page - 1) * page_size
    return {
        "items": output[start : start + page_size],
        "count": len(output),
        "page": page,
        "pageSize": page_size,
        "hasMore": start + page_size < len(output),
        "locations": grid_locations(settings),
        "indexing": indexing,
    }


def grid_locations(settings: dict[str, Any] | None = None) -> list[dict[str, str]]:
    settings = settings or get_settings()
    output = [
        {"id": "all", "name": "全部宮格"},
        {"id": "grid-library", "name": "宮格庫"},
        {"id": "archive", "name": "宮格備份"},
    ]
    output.extend(
        {"id": profile["id"], "name": f"{profile['name']} Inbox"}
        for profile in settings["profiles"]
    )
    return output


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
            timeout=12,
        )
        return round(float(proc.stdout.strip()), 2) if proc.returncode == 0 else None
    except (FileNotFoundError, ValueError, subprocess.SubprocessError):
        return None


def _video_roots() -> list[Path]:
    roots = [
        Path(PREVIEW_VIDEOS_DIR),
        Path(SHORTS_DIR),
        Path(VIDEOS_DIR),
        Path(CHOSEN_DIR),
        Path(GOOD_DIR),
    ]
    settings = get_settings()
    roots.extend(Path(item["output_dir"]) for item in settings["profiles"])
    roots.extend(Path(item["path"]) for item in settings["favorite_folders"])
    return roots


def _video_location(path: Path, settings: dict[str, Any]) -> tuple[str, str]:
    candidates = []
    candidates.extend(
        (profile["id"], profile["name"], Path(profile["output_dir"]))
        for profile in settings["profiles"]
    )
    candidates.extend(
        (favorite["id"], favorite["name"], Path(favorite["path"]))
        for favorite in settings["favorite_folders"]
    )
    for location_id, name, root in candidates:
        try:
            path.relative_to(root.resolve())
            return location_id, name
        except ValueError:
            continue
    return "library", "影片庫"


def _read_video_record(path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    try:
        meta = video_meta.read_mp4_meta(path)
    except Exception:
        meta = {}
    web = dict(meta.get("web_meta") or {})
    location_id, location_name = _video_location(path, settings)
    path_id = encode_path_id(path)
    stat = path.stat()
    subtitle = path.with_suffix(".srt")
    duration = web.get("duration")
    if duration in {None, ""}:
        duration = _probe_duration(path)
    return {
        "id": path_id,
        "type": "video",
        "title": str(web.get("title") or meta.get("title") or path.stem),
        "filename": path.name,
        "path": str(path),
        "folder": str(path.parent),
        "locationId": location_id,
        "locationName": location_name,
        "size": stat.st_size,
        "modified": int(stat.st_mtime),
        "duration": duration,
        "url": web.get("webpage_url") or "",
        "source": web.get("extractor") or "",
        "description": web.get("description") or "",
        "tags": list(web.get("tags") or [])[:40],
        "publishedStage": web.get("published_stage") or web.get("pipeline_stage") or "",
        "hasSubtitle": subtitle.is_file(),
        "mediaUrl": f"/media/{path_id}",
        "subtitleUrl": f"/subtitles/{path_id}.vtt" if subtitle.is_file() else "",
        "thumbnailUrl": f"/thumbnail/{path_id}",
    }


def _minimal_video_record(path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    location_id, location_name = _video_location(path, settings)
    path_id = encode_path_id(path)
    stat = path.stat()
    subtitle = path.with_suffix(".srt")
    return {
        "id": path_id,
        "type": "video",
        "title": path.stem,
        "filename": path.name,
        "path": str(path),
        "folder": str(path.parent),
        "locationId": location_id,
        "locationName": location_name,
        "size": stat.st_size,
        "modified": int(stat.st_mtime),
        "duration": None,
        "url": "",
        "source": "",
        "description": "",
        "tags": [],
        "publishedStage": "",
        "hasSubtitle": subtitle.is_file(),
        "mediaUrl": f"/media/{path_id}",
        "subtitleUrl": f"/subtitles/{path_id}.vtt" if subtitle.is_file() else "",
        "thumbnailUrl": f"/thumbnail/{path_id}",
    }


def list_videos(
    *,
    query: str = "",
    location: str = "all",
    page: int = 1,
    page_size: int = 36,
) -> dict[str, Any]:
    settings = get_settings()
    paths = _walk_files(_video_roots(), VIDEO_EXTENSIONS)
    records, indexing = _refresh_records(
        "videos",
        paths,
        lambda path: _read_video_record(path, settings),
        lambda path: _minimal_video_record(path, settings),
    )
    normalized_query = query.strip().casefold()
    output = []
    for record in records:
        haystack = " ".join(
            [
                str(record.get("title") or ""),
                str(record.get("description") or ""),
                str(record.get("source") or ""),
                " ".join(record.get("tags") or []),
            ]
        ).casefold()
        if normalized_query and normalized_query not in haystack:
            continue
        if location != "all" and record.get("locationId") != location:
            continue
        output.append(
            {key: value for key, value in record.items() if not key.startswith("_")}
        )
    page = max(1, int(page))
    page_size = max(12, min(int(page_size), 96))
    start = (page - 1) * page_size
    locations = [{"id": "all", "name": "全部影片"}]
    locations.extend(
        {"id": profile["id"], "name": profile["name"]}
        for profile in settings["profiles"]
    )
    locations.extend(
        {"id": favorite["id"], "name": favorite["name"]}
        for favorite in settings["favorite_folders"]
    )
    return {
        "items": output[start : start + page_size],
        "count": len(output),
        "page": page,
        "pageSize": page_size,
        "hasMore": start + page_size < len(output),
        "locations": locations,
        "indexing": indexing,
    }


def profile_inventory() -> list[dict[str, Any]]:
    from pipeline_control import list_sources_in_directory

    settings = get_settings()
    output = []
    for profile in settings["profiles"]:
        inbox = Path(profile["inbox_dir"])
        output_dir = Path(profile["output_dir"])
        pending = list_sources_in_directory(
            profile["mode"],
            inbox,
            output_dir=output_dir,
            include_published=bool(profile["options"].get("force")),
        )
        videos = _walk_files([output_dir], VIDEO_EXTENSIONS)
        output.append(
            {
                **profile,
                "pendingCount": len(pending),
                "videoCount": len(videos),
                "inboxExists": inbox.is_dir(),
                "outputExists": output_dir.is_dir(),
            }
        )
    return output


def _unique_destination(directory: Path, name: str) -> Path:
    destination = directory / name
    if not destination.exists():
        return destination
    stem = Path(name).stem
    suffix = Path(name).suffix
    for index in range(2, 10000):
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"


def route_grids(
    item_ids: Iterable[str],
    profile_id: str,
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    profile = get_profile(profile_id)
    route_mode = mode or profile.get("route_mode") or "copy"
    if route_mode not in {"copy", "move"}:
        raise ValueError("路由方式必須是 copy 或 move")
    destination_dir = Path(profile["inbox_dir"]).resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    history = _route_history()
    routed = []
    errors = []
    for item_id in list(item_ids)[:500]:
        try:
            source = decode_path_id(str(item_id), allowed_roots())
            if source.suffix.casefold() not in GRID_EXTENSIONS or not source.is_file():
                raise ValueError("只能路由宮格圖片")
            destination = _unique_destination(destination_dir, source.name)
            if route_mode == "move":
                shutil.move(str(source), str(destination))
            else:
                shutil.copy2(source, destination)
            event = {
                "profileId": profile_id,
                "profileName": profile["name"],
                "mode": route_mode,
                "destination": str(destination),
                "createdAt": time.time(),
            }
            history.setdefault(str(source), []).append(event)
            routed.append(event)
        except Exception as exc:
            errors.append(str(exc))
    _atomic_json_write(ROUTE_HISTORY_FILE, history)
    return {"routed": routed, "errors": errors, "count": len(routed)}


def _favorite_target(target_id: str) -> Path:
    settings = get_settings()
    for item in settings["favorite_folders"]:
        if item["id"] == target_id:
            return Path(item["path"]).resolve()
    for profile in settings["profiles"]:
        if profile["id"] == target_id:
            return Path(profile["output_dir"]).resolve()
    raise ValueError("找不到指定的收藏資料夾")


def _related_media_files(path: Path) -> list[Path]:
    files = [path]
    for suffix in SIDE_CAR_EXTENSIONS:
        candidate = path.with_suffix(suffix)
        if candidate.is_file() and candidate != path:
            files.append(candidate)
    return files


def move_media(item_id: str, target_id: str) -> dict[str, Any]:
    source = decode_path_id(item_id, allowed_roots())
    if source.suffix.casefold() not in VIDEO_EXTENSIONS or not source.is_file():
        raise ValueError("指定項目不是可移動的影片")
    destination_dir = _favorite_target(target_id)
    destination_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    primary_destination = _unique_destination(destination_dir, source.name)
    target_stem = primary_destination.stem
    for related in _related_media_files(source):
        destination = (
            primary_destination
            if related == source
            else destination_dir / f"{target_stem}{related.suffix}"
        )
        if destination.exists():
            destination = _unique_destination(destination_dir, destination.name)
        shutil.move(str(related), str(destination))
        moved.append({"from": str(related), "to": str(destination)})
    invalidate_catalog_cache()
    return {
        "moved": moved,
        "mediaId": encode_path_id(primary_destination),
        "destination": str(primary_destination),
    }


def trash_media(item_id: str) -> dict[str, Any]:
    source = decode_path_id(item_id, allowed_roots())
    if source.suffix.casefold() not in VIDEO_EXTENSIONS or not source.is_file():
        raise ValueError("指定項目不是可刪除的影片")
    settings = get_settings()
    token = uuid.uuid4().hex
    trash_dir = Path(settings["trash_dir"]) / time.strftime("%Y-%m-%d") / token
    trash_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for related in _related_media_files(source):
        destination = trash_dir / related.name
        shutil.move(str(related), str(destination))
        moved.append({"from": str(related), "to": str(destination)})
    history = _read_json(TRASH_HISTORY_FILE, {})
    if not isinstance(history, dict):
        history = {}
    history[token] = {
        "createdAt": time.time(),
        "items": moved,
        "restored": False,
    }
    _atomic_json_write(TRASH_HISTORY_FILE, history)
    invalidate_catalog_cache()
    return {"token": token, "moved": moved, "recoverable": True}


def restore_trashed_media(token: str) -> dict[str, Any]:
    history = _read_json(TRASH_HISTORY_FILE, {})
    record = history.get(token) if isinstance(history, dict) else None
    if not isinstance(record, dict) or record.get("restored"):
        raise ValueError("找不到可復原的刪除紀錄")
    restored = []
    for item in record.get("items") or []:
        source = Path(str(item.get("to") or ""))
        destination = Path(str(item.get("from") or ""))
        if not source.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination = _unique_destination(destination.parent, destination.name)
        shutil.move(str(source), str(destination))
        restored.append({"from": str(source), "to": str(destination)})
    record["restored"] = True
    record["restoredAt"] = time.time()
    history[token] = record
    _atomic_json_write(TRASH_HISTORY_FILE, history)
    invalidate_catalog_cache()
    return {"restored": restored, "count": len(restored)}


def invalidate_catalog_cache() -> None:
    with _CACHE_LOCK:
        try:
            CATALOG_CACHE_FILE.unlink()
        except FileNotFoundError:
            pass


def thumbnail_path(path: Path) -> Path:
    path = path.resolve()
    stat = path.stat()
    key = hashlib.sha1(
        f"{path}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()
    destination = THUMBNAIL_CACHE_DIR / f"{key}.jpg"
    if destination.is_file():
        return destination
    THUMBNAIL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with _THUMBNAIL_SEMAPHORE:
        if destination.is_file():
            return destination
        temporary = destination.with_suffix(".tmp.jpg")
        if path.suffix.casefold() in GRID_EXTENSIONS:
            with Image.open(path) as source:
                image = source.convert("RGB")
                image.thumbnail((960, 960))
                image.save(temporary, "JPEG", quality=82, optimize=True)
        elif path.suffix.casefold() in VIDEO_EXTENSIONS:
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "5",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=640:-2",
                "-q:v",
                "4",
                "-y",
                str(temporary),
            ]
            proc = subprocess.run(command, capture_output=True, timeout=30)
            if proc.returncode or not temporary.is_file():
                command[5] = "0"
                proc = subprocess.run(command, capture_output=True, timeout=30)
            if proc.returncode or not temporary.is_file():
                raise FileNotFoundError("無法建立影片縮圖")
        else:
            raise ValueError("不支援的縮圖格式")
        os.replace(temporary, destination)
    return destination


def dashboard_summary() -> dict[str, Any]:
    settings = get_settings()
    profiles = profile_inventory()
    return {
        "outputRoot": settings["output_root"],
        "gridLibrary": len(_walk_files([Path(PREVIEW_IMAGES_DIR)], GRID_EXTENSIONS)),
        "gridArchive": len(_walk_files([Path(DOWNLOADED_DIR)], GRID_EXTENSIONS)),
        "profiles": [
            {
                "id": item["id"],
                "name": item["name"],
                "mode": item["mode"],
                "pendingCount": item["pendingCount"],
                "videoCount": item["videoCount"],
                "color": item.get("color"),
            }
            for item in profiles
        ],
        "favoriteFolders": settings["favorite_folders"],
    }

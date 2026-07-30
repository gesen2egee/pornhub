"""設定檔驅動的下載流程盤點、預算設定與執行控制。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, fields, replace
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

from project_paths import (
    CHOSEN_DIR,
    DOWNLOADED_DIR,
    GOOD_DIR,
    OUTPUT_ROOT,
    PREVIEW_VIDEOS_DIR,
    SHORTS_DIR,
    VIDEOS_DIR,
    PROJECT_ROOT,
    ensure_output_directories,
)

STAGE_ORDER = ("preview", "shorts", "video", "chosen")
GRID_EXTENSIONS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
DEFAULT_SOURCE_PIPELINE_WORKERS = 2
_PIPELINE_PROCESS_THREAD_LOCK = Lock()
FOLDER_CONFIG_VERSION = 1
FOLDER_CONFIG_PATTERN = re.compile(
    r"^(?P<order>\d{2})_(?P<name>.+)\.json$",
    re.IGNORECASE,
)
FOLDER_CONFIG_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
LEGACY_WORK_BUCKETS = {
    ("preview", "preview"): "02_preview_videos",
    ("shorts", "shorts"): "02_shorts",
    ("video", "video"): "03_videos",
    ("chosen", "chosen"): "05_chosen",
}


def source_pipeline_workers() -> int:
    """同層可重疊的影片管線數；GPU worker 仍由各自鎖序列化。"""
    try:
        value = int(os.getenv("PIPELINE_SOURCE_WORKERS", str(DEFAULT_SOURCE_PIPELINE_WORKERS)))
    except ValueError:
        value = DEFAULT_SOURCE_PIPELINE_WORKERS
    return max(1, min(value, 4))


@contextmanager
def pipeline_process_lock(
    lock_root: str | Path = OUTPUT_ROOT,
):
    """避免 BAT、Muse 或多個 Muse 程序同時處理同一個 output。"""
    lock_path = Path(lock_root).resolve() / "00_temp" / ".pipeline.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _PIPELINE_PROCESS_THREAD_LOCK, lock_path.open("a+b") as handle:
        waiting = False
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if not waiting:
                    print("[WAIT] 另一個下載流程正在執行，等待取得 Pipeline 鎖…")
                    waiting = True
                time.sleep(0.2)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class StageDefinition:
    key: str
    title: str
    directory: Path
    budget: str
    description: str
    accepted_extensions: frozenset[str]


@dataclass(frozen=True)
class FeatureSwitches:
    """None 表示使用該層預設值，True/False 表示明確覆寫。"""

    asr: bool | None = None
    demucs_asr: bool | None = None
    asr_stream: bool | None = None
    subtitles: bool | None = None
    translation: bool | None = None
    dialogue_trim: bool | None = None
    selective_download: bool | None = None
    three_phase_selection: bool | None = None
    edge_padding: bool | None = None  # 可選的對白前後 0.75s 延伸
    enhance: bool | None = None
    metadata: bool | None = None
    archive_grid: bool | None = None
    keep_work: bool | None = None
    reuse_cache: bool | None = None
    force: bool | None = None
    preview_seconds: int | None = None
    video_height: int | None = None
    chosen_height: int | None = None
    asr_backend: str | None = None
    translation_model: str | None = None
    reasoning_effort: str | None = None
    trim_threshold: float | None = None
    segment_gap: float | None = None
    asr_chunk_seconds: int | None = None
    asr_batch_size: int | None = None


@dataclass(frozen=True)
class FolderConfig:
    """一個位於 output 根目錄、可依檔名排序的資料夾流程設定。"""

    id: str
    name: str
    stage_name: str
    config_path: Path
    enabled: bool
    source_dir: Path
    output_dir: Path
    archive_dir: Path
    options: FeatureSwitches
    description: str = ""
    route_mode: str = "copy"
    color: str = "#d6ff3f"

    @property
    def filename(self) -> str:
        return self.config_path.name

    @property
    def work_bucket(self) -> str:
        """內建流程沿用舊快取；自訂設定以 ID／路徑隔離，避免單獨執行互撞。"""
        legacy_bucket = LEGACY_WORK_BUCKETS.get((self.id, self.stage_name))
        try:
            relative = self.source_dir.relative_to(self.config_path.parent)
            if (
                legacy_bucket is not None
                and len(relative.parts) == 1
                and relative.name.casefold() == legacy_bucket.casefold()
            ):
                return legacy_bucket
        except ValueError:
            pass
        digest = hashlib.sha1(
            str(self.source_dir).casefold().encode("utf-8")
        ).hexdigest()[:10]
        return f"{self.id}-{digest}"


FOLDER_CONFIG_TOP_LEVEL_KEYS = {
    "version",
    "id",
    "name",
    "description",
    "color",
    "enabled",
    "pipeline",
    "route_mode",
    "source_dir",
    "output_dir",
    "archive_dir",
    "settings",
}
FOLDER_CONFIG_SETTING_TO_OPTION = {
    "asr": "asr",
    "demucs_asr": "demucs_asr",
    "asr_stream": "asr_stream",
    "subtitles": "subtitles",
    "translation": "translation",
    "dialogue_trim": "dialogue_trim",
    "selective_download": "selective_download",
    "three_phase_selection": "three_phase_selection",
    "edge_padding": "edge_padding",
    "enhance": "enhance",
    "metadata": "metadata",
    "archive": "archive_grid",
    "keep_work": "keep_work",
    "reuse_cache": "reuse_cache",
    "force": "force",
    "preview_seconds": "preview_seconds",
    "video_height": "video_height",
    "chosen_height": "chosen_height",
    "asr_backend": "asr_backend",
    "translation_model": "translation_model",
    "reasoning_effort": "reasoning_effort",
    "trim_threshold": "trim_threshold",
    "segment_gap": "segment_gap",
    "asr_chunk_seconds": "asr_chunk_seconds",
    "asr_batch_size": "asr_batch_size",
}
BOOLEAN_CONFIG_SETTINGS = {
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
POSITIVE_INTEGER_CONFIG_SETTINGS = {
    "preview_seconds",
    "video_height",
    "chosen_height",
    "asr_chunk_seconds",
    "asr_batch_size",
}


STAGES = {
    "preview": StageDefinition(
        "preview",
        "第一層：Preview Video",
        PREVIEW_VIDEOS_DIR,
        "$ 低預算",
        "一次下載 BS×3 分鐘低畫質、批次 MOSS、一次精選翻譯、保留對白>30s 剪片",
        frozenset(GRID_EXTENSIONS | VIDEO_EXTENSIONS),
    ),
    "video": StageDefinition(
        "video",
        "第三層：Video",
        VIDEOS_DIR,
        "$$ 中預算",
        "240P/MOSS 串流→GLM 5.2 minimal 精選、Grok 4.3 minimal 一次翻譯（失敗改 Grok 4.5）30秒三段→關鍵格安全高畫質切塊、音訊判斷 enhance＋crossfade",
        frozenset(GRID_EXTENSIONS | VIDEO_EXTENSIONS),
    ),
    "shorts": StageDefinition(
        "shorts",
        "第二層：Shorts",
        SHORTS_DIR,
        "$$ 精準預算",
        "前 9 分鐘 240P/MOSS → GLM 5.2 minimal 精選、Grok 4.3 minimal 一次翻譯；純語音不足 30 秒保留前 9 分鐘",
        frozenset(GRID_EXTENSIONS | VIDEO_EXTENSIONS),
    ),
    "chosen": StageDefinition(
        "chosen",
        "第四層：Chosen",
        CHOSEN_DIR,
        "$$$ 高預算",
        "240P/MOSS 串流→GLM 5.2 minimal 精選、Grok 4.3 minimal 一次翻譯（失敗改 Grok 4.5）30秒三段→關鍵格安全 1080P 切塊、音訊判斷 enhance＋crossfade",
        frozenset(GRID_EXTENSIONS | VIDEO_EXTENSIONS),
    ),
}


def _config_error(path: Path, message: str) -> ValueError:
    return ValueError(f"{path.name}：{message}")


def _resolve_folder_path(value: Any, output_root: Path, path: Path, key: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise _config_error(path, f"{key} 必須是非空白路徑字串")
    expanded = os.path.expandvars(os.path.expanduser(value.strip()))
    resolved = Path(expanded)
    if not resolved.is_absolute():
        resolved = output_root / resolved
    return resolved.resolve()


def _parse_folder_settings(raw: Any, path: Path) -> FeatureSwitches:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _config_error(path, "settings 必須是 JSON 物件")
    unknown = sorted(set(raw) - set(FOLDER_CONFIG_SETTING_TO_OPTION))
    if unknown:
        raise _config_error(path, f"未知 settings 欄位：{', '.join(unknown)}")

    values: dict[str, Any] = {}
    for config_name, option_name in FOLDER_CONFIG_SETTING_TO_OPTION.items():
        if config_name not in raw or raw[config_name] is None:
            values[option_name] = None
            continue
        value = raw[config_name]
        if config_name in BOOLEAN_CONFIG_SETTINGS:
            if not isinstance(value, bool):
                raise _config_error(path, f"settings.{config_name} 必須是 true 或 false")
        elif config_name in POSITIVE_INTEGER_CONFIG_SETTINGS:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise _config_error(path, f"settings.{config_name} 必須是大於 0 的整數")
        elif config_name == "trim_threshold":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
                or not math.isfinite(value)
            ):
                raise _config_error(path, "settings.trim_threshold 必須是大於 0 的數字")
            value = float(value)
        elif config_name == "segment_gap":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
                or not math.isfinite(value)
            ):
                raise _config_error(path, "settings.segment_gap 必須是大於或等於 0 的數字")
            value = float(value)
        elif config_name == "asr_backend":
            if not isinstance(value, str) or value not in {
                "whisper",
                "moss",
                "voxtral",
                "grok-stt",
            }:
                raise _config_error(
                    path,
                    "settings.asr_backend 必須是 whisper、moss、voxtral 或 grok-stt",
                )
        elif config_name == "reasoning_effort":
            if not isinstance(value, str) or value not in {
                "none",
                "minimal",
                "low",
                "medium",
                "high",
            }:
                raise _config_error(
                    path,
                    "settings.reasoning_effort 必須是 none、minimal、low、medium 或 high",
                )
        elif not isinstance(value, str) or not value.strip():
            raise _config_error(path, f"settings.{config_name} 必須是非空白字串")
        if isinstance(value, str):
            value = value.strip()
        values[option_name] = value
    return FeatureSwitches(**values)


def _folder_config_sort_key(path: Path) -> tuple[int, str]:
    match = FOLDER_CONFIG_PATTERN.fullmatch(path.name)
    if match is None:
        return (2**31 - 1, path.name.casefold())
    return (int(match.group("order")), path.name.casefold())


def load_folder_configs(
    output_root: str | Path = OUTPUT_ROOT,
) -> list[FolderConfig]:
    """載入 output/NN_*.json；數字前綴及完整檔名共同決定執行順序。"""
    root = Path(output_root).resolve()
    if not root.is_dir():
        return []
    paths = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_file()
            and (match := FOLDER_CONFIG_PATTERN.fullmatch(path.name))
            and int(match.group("order")) >= 2
        ),
        key=_folder_config_sort_key,
    )
    configs: list[FolderConfig] = []
    seen_ids: dict[str, Path] = {}
    seen_orders: dict[int, Path] = {}
    for path in paths:
        match = FOLDER_CONFIG_PATTERN.fullmatch(path.name)
        assert match is not None
        order = int(match.group("order"))
        if order in seen_orders:
            raise _config_error(
                path,
                f"順序 {order} 與 {seen_orders[order].name} 重複，請修改檔名前綴",
            )
        seen_orders[order] = path
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise _config_error(
                path,
                f"JSON 格式錯誤（第 {exc.lineno} 行、第 {exc.colno} 欄）",
            ) from exc
        except OSError as exc:
            raise _config_error(path, f"無法讀取：{exc}") from exc
        if not isinstance(raw, dict):
            raise _config_error(path, "最外層必須是 JSON 物件")
        unknown = sorted(set(raw) - FOLDER_CONFIG_TOP_LEVEL_KEYS)
        if unknown:
            raise _config_error(path, f"未知欄位：{', '.join(unknown)}")
        version = raw.get("version")
        if type(version) is not int or version != FOLDER_CONFIG_VERSION:
            raise _config_error(path, f"version 必須是 {FOLDER_CONFIG_VERSION}")
        config_id = raw.get("id")
        if (
            not isinstance(config_id, str)
            or not FOLDER_CONFIG_ID_PATTERN.fullmatch(config_id)
        ):
            raise _config_error(
                path,
                "id 必須是 1–80 字元的小寫英數字、連字號或底線，且以英數字開頭",
            )
        config_id = config_id.casefold()
        if config_id in seen_ids:
            raise _config_error(
                path,
                f"id 與 {seen_ids[config_id].name} 重複：{config_id}",
            )
        seen_ids[config_id] = path
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise _config_error(path, "enabled 必須是 true 或 false")
        stage_name = raw.get("pipeline")
        if not isinstance(stage_name, str) or stage_name.casefold() not in STAGES:
            raise _config_error(
                path,
                f"pipeline 必須是：{', '.join(STAGE_ORDER)}",
            )
        stage_name = stage_name.casefold()
        name = raw.get("name", config_id)
        if not isinstance(name, str) or not name.strip():
            raise _config_error(path, "name 必須是非空白字串")
        description = raw.get("description", "")
        if not isinstance(description, str):
            raise _config_error(path, "description 必須是字串")
        route_mode = raw.get("route_mode", "copy")
        if not isinstance(route_mode, str) or route_mode not in {"copy", "move"}:
            raise _config_error(path, "route_mode 必須是 copy 或 move")
        color = raw.get("color", "#d6ff3f")
        if not isinstance(color, str) or not re.fullmatch(
            r"#[0-9a-fA-F]{6}",
            color,
        ):
            raise _config_error(path, "color 必須是 #RRGGBB 色碼")
        configs.append(
            FolderConfig(
                id=config_id,
                name=name.strip(),
                stage_name=stage_name,
                config_path=path.resolve(),
                enabled=enabled,
                source_dir=_resolve_folder_path(
                    raw.get("source_dir"), root, path, "source_dir"
                ),
                output_dir=_resolve_folder_path(
                    raw.get("output_dir"), root, path, "output_dir"
                ),
                archive_dir=_resolve_folder_path(
                    raw.get("archive_dir"), root, path, "archive_dir"
                ),
                options=_parse_folder_settings(raw.get("settings"), path),
                description=description.strip(),
                route_mode=route_mode,
                color=color.casefold(),
            )
        )
    return configs


def bootstrap_folder_configs(
    output_root: str | Path = OUTPUT_ROOT,
) -> list[Path]:
    """自訂 PORN_OUTPUT_DIR 首次使用時複製預設 JSON，絕不覆寫既有設定。"""
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(
        path.is_file()
        and (match := FOLDER_CONFIG_PATTERN.fullmatch(path.name))
        and int(match.group("order")) >= 2
        for path in root.iterdir()
    ):
        return []
    templates = (Path(PROJECT_ROOT) / "output").resolve()
    if root == templates or not templates.is_dir():
        return []
    copied: list[Path] = []
    for source in sorted(templates.iterdir(), key=_folder_config_sort_key):
        match = FOLDER_CONFIG_PATTERN.fullmatch(source.name)
        if not source.is_file() or match is None or int(match.group("order")) < 2:
            continue
        destination = root / source.name
        if destination.exists():
            continue
        shutil.copy2(source, destination)
        copied.append(destination)
    return copied


def select_folder_configs(
    configs: Iterable[FolderConfig],
    values: Iterable[str] | None,
) -> list[FolderConfig]:
    """依 id 或 JSON 檔名選設定，同時保留檔名所定義的原始順序。"""
    available = list(configs)
    if not values:
        return available
    requested: list[str] = []
    for raw in values:
        for value in raw.split(","):
            key = value.strip().casefold()
            if key and key not in requested:
                requested.append(key)
    aliases: dict[str, FolderConfig] = {}
    for config in available:
        for alias in (
            config.id.casefold(),
            config.config_path.stem.casefold(),
            config.filename.casefold(),
        ):
            existing = aliases.get(alias)
            if existing is not None and existing.id != config.id:
                raise ValueError(
                    f"資料夾設定別名衝突：{alias}（{existing.filename}、{config.filename}）"
                )
            aliases[alias] = config
    missing = [value for value in requested if value not in aliases]
    if missing:
        raise ValueError(f"找不到資料夾設定：{', '.join(missing)}")
    selected = {aliases[value].id for value in requested}
    return [config for config in available if config.id in selected]


def merge_feature_switches(
    base: FeatureSwitches,
    overrides: FeatureSwitches,
) -> FeatureSwitches:
    """CLI 非空值優先於 JSON；其餘保留 JSON 設定。"""
    values = {}
    for field in fields(FeatureSwitches):
        override = getattr(overrides, field.name)
        values[field.name] = (
            override if override is not None else getattr(base, field.name)
        )
    return FeatureSwitches(**values)


def parse_stage_names(values: Iterable[str] | None) -> list[str]:
    if not values:
        return list(STAGE_ORDER)
    result: list[str] = []
    for raw in values:
        for value in raw.split(","):
            key = value.strip().casefold()
            if not key:
                continue
            if key not in STAGES:
                raise ValueError(f"未知層級：{value}")
            if key not in result:
                result.append(key)
    return result


def _published_in_same_stage(path: Path, stage_name: str) -> bool:
    """只排除已在目前層級發布的影片；移到另一層仍可當作 URL 輸入。"""
    if path.suffix.casefold() not in VIDEO_EXTENSIONS:
        return False
    try:
        import video_meta

        web_meta = video_meta.read_mp4_meta(path).get("web_meta") or {}
        published_stage = web_meta.get("published_stage") or web_meta.get(
            "pipeline_stage"
        )
        return published_stage == stage_name
    except Exception:
        return False


def expected_output_stem(path: Path, stage_name: str) -> str:
    """回傳各 Pipeline 實際發布使用的檔名 stem。"""
    if stage_name == "preview":
        return path.stem
    return re.sub(r"^\d{4}-", "", path.stem)


def _is_usable_published_video(path: Path) -> bool:
    """只把 ffprobe 未明確判壞的非空影片視為既有成品。"""
    try:
        if path.stat().st_size <= 0:
            return False
    except OSError:
        return False
    try:
        import full_video_pipeline

        return full_video_pipeline.has_video_stream(path) is not False
    except Exception:
        # 無法使用 ffprobe 時保守保留既有檔案，避免誤覆寫。
        return True


def _source_has_published_output(
    path: Path,
    stage_name: str,
    output_dir: str | Path | None = None,
) -> bool:
    """來源在實際成品目錄已有同層成品時，掃描階段直接略過。"""
    if path.suffix.casefold() not in GRID_EXTENSIONS | VIDEO_EXTENSIONS:
        return False
    stage = STAGES[stage_name]
    resolved_output = Path(
        output_dir
        if output_dir is not None
        else (GOOD_DIR if stage_name == "chosen" else stage.directory)
    )
    stems = [path.stem, expected_output_stem(path, stage_name)]
    stems = list(dict.fromkeys(stems))
    candidates = [resolved_output / f"{stem}.mp4" for stem in stems]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            is_source_itself = candidate.resolve() == path.resolve()
        except OSError:
            is_source_itself = candidate == path
        # 分離 Inbox/Output 或 grid→MP4 可用成品存在性判斷；來源即成品時需 Metadata。
        if is_source_itself:
            if _published_in_same_stage(candidate, stage_name):
                return True
        elif _is_usable_published_video(candidate):
            return True
    return False


def validate_source_collisions(
    sources: Iterable[Path],
    stage_name: str,
    output_dir: str | Path,
) -> list[Path]:
    """阻止不同來源寫入同一成品／快取；force 時排除同資料夾的舊成品。"""
    resolved_output = Path(output_dir).resolve()
    groups: dict[str, list[Path]] = {}
    for path in sources:
        key = expected_output_stem(path, stage_name).casefold()
        groups.setdefault(key, []).append(path)
    result: list[Path] = []
    for paths in groups.values():
        actionable = [
            path
            for path in paths
            if not (
                path.parent.resolve() == resolved_output
                and _published_in_same_stage(path, stage_name)
            )
        ]
        if not actionable:
            actionable = paths[:1]
        if len(actionable) > 1:
            names = "、".join(path.name for path in actionable)
            target = expected_output_stem(actionable[0], stage_name) + ".mp4"
            raise ValueError(f"來源檔名碰撞，皆會寫入 {target}：{names}")
        result.append(actionable[0])
    return sorted(result, key=lambda item: item.name.casefold())


def list_stage_sources(
    stage_name: str,
    *,
    include_published: bool = False,
) -> list[Path]:
    return list_sources_in_directory(
        stage_name,
        STAGES[stage_name].directory,
        output_dir=GOOD_DIR if stage_name == "chosen" else STAGES[stage_name].directory,
        include_published=include_published,
    )


def list_sources_in_directory(
    stage_name: str,
    directory: str | Path,
    *,
    output_dir: str | Path | None = None,
    include_published: bool = False,
) -> list[Path]:
    """依指定 Profile Inbox 盤點來源，沿用各 Stage 的成品排除規則。"""
    stage = STAGES[stage_name]
    root = Path(directory).resolve()
    if not root.is_dir():
        return []
    return [
        path
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold())
        if path.is_file()
        and path.suffix.casefold() in stage.accepted_extensions
        and (
            include_published
            or not _source_has_published_output(
                path,
                stage_name,
                output_dir=output_dir,
            )
        )
    ]


def list_stage_files(stage_name: str) -> list[Path]:
    stage = STAGES[stage_name]
    if not stage.directory.is_dir():
        return []
    return [
        path
        for path in sorted(stage.directory.iterdir(), key=lambda item: item.name.casefold())
        if path.is_file()
    ]


def _format_size(path: Path) -> str:
    size = path.stat().st_size
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} GB"
    if size >= 1024**2:
        return f"{size / 1024**2:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def print_stage_inventory(stage_name: str) -> int:
    stage = STAGES[stage_name]
    files = list_stage_files(stage_name)
    sources = set(list_stage_sources(stage_name))
    print("\n" + "=" * 68)
    print(f"{stage.title}｜{stage.budget}")
    print(f"{stage.description}")
    print(f"資料夾：{stage.directory}")
    print("-" * 68)
    if not files:
        print("（沒有檔案）")
    else:
        for path in files:
            marker = "待處理" if path in sources else "既有成品"
            print(f"[{marker:^8}] {_format_size(path):>10}  {path.name}")
    print(f"待處理來源：{len(sources)}；資料夾檔案：{len(files)}")
    return len(sources)


def list_folder_config_sources(
    config: FolderConfig,
    *,
    include_published: bool = False,
) -> list[Path]:
    return list_sources_in_directory(
        config.stage_name,
        config.source_dir,
        output_dir=config.output_dir,
        include_published=include_published,
    )


def list_folder_config_files(config: FolderConfig) -> list[Path]:
    if not config.source_dir.is_dir():
        return []
    return [
        path
        for path in sorted(
            config.source_dir.iterdir(),
            key=lambda item: item.name.casefold(),
        )
        if path.is_file()
    ]


def print_folder_config_inventory(
    config: FolderConfig,
    overrides: FeatureSwitches | None = None,
) -> int:
    options = resolve_stage_options(
        config.stage_name,
        merge_feature_switches(config.options, overrides or FeatureSwitches()),
    )
    files = list_folder_config_files(config)
    if config.enabled:
        validate_stage_options(config.stage_name, options)
        sources = set(
            list_folder_config_sources(
                config,
                include_published=bool(options.force),
            )
        )
    else:
        sources = set()
    stage = STAGES[config.stage_name]
    print("\n" + "=" * 68)
    state = "啟用" if config.enabled else "停用"
    print(f"{config.name}｜{stage.budget}｜{state}")
    print(config.description or stage.description)
    print(f"設定檔：{config.config_path}")
    print(f"來源：{config.source_dir}")
    print(f"成品：{config.output_dir}")
    print(f"歸檔：{config.archive_dir}")
    print("-" * 68)
    if not files:
        print("（沒有檔案）")
    else:
        for path in files:
            marker = (
                "已停用"
                if not config.enabled
                else ("待處理" if path in sources else "既有成品")
            )
            print(f"[{marker:^8}] {_format_size(path):>10}  {path.name}")
    print(f"待處理來源：{len(sources)}；資料夾檔案：{len(files)}")
    return len(sources)


def _set_bool_env(name: str, value: bool | None) -> None:
    if value is not None:
        os.environ[name] = "1" if value else "0"


def apply_feature_switches(options: FeatureSwitches) -> None:
    _set_bool_env("ENABLE_ASR", options.asr)
    _set_bool_env("ENABLE_DEMUCS_ASR", options.demucs_asr)
    _set_bool_env("ENABLE_ASR_STREAM", options.asr_stream)
    _set_bool_env("EXPORT_SUBTITLES", options.subtitles)
    _set_bool_env("ENABLE_TRANSLATION", options.translation)
    _set_bool_env("ENABLE_DIALOGUE_TRIM", options.dialogue_trim)
    _set_bool_env("ENABLE_SELECTIVE_DOWNLOAD", options.selective_download)
    _set_bool_env("ENABLE_THREE_PHASE_SELECTION", options.three_phase_selection)
    _set_bool_env("ENABLE_EDGE_PADDING", options.edge_padding)
    _set_bool_env("AUDIO_AUTO_ENHANCE", options.enhance)
    _set_bool_env("ENABLE_METADATA", options.metadata)
    _set_bool_env("REUSE_ASR_RESULT", options.reuse_cache)
    if options.preview_seconds is not None:
        os.environ["PREVIEW_SECONDS"] = str(options.preview_seconds)
    if options.asr_chunk_seconds is not None:
        os.environ["ASR_STREAM_CHUNK_SECONDS"] = str(options.asr_chunk_seconds)
    if options.asr_batch_size is not None:
        os.environ["MOSS_ASR_BATCH_SIZE"] = str(options.asr_batch_size)
    if options.asr_backend:
        os.environ["ASR_BACKEND"] = options.asr_backend
        os.environ["CHOSEN_ASR_BACKEND"] = options.asr_backend
    if options.translation_model:
        os.environ["OPENROUTER_MODEL"] = options.translation_model
        os.environ["CHOSEN_OPENROUTER_MODEL"] = options.translation_model
    if options.reasoning_effort:
        os.environ["TRANSLATE_REASONING_EFFORT"] = options.reasoning_effort
        os.environ["CHOSEN_TRANSLATE_REASONING"] = options.reasoning_effort
    os.environ["TRANSLATE_FALLBACK_MODEL"] = os.getenv(
        "TRANSLATE_FALLBACK_MODEL", "x-ai/grok-4.5"
    )
    os.environ["CHOSEN_TRANSLATE_FALLBACK_MODEL"] = os.getenv(
        "CHOSEN_TRANSLATE_FALLBACK_MODEL", "x-ai/grok-4.5"
    )


def resolve_stage_options(
    stage_name: str,
    options: FeatureSwitches,
) -> FeatureSwitches:
    """補上該層預設值；不改動或連帶切換任何其他功能。"""
    defaults = {
        "asr": True,
        "demucs_asr": True,
        "asr_stream": False,
        "subtitles": True,
        "translation": True,
        "dialogue_trim": True,
        # 所有層預設 ON：先精選翻譯，再依字幕時間軸規劃下載
        "selective_download": True,
        "three_phase_selection": stage_name in {"shorts", "video", "chosen"},
        # 停頓 ≥1.5s 剪掉；所有流程預設不做前後延伸
        "edge_padding": False,
        "enhance": True,
        "metadata": True,
        "archive_grid": stage_name != "preview",
        "keep_work": False,
        "reuse_cache": True,
        "force": False,
        "preview_seconds": 180,
        "video_height": 480,
        "chosen_height": 1080,
        "asr_backend": "moss",
        "translation_model": "x-ai/grok-4.3",
        "reasoning_effort": "minimal",
        "trim_threshold": 30.0,
        "segment_gap": 1.5,
        "asr_chunk_seconds": 180,
        "asr_batch_size": 3,
    }
    values = {
        name: getattr(options, name)
        if getattr(options, name) is not None
        else default
        for name, default in defaults.items()
    }
    if stage_name not in {"shorts", "video", "chosen"}:
        values["three_phase_selection"] = False
    return replace(options, **values)


def validate_stage_options(stage_name: str, options: FeatureSwitches) -> None:
    if options.three_phase_selection and not options.selective_download:
        raise ValueError(
            f"{STAGES[stage_name].title}：30 秒三段精選需要精選下載；"
            "請開啟 selective_download，或關閉 three_phase_selection。"
        )
    if options.three_phase_selection and not options.dialogue_trim:
        raise ValueError(
            f"{STAGES[stage_name].title}：30 秒三段精選需要對白剪片；"
            "請開啟 dialogue_trim，或關閉 three_phase_selection。"
        )
    if not options.asr and not options.reuse_cache and (
        options.translation or options.dialogue_trim or options.selective_download
    ):
        raise ValueError(
            f"{STAGES[stage_name].title}：關閉 ASR 且不重用快取時，"
            "翻譯、對白剪片與精選下載沒有時間軸來源；請個別關閉它們，或開啟 --reuse-cache。"
        )
    if options.selective_download and options.translation is False:
        raise ValueError(
            f"{STAGES[stage_name].title}：精選下載需要翻譯；"
            "請開啟 --translation，或關閉 --selective-download。"
        )


def validate_folder_paths(
    label: str,
    source_dir: str | Path,
    output_dir: str | Path,
    archive_dir: str | Path,
    options: FeatureSwitches,
) -> None:
    """驗證所有入口共用的資料夾結構規則，不掃描或修改媒體。"""
    source = Path(source_dir).resolve()
    output = Path(output_dir).resolve()
    archive = Path(archive_dir).resolve()
    if options.metadata is False and source == output:
        raise ValueError(
            f"{label}：來源與成品使用同一資料夾時不可關閉 metadata，"
            "否則成品影片無法和新輸入區分；請開啟 metadata 或分離 output_dir。"
        )
    if options.archive_grid and source == archive:
        raise ValueError(
            f"{label}：開啟 archive 時 archive_dir 不可等於 source_dir，"
            "否則宮格會在 Inbox 內反覆改名與重跑。"
        )
    for path_label, directory in (
        ("source_dir", source),
        ("output_dir", output),
        ("archive_dir", archive),
    ):
        if directory.exists() and not directory.is_dir():
            raise ValueError(f"{label}：{path_label} 不是資料夾：{directory}")


def print_effective_options(stage_name: str, options: FeatureSwitches) -> None:
    def state(value: bool | None) -> str:
        return "ON" if value else "OFF"

    print(
        "實際設定："
        f"ASR={state(options.asr)}｜Demucs={state(options.demucs_asr)}｜"
        f"ASR串流={state(options.asr_stream)}｜"
        f"SRT={state(options.subtitles)}｜"
        f"翻譯={state(options.translation)}｜剪片={state(options.dialogue_trim)}｜"
        f"精選下載={state(options.selective_download)}｜"
        f"30秒三段={state(options.three_phase_selection)}｜"
        f"0.75延伸={state(options.edge_padding)}｜"
        f"增強={state(options.enhance)}｜Meta={state(options.metadata)}｜"
        f"歸檔={state(options.archive_grid)}｜快取={state(options.reuse_cache)}｜"
        f"強制重跑={state(options.force)}"
    )
    print(
        f"Backend={options.asr_backend}｜Model={options.translation_model}｜"
        f"Reasoning={options.reasoning_effort}｜"
        f"剪片門檻={options.trim_threshold}s｜停頓切段={options.segment_gap}s"
        f"｜延伸={0.75 if options.edge_padding else 0}s"
        f"｜ASR區段={options.asr_chunk_seconds}s｜ASR固定批次={options.asr_batch_size}"
    )


@contextmanager
def feature_environment(options: FeatureSwitches):
    """每一層使用隔離環境，完成後還原，避免設定污染下一層。"""
    names = (
        "ENABLE_ASR",
        "ENABLE_DEMUCS_ASR",
        "ENABLE_ASR_STREAM",
        "EXPORT_SUBTITLES",
        "ENABLE_TRANSLATION",
        "ENABLE_DIALOGUE_TRIM",
        "ENABLE_SELECTIVE_DOWNLOAD",
        "ENABLE_THREE_PHASE_SELECTION",
        "ENABLE_EDGE_PADDING",
        "AUDIO_AUTO_ENHANCE",
        "ENABLE_METADATA",
        "REUSE_ASR_RESULT",
        "PREVIEW_SECONDS",
        "ASR_STREAM_CHUNK_SECONDS",
        "MOSS_ASR_BATCH_SIZE",
        "ASR_BACKEND",
        "CHOSEN_ASR_BACKEND",
        "OPENROUTER_MODEL",
        "CHOSEN_OPENROUTER_MODEL",
        "TRANSLATE_FALLBACK_MODEL",
        "CHOSEN_TRANSLATE_FALLBACK_MODEL",
        "TRANSLATE_REASONING_EFFORT",
        "CHOSEN_TRANSLATE_REASONING",
        "THREE_PHASE_SELECTION_MODEL",
        "THREE_PHASE_SELECTION_FALLBACK_MODEL",
        "THREE_PHASE_SELECTION_REASONING",
        "THREE_PHASE_TRANSLATE_REASONING",
        "HIGH_VIDEO_HEIGHT",
        "HIGH_VIDEO_UNLIMITED",
    )
    before = {name: os.environ.get(name) for name in names}
    apply_feature_switches(options)
    try:
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def run_stage(
    stage_name: str,
    options: FeatureSwitches,
    *,
    moss_worker=None,
    audio_worker=None,
    source_dir: str | Path | None = None,
    final_dir: str | Path | None = None,
    archive_dir: str | Path | None = None,
    work_bucket: str | None = None,
) -> int:
    ensure_output_directories()
    options = resolve_stage_options(stage_name, options)
    validate_stage_options(stage_name, options)
    validate_folder_paths(
        STAGES[stage_name].title,
        source_dir or STAGES[stage_name].directory,
        final_dir
        or (GOOD_DIR if stage_name == "chosen" else STAGES[stage_name].directory),
        archive_dir or DOWNLOADED_DIR,
        options,
    )
    if work_bucket is None and source_dir is not None:
        resolved_source = Path(source_dir).resolve()
        default_source = STAGES[stage_name].directory.resolve()
        if os.path.normcase(str(resolved_source)) != os.path.normcase(
            str(default_source)
        ):
            digest = hashlib.sha1(
                str(resolved_source).casefold().encode("utf-8")
            ).hexdigest()[:10]
            work_bucket = f"{stage_name}-cli-{digest}"
    sources = (
        list_sources_in_directory(
            stage_name,
            source_dir,
            output_dir=final_dir,
            include_published=bool(options.force),
        )
        if source_dir is not None
        else list_stage_sources(
            stage_name,
            include_published=bool(options.force),
        )
    )
    resolved_final_dir = Path(
        final_dir
        if final_dir is not None
        else (GOOD_DIR if stage_name == "chosen" else STAGES[stage_name].directory)
    )
    sources = validate_source_collisions(
        sources,
        stage_name,
        resolved_final_dir,
    )
    if not sources:
        print(f"[SKIP] {STAGES[stage_name].title} 沒有待處理來源。")
        return 0
    print_effective_options(stage_name, options)
    with feature_environment(options):
        # 直接執行單層時，也讓該層所有來源共用 MOSS 與音訊 Enhance 權重。
        import full_video_pipeline
        import audio_enhance_stage

        moss_session = (
            nullcontext(moss_worker)
            if moss_worker is not None
            else (
            full_video_pipeline.moss_asr_session()
            if options.asr and options.asr_backend == "moss"
            else nullcontext(None)
            )
        )
        enhance_session = (
            nullcontext(audio_worker)
            if audio_worker is not None
            else (
                audio_enhance_stage.audio_enhance_session()
                if options.enhance
                else nullcontext(None)
            )
        )
        with moss_session as owned_moss, enhance_session as owned_audio:
            return _execute_stage(
                stage_name,
                options,
                moss_worker=owned_moss,
                audio_worker=owned_audio,
                source_dir=source_dir,
                final_dir=final_dir,
                archive_dir=archive_dir,
                work_bucket=work_bucket,
                sources=sources,
            )


def _execute_stage(
    stage_name: str,
    options: FeatureSwitches,
    *,
    moss_worker=None,
    audio_worker=None,
    source_dir: str | Path | None = None,
    final_dir: str | Path | None = None,
    archive_dir: str | Path | None = None,
    work_bucket: str | None = None,
    sources: list[Path] | None = None,
) -> int:
    if sources is None:
        sources = (
            list_sources_in_directory(
                stage_name,
                source_dir,
                output_dir=final_dir,
                include_published=bool(options.force),
            )
            if source_dir is not None
            else list_stage_sources(
                stage_name,
                include_published=bool(options.force),
            )
        )
    if not sources:
        print(f"[SKIP] {STAGES[stage_name].title} 沒有待處理來源。")
        return 0

    if stage_name == "preview":
        import preview_pipeline

        failures = 0
        for index, source in enumerate(sources, 1):
            print(f"\n[preview {index}/{len(sources)}] {source.name}")
            try:
                preview_pipeline.process_preview_from_grid(
                    source,
                    final_dir=Path(final_dir or PREVIEW_VIDEOS_DIR),
                    archive_dir=Path(archive_dir or DOWNLOADED_DIR),
                    keep_work=options.keep_work,
                    enable_asr=options.asr,
                    export_subtitles=options.subtitles,
                    enable_dialogue_trim=options.dialogue_trim,
                    enable_selective_download=options.selective_download,
                    enable_edge_padding=options.edge_padding,
                    enable_enhance=options.enhance,
                    enable_metadata=options.metadata,
                    archive_grid_on_done=options.archive_grid,
                    dialogue_trim_threshold=options.trim_threshold,
                    segment_gap=options.segment_gap,
                    force=options.force,
                    moss_worker=moss_worker,
                    audio_worker=audio_worker,
                    work_bucket=work_bucket or "02_preview_videos",
                )
            except Exception as exc:
                print(f"  [FAIL] {exc}")
                failures += 1
        return failures

    if stage_name == "video":
        import full_video_pipeline

        os.environ["ASR_BACKEND"] = options.asr_backend or os.getenv(
            "STANDARD_ASR_BACKEND", "whisper"
        )
        os.environ["OPENROUTER_MODEL"] = options.translation_model or os.getenv(
            "STANDARD_OPENROUTER_MODEL", "x-ai/grok-4.3"
        )
        os.environ["TRANSLATE_REASONING_EFFORT"] = (
            options.reasoning_effort
            or os.getenv("STANDARD_TRANSLATE_REASONING", "minimal")
        )
        os.environ["TRANSLATE_FALLBACK_MODEL"] = os.getenv(
            "STANDARD_TRANSLATE_FALLBACK_MODEL", "x-ai/grok-4.5"
        )
        def process_video(index: int, source: Path) -> None:
            print(f"\n[video {index}/{len(sources)}] {source.name}")
            full_video_pipeline.process_full_video_from_grid(
                source,
                final_dir=Path(final_dir or VIDEOS_DIR),
                archive_dir=Path(archive_dir or DOWNLOADED_DIR),
                keep_proxy=bool(options.keep_work),
                max_height=options.video_height or 480,
                enable_enhance=options.enhance,
                enable_asr=options.asr,
                export_subtitles=options.subtitles,
                enable_dialogue_trim=options.dialogue_trim,
                enable_translation=options.translation,
                enable_selective_download=options.selective_download,
                enable_three_phase_selection=options.three_phase_selection,
                enable_edge_padding=options.edge_padding,
                enable_metadata=options.metadata,
                dialogue_trim_threshold=options.trim_threshold,
                segment_gap=options.segment_gap,
                force=options.force,
                work_bucket=work_bucket or "03_videos",
                pipeline_stage="video",
                archive_grid_on_done=options.archive_grid,
                moss_worker=moss_worker,
                audio_worker=audio_worker,
            )

        workers = source_pipeline_workers()
        print(f"[video] 來源管線併行={workers}；MOSS／音訊 GPU 仍會安全排隊")
        failures = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="video-source") as executor:
            futures = {
                executor.submit(process_video, index, source): source
                for index, source in enumerate(sources, 1)
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    print(f"  [FAIL] {futures[future].name}：{exc}")
                    failures += 1
        return failures

    if stage_name == "shorts":
        import full_video_pipeline

        failures = 0
        for index, source in enumerate(sources, 1):
            print(f"\n[shorts {index}/{len(sources)}] {source.name}")
            try:
                full_video_pipeline.process_full_video_from_grid(
                    source,
                    final_dir=Path(final_dir or SHORTS_DIR),
                    archive_dir=Path(archive_dir or DOWNLOADED_DIR),
                    keep_proxy=bool(options.keep_work),
                    max_height=1080,
                    enable_enhance=options.enhance,
                    enable_asr=options.asr,
                    export_subtitles=options.subtitles,
                    enable_dialogue_trim=options.dialogue_trim,
                    enable_translation=options.translation,
                    enable_selective_download=options.selective_download,
                    enable_three_phase_selection=options.three_phase_selection,
                    enable_edge_padding=options.edge_padding,
                    enable_metadata=options.metadata,
                    dialogue_trim_threshold=options.trim_threshold,
                    segment_gap=options.segment_gap,
                    force=options.force,
                    work_bucket=work_bucket or "02_shorts",
                    pipeline_stage="shorts",
                    archive_grid_on_done=options.archive_grid,
                    moss_worker=moss_worker,
                    audio_worker=audio_worker,
                    analysis_limit_seconds=540.0,
                    reuse_embedded_translation=True,
                    always_download_subtitle_ranges=False,
                    require_subtitle_ranges=True,
                    unlimited_high_quality=True,
                )
            except Exception as exc:
                print(f"  [FAIL] {exc}")
                failures += 1
        return failures

    import chosen_pipeline

    successes, failures = chosen_pipeline.process_chosen_items(
        sources,
        final_dir=Path(final_dir or GOOD_DIR),
        archive_dir=Path(archive_dir or DOWNLOADED_DIR),
        archive_grid=options.archive_grid,
        keep_work=bool(options.keep_work),
        enable_asr=options.asr,
        export_subtitles=options.subtitles,
        enable_translation=options.translation,
        enable_dialogue_trim=options.dialogue_trim,
        enable_selective_download=options.selective_download,
        enable_three_phase_selection=options.three_phase_selection,
        enable_edge_padding=options.edge_padding,
        enable_enhance=options.enhance,
        enable_metadata=options.metadata,
        max_height=options.chosen_height,
        dialogue_trim_threshold=options.trim_threshold,
        segment_gap=options.segment_gap,
        force=options.force,
        moss_worker=moss_worker,
        audio_worker=audio_worker,
        source_workers=source_pipeline_workers(),
        work_bucket=work_bucket or "05_chosen",
    )
    print(f"[chosen] 完成：成功 {successes}；失敗 {failures}")
    return failures


def run_folder_config(
    config: FolderConfig,
    overrides: FeatureSwitches,
    *,
    moss_worker=None,
    audio_worker=None,
) -> int:
    if not config.enabled:
        print(f"[SKIP] {config.filename} 已停用。")
        return 0
    for directory in (
        config.source_dir,
        config.output_dir,
        config.archive_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return run_stage(
        config.stage_name,
        merge_feature_switches(config.options, overrides),
        moss_worker=moss_worker,
        audio_worker=audio_worker,
        source_dir=config.source_dir,
        final_dir=config.output_dir,
        archive_dir=config.archive_dir,
        work_bucket=config.work_bucket,
    )


def preflight_folder_configs(
    configs: Iterable[FolderConfig],
    overrides: FeatureSwitches,
) -> tuple[
    list[FolderConfig],
    dict[str, FeatureSwitches],
    dict[str, list[Path]],
]:
    """在任何下載前驗證全部設定、路徑及單一／跨設定成品碰撞。"""
    enabled = [config for config in configs if config.enabled]
    effective: dict[str, FeatureSwitches] = {}
    sources_by_config: dict[str, list[Path]] = {}
    targets: dict[tuple[str, str], tuple[FolderConfig, Path]] = {}
    source_roots: dict[str, FolderConfig] = {}
    for config in enabled:
        source_key = str(config.source_dir.resolve()).casefold()
        existing_source = source_roots.get(source_key)
        if existing_source is not None:
            raise ValueError(
                "同一來源資料夾只能有一份啟用設定："
                f"{existing_source.filename}、{config.filename}"
            )
        source_roots[source_key] = config
        options = resolve_stage_options(
            config.stage_name,
            merge_feature_switches(config.options, overrides),
        )
        try:
            validate_stage_options(config.stage_name, options)
        except ValueError as exc:
            raise ValueError(f"{config.filename}：{exc}") from exc
        validate_folder_paths(
            config.filename,
            config.source_dir,
            config.output_dir,
            config.archive_dir,
            options,
        )
        sources = validate_source_collisions(
            list_folder_config_sources(
                config,
                include_published=bool(options.force),
            ),
            config.stage_name,
            config.output_dir,
        )
        effective[config.id] = options
        sources_by_config[config.id] = sources
        output_key = str(config.output_dir.resolve()).casefold()
        for source in sources:
            stem = expected_output_stem(source, config.stage_name).casefold()
            target_key = (output_key, stem)
            existing = targets.get(target_key)
            if existing is not None:
                other_config, other_source = existing
                raise ValueError(
                    "跨設定成品碰撞："
                    f"{other_config.filename}/{other_source.name} 與 "
                    f"{config.filename}/{source.name} 都會寫入 "
                    f"{config.output_dir / (stem + '.mp4')}"
                )
            targets[target_key] = (config, source)
    return enabled, effective, sources_by_config


def run_folder_configs(
    configs: Iterable[FolderConfig],
    overrides: FeatureSwitches,
) -> int:
    """依 JSON 檔名順序執行所有啟用的資料夾設定。"""
    enabled, effective, sources_by_config = preflight_folder_configs(
        configs,
        overrides,
    )
    if not enabled:
        print("[SKIP] 沒有已啟用的資料夾設定。")
        return 0
    for config in enabled:
        for directory in (
            config.source_dir,
            config.output_dir,
            config.archive_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    use_moss = any(
        bool(sources_by_config[config.id])
        and effective[config.id].asr
        and effective[config.id].asr_backend == "moss"
        for config in enabled
    )
    use_other_asr = any(
        bool(sources_by_config[config.id])
        and effective[config.id].asr
        and effective[config.id].asr_backend != "moss"
        for config in enabled
    )
    use_enhance = any(
        sources_by_config[config.id] and effective[config.id].enhance
        for config in enabled
    )
    has_sources = any(
        sources_by_config[config.id]
        for config in enabled
    )
    if not has_sources:
        for config in enabled:
            print_folder_config_inventory(config, overrides)
            print(f"[SKIP] {config.name} 沒有待處理來源。")
        return 0
    if use_moss and use_other_asr:
        # 不讓常駐 MOSS 與其他 ASR backend 同時占用 GPU；逐設定開啟與釋放。
        failures = 0
        for config in enabled:
            print_folder_config_inventory(config, overrides)
            failures += run_folder_config(config, overrides)
        return failures

    # 同一次 BAT 執行共用 GPU worker；JSON 之間仍嚴格依檔名順序切換。
    import full_video_pipeline
    import audio_enhance_stage

    previous_backend = os.environ.get("ASR_BACKEND")
    if use_moss:
        os.environ["ASR_BACKEND"] = "moss"
    try:
        moss_session = (
            full_video_pipeline.moss_asr_session()
            if use_moss
            else nullcontext(None)
        )
        enhance_session = (
            audio_enhance_stage.audio_enhance_session()
            if use_enhance
            else nullcontext(None)
        )
        with moss_session as moss_worker, enhance_session as audio_worker:
            failures = 0
            for config in enabled:
                print_folder_config_inventory(config, overrides)
                failures += run_folder_config(
                    config,
                    overrides,
                    moss_worker=(
                        moss_worker
                        if effective[config.id].asr
                        and effective[config.id].asr_backend == "moss"
                        else None
                    ),
                    audio_worker=(
                        audio_worker
                        if effective[config.id].enhance
                        else None
                    ),
                )
            return failures
    finally:
        if use_moss:
            if previous_backend is None:
                os.environ.pop("ASR_BACKEND", None)
            else:
                os.environ["ASR_BACKEND"] = previous_backend


def run_folder_configs_interactive(
    configs: Iterable[FolderConfig],
    overrides: FeatureSwitches,
) -> int:
    configs = list(configs)
    preflight_folder_configs(configs, overrides)
    failures = 0
    for config in configs:
        if not config.enabled:
            print(f"[SKIP] {config.filename} 已停用。")
            continue
        pending = print_folder_config_inventory(config, overrides)
        if not pending:
            continue
        while True:
            answer = input("執行此設定？[R] 執行 / [S] 跳過 / [Q] 結束：").strip().casefold()
            if answer in {"r", "run", "y", "yes", "執行"}:
                failures += run_folder_config(config, overrides)
                break
            if answer in {"s", "skip", "n", "no", "", "跳過"}:
                break
            if answer in {"q", "quit", "exit", "結束"}:
                return failures
            print("請輸入 R、S 或 Q。")
    return failures


def run_stages(stage_names: Iterable[str], options: FeatureSwitches) -> int:
    names = list(stage_names)
    use_moss = any(
        resolve_stage_options(stage_name, options).asr
        and resolve_stage_options(stage_name, options).asr_backend == "moss"
        for stage_name in names
    )
    use_enhance = any(
        resolve_stage_options(stage_name, options).enhance
        for stage_name in names
    )
    has_sources = any(
        list_stage_sources(
            stage_name,
            include_published=bool(resolve_stage_options(stage_name, options).force),
        )
        for stage_name in names
    )
    if not has_sources:
        for stage_name in names:
            print_stage_inventory(stage_name)
            print(f"[SKIP] {STAGES[stage_name].title} 沒有待處理來源。")
        return 0

    # 一次 BAT 執行共用 MOSS 與音訊 Enhance worker，跨各層也不重載。
    import full_video_pipeline
    import audio_enhance_stage

    previous_backend = os.environ.get("ASR_BACKEND")
    if use_moss:
        os.environ["ASR_BACKEND"] = "moss"
    try:
        moss_session = (
            full_video_pipeline.moss_asr_session()
            if use_moss
            else nullcontext(None)
        )
        enhance_session = (
            audio_enhance_stage.audio_enhance_session()
            if use_enhance
            else nullcontext(None)
        )
        with moss_session as moss_worker, enhance_session as audio_worker:
            failures = 0
            for stage_name in names:
                print_stage_inventory(stage_name)
                effective_options = resolve_stage_options(stage_name, options)
                failures += run_stage(
                    stage_name,
                    options,
                    moss_worker=(
                        moss_worker
                        if effective_options.asr
                        and effective_options.asr_backend == "moss"
                        else None
                    ),
                    audio_worker=(
                        audio_worker
                        if effective_options.enhance
                        else None
                    ),
                )
            return failures
    finally:
        if use_moss:
            if previous_backend is None:
                os.environ.pop("ASR_BACKEND", None)
            else:
                os.environ["ASR_BACKEND"] = previous_backend


def run_interactive(
    options: FeatureSwitches,
    stage_names: Iterable[str] = STAGE_ORDER,
) -> int:
    """依 Preview → Shorts → Video → Chosen 顯示並逐層詢問。"""
    failures = 0
    for stage_name in stage_names:
        pending = print_stage_inventory(stage_name)
        if not pending:
            continue
        while True:
            answer = input("執行此層？[R] 執行 / [S] 跳過 / [Q] 結束：").strip().casefold()
            if answer in {"r", "run", "y", "yes", "執行"}:
                failures += run_stage(stage_name, options)
                break
            if answer in {"s", "skip", "n", "no", "", "跳過"}:
                break
            if answer in {"q", "quit", "exit", "結束"}:
                return failures
            print("請輸入 R、S 或 Q。")
    return failures

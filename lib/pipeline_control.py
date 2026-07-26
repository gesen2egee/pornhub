"""三層下載流程的盤點、預算設定與執行控制。"""

from __future__ import annotations

import os
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from project_paths import (
    CHOSEN_DIR,
    DOWNLOADED_DIR,
    GOOD_DIR,
    PREVIEW_VIDEOS_DIR,
    VIDEOS_DIR,
    ensure_output_directories,
)

STAGE_ORDER = ("preview", "video", "chosen")
GRID_EXTENSIONS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}


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


STAGES = {
    "preview": StageDefinition(
        "preview",
        "第一層：Preview Video",
        PREVIEW_VIDEOS_DIR,
        "$ 低預算",
        "每段 3 分鐘低畫質、MOSS、對話不足會續抓、自動 enhance、軟字幕",
        frozenset(GRID_EXTENSIONS | VIDEO_EXTENSIONS),
    ),
    "video": StageDefinition(
        "video",
        "第二層：Video",
        VIDEOS_DIR,
        "$$ 中預算",
        "480P 全片、MOSS、Grok 4.3、自動 enhance",
        frozenset(GRID_EXTENSIONS | VIDEO_EXTENSIONS),
    ),
    "chosen": StageDefinition(
        "chosen",
        "第三層：Chosen",
        CHOSEN_DIR,
        "$$$ 高預算",
        "1080P、MOSS、Grok 4.5、音訊增強",
        frozenset(GRID_EXTENSIONS | VIDEO_EXTENSIONS),
    ),
}


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


def list_stage_sources(
    stage_name: str,
    *,
    include_published: bool = False,
) -> list[Path]:
    stage = STAGES[stage_name]
    if not stage.directory.is_dir():
        return []
    return [
        path
        for path in sorted(stage.directory.iterdir(), key=lambda item: item.name.casefold())
        if path.is_file()
        and path.suffix.casefold() in stage.accepted_extensions
        and (
            include_published
            or not _published_in_same_stage(path, stage_name)
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


def resolve_stage_options(
    stage_name: str,
    options: FeatureSwitches,
) -> FeatureSwitches:
    """補上該層預設值；不改動或連帶切換任何其他功能。"""
    defaults = {
        "asr": True,
        "demucs_asr": True,
        "asr_stream": True,
        "subtitles": True,
        "translation": True,
        "dialogue_trim": True,
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
        "translation_model": (
            "x-ai/grok-4.5" if stage_name == "chosen" else "x-ai/grok-4.3"
        ),
        "reasoning_effort": "minimal" if stage_name == "chosen" else "none",
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
    return replace(options, **values)


def validate_stage_options(stage_name: str, options: FeatureSwitches) -> None:
    if not options.asr and not options.reuse_cache and (
        options.translation or options.dialogue_trim
    ):
        raise ValueError(
            f"{STAGES[stage_name].title}：關閉 ASR 且不重用快取時，"
            "翻譯與對白剪片沒有時間軸來源；請個別關閉它們，或開啟 --reuse-cache。"
        )


def print_effective_options(stage_name: str, options: FeatureSwitches) -> None:
    def state(value: bool | None) -> str:
        return "ON" if value else "OFF"

    print(
        "實際設定："
        f"ASR={state(options.asr)}｜Demucs={state(options.demucs_asr)}｜"
        f"ASR串流={state(options.asr_stream)}｜"
        f"SRT={state(options.subtitles)}｜"
        f"翻譯={state(options.translation)}｜剪片={state(options.dialogue_trim)}｜"
        f"增強={state(options.enhance)}｜Meta={state(options.metadata)}｜"
        f"歸檔={state(options.archive_grid)}｜快取={state(options.reuse_cache)}｜"
        f"強制重跑={state(options.force)}"
    )
    print(
        f"Backend={options.asr_backend}｜Model={options.translation_model}｜"
        f"Reasoning={options.reasoning_effort}｜"
        f"剪片門檻={options.trim_threshold}s｜段落間隔={options.segment_gap}s"
        f"｜ASR區段={options.asr_chunk_seconds}s｜ASR批次上限={options.asr_batch_size}"
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
        "TRANSLATE_REASONING_EFFORT",
        "CHOSEN_TRANSLATE_REASONING",
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
) -> int:
    ensure_output_directories()
    options = resolve_stage_options(stage_name, options)
    validate_stage_options(stage_name, options)
    print_effective_options(stage_name, options)
    with feature_environment(options):
        if moss_worker is not None:
            return _execute_stage(stage_name, options, moss_worker=moss_worker)
        # 直接執行單層時，也讓該層所有來源共用同一個 MOSS 權重。
        import full_video_pipeline

        session = (
            full_video_pipeline.moss_asr_session()
            if options.asr and options.asr_backend == "moss"
            else nullcontext(None)
        )
        with session as owned_worker:
            return _execute_stage(stage_name, options, moss_worker=owned_worker)


def _execute_stage(stage_name: str, options: FeatureSwitches, *, moss_worker=None) -> int:
    sources = list_stage_sources(
        stage_name, include_published=bool(options.force)
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
                    final_dir=PREVIEW_VIDEOS_DIR,
                    keep_work=options.keep_work,
                    enable_asr=options.asr,
                    export_subtitles=options.subtitles,
                    enable_dialogue_trim=options.dialogue_trim,
                    enable_enhance=options.enhance,
                    enable_metadata=options.metadata,
                    archive_grid_on_done=options.archive_grid,
                    dialogue_trim_threshold=options.trim_threshold,
                    segment_gap=options.segment_gap,
                    force=options.force,
                    moss_worker=moss_worker,
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
            or os.getenv("STANDARD_TRANSLATE_REASONING", "none")
        )
        failures = 0
        for index, source in enumerate(sources, 1):
            print(f"\n[video {index}/{len(sources)}] {source.name}")
            try:
                full_video_pipeline.process_full_video_from_grid(
                    source,
                    final_dir=VIDEOS_DIR,
                    archive_dir=DOWNLOADED_DIR,
                    keep_proxy=bool(options.keep_work),
                    max_height=options.video_height or 480,
                    enable_enhance=(
                        options.enhance
                    ),
                    enable_asr=options.asr,
                    export_subtitles=options.subtitles,
                    enable_dialogue_trim=options.dialogue_trim,
                    enable_translation=options.translation,
                    enable_metadata=options.metadata,
                    dialogue_trim_threshold=options.trim_threshold,
                    segment_gap=options.segment_gap,
                    force=options.force,
                    work_bucket="03_videos",
                    archive_grid_on_done=options.archive_grid,
                    moss_worker=moss_worker,
                )
            except Exception as exc:
                print(f"  [FAIL] {exc}")
                failures += 1
        return failures

    import chosen_pipeline

    successes, failures = chosen_pipeline.process_chosen_items(
        sources,
        final_dir=GOOD_DIR,
        archive_grid=options.archive_grid,
        keep_work=bool(options.keep_work),
        enable_asr=options.asr,
        export_subtitles=options.subtitles,
        enable_translation=options.translation,
        enable_dialogue_trim=options.dialogue_trim,
        enable_enhance=options.enhance,
        enable_metadata=options.metadata,
        max_height=options.chosen_height,
        dialogue_trim_threshold=options.trim_threshold,
        segment_gap=options.segment_gap,
        force=options.force,
        moss_worker=moss_worker,
    )
    print(f"[chosen] 完成：成功 {successes}；失敗 {failures}")
    return failures


def run_stages(stage_names: Iterable[str], options: FeatureSwitches) -> int:
    names = list(stage_names)
    use_moss = any(
        resolve_stage_options(stage_name, options).asr
        and resolve_stage_options(stage_name, options).asr_backend == "moss"
        for stage_name in names
    )
    has_sources = any(
        list_stage_sources(
            stage_name,
            include_published=bool(resolve_stage_options(stage_name, options).force),
        )
        for stage_name in names
    )
    if not use_moss or not has_sources:
        failures = 0
        for stage_name in names:
            print_stage_inventory(stage_name)
            failures += run_stage(stage_name, options)
        return failures

    # 一次 BAT 執行共用同一個 MOSS worker，跨 Preview／Video／Chosen 也不重載。
    import full_video_pipeline

    previous_backend = os.environ.get("ASR_BACKEND")
    os.environ["ASR_BACKEND"] = "moss"
    try:
        with full_video_pipeline.moss_asr_session() as moss_worker:
            failures = 0
            for stage_name in names:
                print_stage_inventory(stage_name)
                failures += run_stage(
                    stage_name, options, moss_worker=moss_worker
                )
            return failures
    finally:
        if previous_backend is None:
            os.environ.pop("ASR_BACKEND", None)
        else:
            os.environ["ASR_BACKEND"] = previous_backend


def run_interactive(
    options: FeatureSwitches,
    stage_names: Iterable[str] = STAGE_ORDER,
) -> int:
    """依 Preview → Video → Chosen 顯示內容，再讓使用者逐層決定預算。"""
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

"""四層下載流程的盤點、預算設定與執行控制。"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from project_paths import (
    CHOSEN_DIR,
    DOWNLOADED_DIR,
    GOOD_DIR,
    PREVIEW_VIDEOS_DIR,
    SHORTS_DIR,
    VIDEOS_DIR,
    ensure_output_directories,
)

STAGE_ORDER = ("preview", "shorts", "video", "chosen")
GRID_EXTENSIONS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
DEFAULT_SOURCE_PIPELINE_WORKERS = 2


def source_pipeline_workers() -> int:
    """同層可重疊的影片管線數；GPU worker 仍由各自鎖序列化。"""
    try:
        value = int(os.getenv("PIPELINE_SOURCE_WORKERS", str(DEFAULT_SOURCE_PIPELINE_WORKERS)))
    except ValueError:
        value = DEFAULT_SOURCE_PIPELINE_WORKERS
    return max(1, min(value, 4))


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
        "240P/MOSS 串流→Grok 4.5（失敗改 Step 3.7 Flash）30秒三段→關鍵格安全高畫質切塊、音訊判斷 enhance＋crossfade",
        frozenset(GRID_EXTENSIONS | VIDEO_EXTENSIONS),
    ),
    "shorts": StageDefinition(
        "shorts",
        "第二層：Shorts",
        SHORTS_DIR,
        "$$ 精準預算",
        "內嵌翻譯直接抓最高畫質片段；僅 URL 則先分析前 9 分鐘 240P",
        frozenset(GRID_EXTENSIONS | VIDEO_EXTENSIONS),
    ),
    "chosen": StageDefinition(
        "chosen",
        "第四層：Chosen",
        CHOSEN_DIR,
        "$$$ 高預算",
        "240P/MOSS 串流→Grok 4.5（失敗改 Step 3.7 Flash）30秒三段→關鍵格安全 1080P 切塊、音訊判斷 enhance＋crossfade",
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


def _grid_has_published_output(path: Path, stage_name: str) -> bool:
    """九宮格已有同名、同層成品時，掃描階段直接略過。"""
    if path.suffix.casefold() not in GRID_EXTENSIONS:
        return False
    stage = STAGES[stage_name]
    output_dir = GOOD_DIR if stage_name == "chosen" else stage.directory
    candidates = [output_dir / f"{path.stem}.mp4"]
    # Chosen 的影片 URL 載體會移除四位數前綴；兼容同名舊成品。
    if stage_name == "chosen" and len(path.stem) > 5 and path.stem[:4].isdigit():
        candidates.append(output_dir / f"{path.stem[5:]}.mp4")
    return any(
        candidate.is_file() and _published_in_same_stage(candidate, stage_name)
        for candidate in candidates
    )


def list_stage_sources(
    stage_name: str,
    *,
    include_published: bool = False,
) -> list[Path]:
    return list_sources_in_directory(
        stage_name,
        STAGES[stage_name].directory,
        include_published=include_published,
    )


def list_sources_in_directory(
    stage_name: str,
    directory: str | Path,
    *,
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
            or (
                not _published_in_same_stage(path, stage_name)
                and not _grid_has_published_output(path, stage_name)
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
        # 所有層預設 ON：先精選翻譯，再依字幕時間軸規劃下載
        "selective_download": True,
        "three_phase_selection": stage_name in {"video", "chosen"},
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
        "translation_model": "x-ai/grok-4.5",
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
    if stage_name not in {"video", "chosen"}:
        values["three_phase_selection"] = False
    return replace(options, **values)


def validate_stage_options(stage_name: str, options: FeatureSwitches) -> None:
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
        "TRANSLATE_REASONING_EFFORT",
        "CHOSEN_TRANSLATE_REASONING",
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
) -> int:
    ensure_output_directories()
    options = resolve_stage_options(stage_name, options)
    validate_stage_options(stage_name, options)
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
) -> int:
    sources = (
        list_sources_in_directory(
            stage_name,
            source_dir,
            include_published=bool(options.force),
        )
        if source_dir is not None
        else list_stage_sources(stage_name, include_published=bool(options.force))
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
            "STANDARD_OPENROUTER_MODEL", "x-ai/grok-4.5"
        )
        os.environ["TRANSLATE_REASONING_EFFORT"] = (
            options.reasoning_effort
            or os.getenv("STANDARD_TRANSLATE_REASONING", "minimal")
        )
        os.environ["TRANSLATE_FALLBACK_MODEL"] = os.getenv(
            "STANDARD_TRANSLATE_FALLBACK_MODEL", "stepfun/step-3.7-flash"
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
                work_bucket="03_videos",
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
                    enable_dialogue_trim=True,
                    enable_translation=options.translation,
                    enable_selective_download=options.selective_download,
                    enable_edge_padding=options.edge_padding,
                    enable_metadata=options.metadata,
                    dialogue_trim_threshold=options.trim_threshold,
                    segment_gap=options.segment_gap,
                    force=options.force,
                    work_bucket="02_shorts",
                    archive_grid_on_done=options.archive_grid,
                    moss_worker=moss_worker,
                    audio_worker=audio_worker,
                    analysis_limit_seconds=540.0,
                    reuse_embedded_translation=True,
                    always_download_subtitle_ranges=True,
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
        failures = 0
        for stage_name in names:
            print_stage_inventory(stage_name)
            failures += run_stage(stage_name, options)
        return failures

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
                failures += run_stage(
                    stage_name,
                    options,
                    moss_worker=moss_worker,
                    audio_worker=audio_worker,
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

"""下載入口：只負責 CLI、四層下載控制與維護模式分流。"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from contextlib import nullcontext
from pathlib import Path

import download_maintenance as maintenance
from download_maintenance import *  # noqa: F401,F403 - 保留舊工具的相容入口
from pipeline_control import (
    FeatureSwitches,
    feature_environment,
    parse_stage_names,
    print_stage_inventory,
    resolve_stage_options,
    run_interactive,
    run_stages,
    validate_stage_options,
)
from project_paths import DOWNLOADED_DIR, VIDEOS_DIR, ensure_output_directories

WORK_TEMP_DIR = maintenance.WORK_TEMP_DIR


def _pipeline_dir(target_dir):
    """相容舊呼叫，但使用入口模組可覆寫的暫存根目錄。"""
    return os.path.join(
        WORK_TEMP_DIR,
        "pipeline",
        os.path.basename(os.path.normpath(target_dir)),
    )


def remove_invalid_video(path, label):
    """只在 ffprobe 明確判定無 video stream 時移除檔案。"""
    stream_state = has_video_stream(path) if os.path.exists(path) else None
    if stream_state is not False:
        return False
    try:
        os.remove(path)
        print(f"   [INVALID] {label} 沒有 video stream，已移除並重新下載")
    except OSError as exc:
        print(f"   [!] 無法移除無效的 {label}：{exc}")
    return True


def process_single_directory(
    target_dir,
    is_low_quality,
    subtitle_worker,
    selected_jpgs=None,
):
    """舊 API 相容層；既有正式片只補排字幕，新來源交給新 pipeline。"""
    if is_low_quality:
        return maintenance.process_single_directory(
            target_dir,
            is_low_quality,
            subtitle_worker,
            selected_jpgs=selected_jpgs,
        )
    jpgs = (
        sorted(selected_jpgs)
        if selected_jpgs is not None
        else sorted(glob.glob(os.path.join(target_dir, "*.jpg")))
    )
    pending = []
    for jpg in jpgs:
        stem = re.sub(r"^\d{4}-", "", Path(jpg).stem)
        video = os.path.join(target_dir, f"{stem}.mp4")
        if not os.path.exists(video) or has_video_stream(video) is not True:
            pending.append(jpg)
            continue
        url = get_video_url_from_image(jpg)
        if url:
            upgrade_media_web_meta(jpg, video, url)
        subtitle_worker.enqueue(
            video,
            video,
            jpg,
            is_low_quality=False,
        )
    if pending:
        return maintenance.process_official_directory(selected_jpgs=pending)
    return 0


def enqueue_staged_subtitle_retries(target_dir, is_low_quality, subtitle_worker):
    """相容舊暫存目錄；正式片先發布，再補排字幕。"""
    pipeline_dir = _pipeline_dir(target_dir)
    queued = 0
    for staged in sorted(glob.glob(os.path.join(pipeline_dir, "*.mp4"))):
        final_video = os.path.join(target_dir, os.path.basename(staged))
        if os.path.exists(final_video):
            continue
        final_video = publish_official_video(staged, final_video)
        grid = maintenance._archived_grid_for_video(final_video)
        subtitle_worker.enqueue(
            final_video,
            final_video,
            grid,
            is_low_quality=is_low_quality,
            archive_grid=False,
        )
        queued += 1
    return queued


def run_download_process(
    retry_subtitles: bool = False,
    repair_over_1080: bool = False,
    *,
    stage_names: list[str] | None = None,
    options: FeatureSwitches | None = None,
) -> int:
    """程式化入口；維護模式與四層流程明確分流。"""
    if retry_subtitles or repair_over_1080:
        return maintenance.run_download_process(
            retry_subtitles=retry_subtitles,
            repair_over_1080=repair_over_1080,
        )
    failures = run_stages(
        stage_names or ["preview", "shorts", "video", "chosen"],
        options or FeatureSwitches(),
    )
    return 3 if failures else 0


def _boolean_switch(parser: argparse.ArgumentParser, name: str, help_text: str) -> None:
    parser.add_argument(
        f"--{name}",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=help_text,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview → Shorts → Video → Chosen 下載控制器"
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="逐層顯示檔案並詢問是否執行",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="只列出各層檔案與待處理數量，不下載",
    )
    parser.add_argument(
        "--stages",
        nargs="+",
        metavar="STAGE",
        help="指定 preview、shorts、video、chosen；可用空白或逗號分隔",
    )
    _boolean_switch(parser, "translation", "開啟或關閉 OpenRouter 翻譯")
    _boolean_switch(parser, "asr", "開啟或關閉語音辨識")
    _boolean_switch(parser, "demucs-asr", "開啟或關閉 ASR 前 Demucs 人聲分離")
    _boolean_switch(parser, "asr-stream", "開啟或關閉 240P 分段下載與 ASR 串流")
    _boolean_switch(parser, "subtitles", "開啟或關閉外掛 SRT 輸出")
    _boolean_switch(parser, "dialogue-trim", "開啟或關閉依對白剪片")
    _boolean_switch(
        parser,
        "selective-download",
        "開啟或關閉精選翻譯／下載（預設開；所有層皆用；"
        "以保留對白淨長判斷 >30s 剪片；歌詞則完整翻譯）",
    )
    _boolean_switch(
        parser,
        "edge-padding",
        "開啟或關閉對白前後 0.75s 延伸（預設關；停頓≥1.5s 仍會剪掉）",
    )
    _boolean_switch(parser, "enhance", "開啟或關閉音訊增強")
    _boolean_switch(parser, "metadata", "開啟或關閉 MP4/JPG Metadata 寫入")
    _boolean_switch(parser, "archive", "開啟或關閉完成後九宮格歸檔")
    _boolean_switch(parser, "keep-work", "開啟或關閉保留 pipeline 工作檔")
    _boolean_switch(parser, "reuse-cache", "開啟或關閉重用 ASR 字幕快取")
    _boolean_switch(parser, "force", "開啟或關閉強制重跑既有成品")
    parser.add_argument(
        "--preview-seconds",
        type=int,
        metavar="SECONDS",
        help="Preview 下載秒數，預設 180",
    )
    parser.add_argument(
        "--video-height", type=int, metavar="P", help="Video 解析度高度，預設 480"
    )
    parser.add_argument(
        "--chosen-height", type=int, metavar="P", help="Chosen 解析度高度，預設 1080"
    )
    parser.add_argument(
        "--shorts-height", type=int, metavar="P", help="Shorts 解析度高度，預設 1080"
    )
    parser.add_argument(
        "--asr-backend",
        choices=("whisper", "moss", "voxtral", "grok-stt"),
        help="覆寫所選層級的 ASR backend",
    )
    parser.add_argument(
        "--translation-model",
        metavar="MODEL",
        help="覆寫 OpenRouter 翻譯 model",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high"),
        help="覆寫翻譯 reasoning effort",
    )
    parser.add_argument(
        "--trim-threshold",
        type=float,
        metavar="SECONDS",
        help="啟動對白剪片所需的淨對白秒數，預設 30",
    )
    parser.add_argument(
        "--segment-gap",
        type=float,
        metavar="SECONDS",
        help="對白停頓≥此秒數就剪掉中間空白，預設 1.5",
    )
    parser.add_argument(
        "--asr-chunk-seconds",
        type=int,
        metavar="SECONDS",
        help="240P 串流下載與 ASR 的區段秒數，預設 180",
    )
    parser.add_argument(
        "--asr-batch-size",
        type=int,
        metavar="COUNT",
        help="累計多少個片段才送一次批次 ASR，預設 3；尾批不足仍會送出",
    )
    parser.add_argument(
        "--retry-subtitles",
        action="store_true",
        help="只修復舊字幕，不下載新影片",
    )
    parser.add_argument(
        "--repair-over-1080",
        action="store_true",
        help="只備份並重下載超過等效 1080P 的影片",
    )
    parser.add_argument("--grid", type=Path, help="只處理指定九宮格")
    parser.add_argument(
        "--keep-proxy",
        action="store_true",
        help="搭配 --grid 保留代理工作檔",
    )
    return parser


def run_maintenance(args: argparse.Namespace) -> int | None:
    if args.grid:
        import audio_enhance_stage
        import full_video_pipeline

        ensure_output_directories()
        options = resolve_stage_options(
            "video",
            FeatureSwitches(
                asr=args.asr,
                demucs_asr=args.demucs_asr,
                asr_stream=args.asr_stream,
                subtitles=args.subtitles,
                translation=args.translation,
                dialogue_trim=args.dialogue_trim,
                selective_download=args.selective_download,
                edge_padding=args.edge_padding,
                enhance=args.enhance,
                metadata=args.metadata,
                archive_grid=args.archive,
                keep_work=args.keep_work,
                reuse_cache=args.reuse_cache,
                force=args.force,
                video_height=args.video_height,
                asr_backend=args.asr_backend,
                translation_model=args.translation_model,
                reasoning_effort=args.reasoning_effort,
                trim_threshold=args.trim_threshold,
                segment_gap=args.segment_gap,
                asr_chunk_seconds=args.asr_chunk_seconds,
                asr_batch_size=args.asr_batch_size,
            ),
        )
        try:
            validate_stage_options("video", options)
            with feature_environment(options):
                moss_session = (
                    full_video_pipeline.moss_asr_session()
                    if options.asr and options.asr_backend == "moss"
                    else nullcontext(None)
                )
                enhance_session = (
                    audio_enhance_stage.audio_enhance_session()
                    if options.enhance
                    else nullcontext(None)
                )
                with moss_session as moss_worker, enhance_session as audio_worker:
                    full_video_pipeline.process_full_video_from_grid(
                        args.grid.resolve(),
                        final_dir=VIDEOS_DIR,
                        archive_dir=DOWNLOADED_DIR,
                        keep_proxy=args.keep_proxy or options.keep_work,
                        max_height=options.video_height,
                        enable_enhance=options.enhance,
                        enable_asr=options.asr,
                        export_subtitles=options.subtitles,
                        enable_dialogue_trim=options.dialogue_trim,
                        enable_translation=options.translation,
                        enable_selective_download=options.selective_download,
                        enable_edge_padding=options.edge_padding,
                        enable_metadata=options.metadata,
                        archive_grid_on_done=options.archive_grid,
                        dialogue_trim_threshold=options.trim_threshold,
                        segment_gap=options.segment_gap,
                        force=options.force,
                        moss_worker=moss_worker,
                        audio_worker=audio_worker,
                    )
            return 0
        except Exception as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 1
    if args.retry_subtitles or args.repair_over_1080:
        return maintenance.run_download_process(
            retry_subtitles=args.retry_subtitles,
            repair_over_1080=args.repair_over_1080,
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name in (
        "preview_seconds",
        "video_height",
        "chosen_height",
        "shorts_height",
        "trim_threshold",
        "asr_chunk_seconds",
        "asr_batch_size",
    ):
        value = getattr(args, name)
        if value is not None and value <= 0:
            parser.error(f"--{name.replace('_', '-')} 必須大於 0")
    if args.segment_gap is not None and args.segment_gap < 0:
        parser.error("--segment-gap 不得小於 0")
    if args.interactive and args.list:
        parser.error("--interactive 不能與 --list 同時使用")
    if args.retry_subtitles and args.repair_over_1080:
        parser.error("--retry-subtitles 不能與 --repair-over-1080 同時使用")
    if args.keep_proxy and not args.grid:
        parser.error("--keep-proxy 只能搭配 --grid")
    if args.grid and (args.stages or args.interactive or args.list):
        parser.error("--grid 不能混用 --stages、--interactive 或 --list")
    if args.list and any(
        getattr(args, name) is not None
        for name in (
            "asr",
            "demucs_asr",
            "asr_stream",
            "subtitles",
            "translation",
            "dialogue_trim",
            "selective_download",
            "edge_padding",
            "enhance",
            "metadata",
            "archive",
            "keep_work",
            "reuse_cache",
            "force",
            "preview_seconds",
            "video_height",
            "chosen_height",
            "shorts_height",
            "asr_backend",
            "translation_model",
            "reasoning_effort",
            "trim_threshold",
            "segment_gap",
            "asr_chunk_seconds",
            "asr_batch_size",
        )
    ):
        parser.error("--list 只盤點檔案，不能混用功能開關")
    if (args.retry_subtitles or args.repair_over_1080) and (
        args.grid
        or args.stages
        or args.interactive
        or args.list
        or any(
            getattr(args, name) is not None
            for name in (
                "asr",
                "demucs_asr",
                "asr_stream",
                "subtitles",
                "translation",
                "dialogue_trim",
                "selective_download",
                "edge_padding",
                "enhance",
                "metadata",
                "archive",
                "keep_work",
                "reuse_cache",
                "force",
                "preview_seconds",
                "video_height",
                "chosen_height",
                "shorts_height",
                "asr_backend",
                "translation_model",
                "reasoning_effort",
                "trim_threshold",
                "segment_gap",
                "asr_chunk_seconds",
                "asr_batch_size",
            )
        )
    ):
        parser.error("維護模式不能混用四層流程或功能開關")
    maintenance_exit = run_maintenance(args)
    if maintenance_exit is not None:
        return maintenance_exit

    try:
        stages = parse_stage_names(args.stages)
    except ValueError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
    if args.preview_seconds is not None and "preview" not in stages:
        parser.error("--preview-seconds 只能用於 preview 層")
    if args.video_height is not None and "video" not in stages:
        parser.error("--video-height 只能用於 video 層")
    if args.chosen_height is not None and "chosen" not in stages:
        parser.error("--chosen-height 只能用於 chosen 層")
    if args.shorts_height is not None and "shorts" not in stages:
        parser.error("--shorts-height 只能用於 shorts 層")
    options = FeatureSwitches(
        asr=args.asr,
        demucs_asr=args.demucs_asr,
        asr_stream=args.asr_stream,
        subtitles=args.subtitles,
        translation=args.translation,
        dialogue_trim=args.dialogue_trim,
        selective_download=args.selective_download,
        edge_padding=args.edge_padding,
        enhance=args.enhance,
        metadata=args.metadata,
        archive_grid=args.archive,
        keep_work=args.keep_work,
        reuse_cache=args.reuse_cache,
        force=args.force,
        preview_seconds=args.preview_seconds,
        video_height=args.video_height,
        chosen_height=args.chosen_height,
        shorts_height=args.shorts_height,
        asr_backend=args.asr_backend,
        translation_model=args.translation_model,
        reasoning_effort=args.reasoning_effort,
        trim_threshold=args.trim_threshold,
        segment_gap=args.segment_gap,
        asr_chunk_seconds=args.asr_chunk_seconds,
        asr_batch_size=args.asr_batch_size,
    )
    if args.list:
        for stage in stages:
            print_stage_inventory(stage)
        return 0
    try:
        failures = (
            run_interactive(options, stages)
            if args.interactive
            else run_stages(stages, options)
        )
    except ValueError as exc:
        print(f"設定錯誤：{exc}", file=sys.stderr)
        return 2
    return 3 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

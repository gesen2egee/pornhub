# -*- coding: utf-8 -*-
"""05_chosen 精選管線：九宮格或影片（只取 URL）→ 1080P + MOSS + Grok 4.3 minimal（失敗改 Grok 4.5 minimal）+ enhance → 06_good。

來源清理：
  - 九宮格 JPG → 移到 04_downloaded
  - 影片只提供 URL；處理完成後刪除來源
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from project_paths import (
    CHOSEN_DIR,
    DOWNLOADED_DIR,
    GOOD_DIR,
    ensure_output_directories,
)

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
GRID_EXTS = {".jpg", ".jpeg", ".png"}


def _log(msg: str) -> None:
    print(msg, flush=True)


def _chosen_asr(
    video_url: str,
    work: Path,
    video_stem: str,
    *,
    enable_asr: bool,
    enable_translation: bool,
    enable_dialogue_trim: bool,
    moss_worker=None,
) -> tuple[dict, float]:
    """明確依開關重用或串流 ASR；OpenRouter 留在共同第二階段。"""
    import full_video_pipeline

    cache = work / "asr_result.json"
    reuse = os.getenv("REUSE_ASR_RESULT", "1").strip().casefold() not in {
        "0", "false", "no", "off",
    }
    if reuse and cache.is_file():
        result = json.loads(cache.read_text(encoding="utf-8"))
        if not enable_translation:
            result["translated_srt"] = ""
            result["outcome"] = "transcribed"
        _log(f"  [ASR] 重用字幕快取：{cache.name}")
        duration = float(result.get("source_duration") or 0.0)
        return result, duration or (full_video_pipeline.remote_duration(video_url) or 0.0)
    if not enable_asr:
        if enable_translation or enable_dialogue_trim:
            raise RuntimeError(
                "ASR 已關閉且沒有 Chosen 字幕快取；翻譯與剪片無資料來源。"
            )
        return {
            "original_srt": "",
            "translated_srt": "",
            "outcome": "disabled",
            "cues": [],
        }, full_video_pipeline.remote_duration(video_url) or 0.0
    result, duration = full_video_pipeline.run_streamed_asr(
        video_url, work, video_stem, moss_worker=moss_worker
    )
    result["source_duration"] = duration
    cache.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result, duration


def _chosen_segment_plan(
    asr: dict,
    *,
    enabled: bool,
    threshold: float,
    segment_gap: float,
    max_dur: float,
    edge_padding: float = 0.0,
) -> tuple[list[tuple[float, float]] | None, str, str]:
    """依 Chosen 的 ASR 字幕決定是否需要高畫質分段下載。"""
    import full_video_pipeline as fvp
    import segment_cutter

    original = asr.get("original_srt") or ""
    translated = asr.get("translated_srt") or ""
    if not enabled:
        return None, original, translated

    source_srt = translated.strip() or original
    entries = fvp.srt_text_to_entries(source_srt) if source_srt else []
    if not entries and asr.get("cues"):
        entries = fvp.cues_to_entries(asr["cues"])
    net_duration = segment_cutter.calculate_net_dialogue_duration(entries)
    if net_duration <= threshold or not entries:
        _log("  [剪片] 對白未達門檻，保留全片")
        return None, original, translated

    segments = segment_cutter.build_continuous_segments(
        entries,
        max_gap=segment_gap,
        max_dur=max_dur if max_dur > 0 else 99999.0,
        edge_padding=edge_padding,
    )
    _log(
        f"  [剪片] 停頓≥{segment_gap}s 剪掉；前後延伸={edge_padding}s；"
        f"對白 > {threshold}s → 只下載 {len(segments)} 個高畫質區段"
    )
    return segments, original, translated


def _retime_chosen_subtitles(
    original: str,
    translated: str,
    segments: list[tuple[float, float]],
) -> tuple[str, str]:
    """把 Chosen 字幕重排到高畫質分段串接後的時間軸。"""
    import full_video_pipeline as fvp
    import segment_cutter

    if original.strip():
        original = fvp._entries_to_srt(
            segment_cutter.retime_subtitles(
                fvp.srt_text_to_entries(original), segments
            )
        )
    if translated.strip():
        translated = fvp._entries_to_srt(
            segment_cutter.retime_subtitles(
                fvp.srt_text_to_entries(translated), segments
            )
        )
    return original, translated


def _apply_chosen_env(
    *,
    enable_translation: bool | None = None,
    enable_dialogue_trim: bool | None = None,
    enable_enhance: bool | None = None,
    max_height: int | None = None,
) -> None:
    """精選：MOSS + Grok 4.3 minimal 翻譯（失敗改用 Grok 4.5 minimal）+ 1080P enhance。"""
    os.environ["ASR_BACKEND"] = os.getenv("CHOSEN_ASR_BACKEND", "moss")
    # Chosen 預設使用 Grok 4.3 minimal；第一次請求失敗時由翻譯層改用 Grok 4.5 minimal。
    os.environ["OPENROUTER_MODEL"] = os.getenv(
        "CHOSEN_OPENROUTER_MODEL", "x-ai/grok-4.3"
    )
    os.environ["TRANSLATE_REASONING_EFFORT"] = os.getenv(
        "CHOSEN_TRANSLATE_REASONING", "minimal"
    )
    os.environ["TRANSLATE_FALLBACK_MODEL"] = os.getenv(
        "CHOSEN_TRANSLATE_FALLBACK_MODEL", "x-ai/grok-4.5"
    )
    os.environ["HIGH_VIDEO_HEIGHT"] = os.getenv("CHOSEN_VIDEO_HEIGHT", "1080")
    if enable_translation is not None:
        os.environ["ENABLE_TRANSLATION"] = "1" if enable_translation else "0"
    os.environ["AUDIO_AUTO_ENHANCE"] = (
        "1" if enable_enhance is not False else "0"
    )
    os.environ["ENABLE_DIALOGUE_TRIM"] = (
        "1" if enable_dialogue_trim is not False else "0"
    )
    if max_height is not None:
        os.environ["HIGH_VIDEO_HEIGHT"] = str(max_height)


def list_chosen_items(chosen_dir: Path | None = None) -> list[Path]:
    root = Path(chosen_dir or CHOSEN_DIR)
    if not root.is_dir():
        return []
    items: list[Path] = []
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext in GRID_EXTS or ext in VIDEO_EXTS:
            items.append(path)
    return items


def process_chosen_grid(
    jpg_path: Path,
    *,
    final_dir: Path = GOOD_DIR,
    archive_dir: Path = DOWNLOADED_DIR,
    archive_grid: bool = True,
    keep_work: bool = False,
    enable_asr: bool | None = None,
    export_subtitles: bool | None = None,
    enable_translation: bool | None = None,
    enable_dialogue_trim: bool | None = None,
    enable_selective_download: bool | None = None,
    enable_three_phase_selection: bool | None = None,
    enable_edge_padding: bool | None = None,
    enable_enhance: bool | None = None,
    enable_metadata: bool | None = None,
    max_height: int | None = None,
    dialogue_trim_threshold: float = 30.0,
    segment_gap: float = 1.5,
    force: bool = False,
    moss_worker=None,
    audio_worker=None,
    work_bucket: str = "05_chosen",
) -> Path:
    """九宮格：代理 → MOSS → 1080P（可分段）→ 判斷 enhance → 06_good，JPG→04。"""
    import full_video_pipeline

    _apply_chosen_env(
        enable_translation=enable_translation,
        enable_dialogue_trim=enable_dialogue_trim,
        enable_enhance=enable_enhance,
        max_height=max_height,
    )
    return full_video_pipeline.process_full_video_from_grid(
        jpg_path,
        final_dir=final_dir,
        archive_dir=archive_dir,
        keep_proxy=keep_work,
        max_height=max_height or int(os.environ.get("HIGH_VIDEO_HEIGHT", "1080")),
        enable_enhance=True if enable_enhance is None else enable_enhance,
        enable_asr=enable_asr,
        export_subtitles=export_subtitles,
        enable_dialogue_trim=enable_dialogue_trim,
        enable_translation=enable_translation,
        enable_selective_download=enable_selective_download,
        enable_three_phase_selection=enable_three_phase_selection,
        enable_edge_padding=enable_edge_padding,
        enable_metadata=enable_metadata,
        dialogue_trim_threshold=dialogue_trim_threshold,
        segment_gap=segment_gap,
        force=force,
        work_bucket=work_bucket,
        pipeline_stage="chosen",
        archive_grid_on_done=archive_grid,
        moss_worker=moss_worker,
        audio_worker=audio_worker,
    )


def process_chosen_video(
    video_path: Path,
    *,
    final_dir: Path = GOOD_DIR,
    keep_work: bool = False,
    enable_asr: bool | None = None,
    export_subtitles: bool | None = None,
    enable_translation: bool | None = None,
    enable_dialogue_trim: bool | None = None,
    enable_selective_download: bool | None = None,
    enable_three_phase_selection: bool | None = None,
    enable_edge_padding: bool | None = None,
    enable_enhance: bool | None = None,
    enable_metadata: bool | None = None,
    max_height: int | None = None,
    dialogue_trim_threshold: float = 30.0,
    segment_gap: float = 1.5,
    force: bool = False,
    moss_worker=None,
    audio_worker=None,
    work_bucket: str = "05_chosen",
) -> Path:
    """影片只作為 URL 載體；所有處理都重新走 Chosen 的高畫質管線。

    先用 URL 下載低畫質代理做 ASR/區段規劃，再只下載需要的 1080P
    區段；沒有 URL 時直接報錯，不會改走本機影片剪輯。完成後刪除
    05_chosen 來源影片。
    """
    import full_video_pipeline
    import video_meta

    _apply_chosen_env(
        enable_translation=enable_translation,
        enable_dialogue_trim=enable_dialogue_trim,
        enable_enhance=enable_enhance,
        max_height=max_height,
    )
    asr_enabled = (
        os.getenv("ENABLE_ASR", "1").strip().casefold()
        not in {"0", "false", "no", "off"}
        if enable_asr is None
        else enable_asr
    )
    srt_enabled = (
        os.getenv("EXPORT_SUBTITLES", "1").strip().casefold()
        not in {"0", "false", "no", "off"}
        if export_subtitles is None
        else export_subtitles
    )
    metadata_enabled = (
        os.getenv("ENABLE_METADATA", "1").strip().casefold()
        not in {"0", "false", "no", "off"}
        if enable_metadata is None
        else enable_metadata
    )
    enhance_enabled = True if enable_enhance is None else enable_enhance
    translation_enabled = (
        os.getenv("ENABLE_TRANSLATION", "1").strip().casefold()
        not in {"0", "false", "no", "off"}
        if enable_translation is None
        else enable_translation
    )
    trim_enabled = (
        os.getenv("ENABLE_DIALOGUE_TRIM", "1").strip().casefold()
        not in {"0", "false", "no", "off"}
        if enable_dialogue_trim is None
        else enable_dialogue_trim
    )
    selective_enabled = (
        full_video_pipeline.selective_download_enabled()
        if enable_selective_download is None
        else bool(enable_selective_download)
    )
    three_phase_enabled = (
        full_video_pipeline.three_phase_selection_enabled()
        if enable_three_phase_selection is None
        else bool(enable_three_phase_selection)
    )
    if three_phase_enabled:
        selective_enabled = True
        trim_enabled = True
    if selective_enabled and not translation_enabled:
        _log("  [精選下載] 翻譯已關閉，精選下載自動改為 OFF")
        selective_enabled = False
        three_phase_enabled = False
    edge_pad_on = (
        full_video_pipeline.edge_padding_enabled()
        if enable_edge_padding is None
        else bool(enable_edge_padding)
    )
    edge_pad = full_video_pipeline.resolve_edge_padding_seconds(edge_pad_on)
    os.environ["ENABLE_SELECTIVE_DOWNLOAD"] = "1" if selective_enabled else "0"
    os.environ["ENABLE_EDGE_PADDING"] = "1" if edge_pad_on else "0"
    video_path = Path(video_path).resolve()
    ensure_output_directories()
    final_dir = Path(final_dir)
    final_dir.mkdir(parents=True, exist_ok=True)

    stem = re.sub(r"^\d{4}-", "", video_path.stem)
    final_video = final_dir / f"{stem}.mp4"
    final_srt = final_dir / f"{stem}.srt"

    # 若 metadata 有網址，改走九宮格式完整下載管線（需先寫成臨時流程用 proxy 來源）
    try:
        meta = video_meta.read_mp4_meta(video_path)
        web = meta.get("web_meta") or {}
        url = (
            web.get("webpage_url")
            or web.get("url")
            or meta.get("webpage_url")
        )
    except Exception:
        url = None
        meta = {}

    if not (isinstance(url, str) and url.startswith("http")):
        raise RuntimeError(
            "Chosen 影片只作為 URL 輸入；檔案沒有可用的 webpage_url，"
            "請保留原始 Metadata 或改放含 URL 的九宮格。"
        )

    # 用暫存九宮格流程：直接 full pipeline 需要 jpg；改手動 1080 下載+字幕
    _log(f"  [chosen-video] 只取輸入 URL，先低畫質分析再下載 1080P：{stem}")
    work = (
        Path(full_video_pipeline.TEMP_DIR)
        / "pipeline"
        / work_bucket
        / f"_work_{stem}"
    )
    work.mkdir(parents=True, exist_ok=True)
    cache = work / "asr_result.json"
    chosen_asr = _chosen_asr(
        url,
        work,
        stem,
        enable_asr=asr_enabled,
        enable_translation=translation_enabled,
        enable_dialogue_trim=trim_enabled,
        moss_worker=moss_worker,
    )
    # 保留舊呼叫端只回傳 dict 的相容性；正式新流程回傳 (dict, duration)。
    if isinstance(chosen_asr, tuple):
        asr, source_duration = chosen_asr
    else:
        asr = chosen_asr
        source_duration = float(asr.get("source_duration") or 0.0)
    os.environ["HIGH_VIDEO_HEIGHT"] = str(max_height or 1080)

    selective_done = False
    if selective_enabled and translation_enabled:
        _log("  [精選下載] 先劇情整理 + 選擇性翻譯（歌詞則完整翻譯）…")
        try:
            asr = (
                full_video_pipeline.complete_three_phase_translation(asr, cache)
                if three_phase_enabled
                else full_video_pipeline.complete_cached_translation(
                    asr, cache, selective=True
                )
            )
            selective_done = True
            is_full = bool(asr.get("selective_is_full"))
            _log(
                f"  [精選下載] "
                f"{'完整/歌詞' if is_full else '精選省略'}："
                f"保留 {len(asr.get('selective_kept_ids') or [])} 條"
            )
        except Exception as exc:
            _log(f"  [!] 精選翻譯失敗，回退一般流程：{exc}")
            selective_enabled = False

    # 精選後（或完整）對白淨長判斷 > 門檻再分段；停頓≥1.5s 剪；預設不延伸
    segments, original, translated = _chosen_segment_plan(
        asr,
        enabled=trim_enabled,
        threshold=0.0 if three_phase_enabled else dialogue_trim_threshold,
        segment_gap=segment_gap,
        max_dur=source_duration if source_duration > 0 else 99999.0,
        edge_padding=edge_pad,
    )
    if three_phase_enabled and segments is not None:
        _log("  [三段精選] 保留選段完整起訖；僅音訊 crossfade 0.08 秒")
    high, original, translated, enhanced = full_video_pipeline.run_parallel_delivery_phase(
        url,
        work,
        stem,
        asr,
        segments,
        enable_translation=translation_enabled and not selective_done,
        enable_enhance=enhance_enabled,
        asr_cache=cache,
        audio_worker=audio_worker,
        audio_crossfade_seconds=(0.08 if three_phase_enabled and segments is not None else 0.0),
    )
    if final_video.exists():
        final_video.unlink()
    shutil.move(str(high), str(final_video))
    translated = translated.strip()
    original = original.strip()
    if srt_enabled and translated:
        full_video_pipeline.write_compatible_srt(final_srt, translated)
    elif srt_enabled and original:
        full_video_pipeline.write_compatible_srt(final_srt, original)
    elif not srt_enabled:
        final_srt.unlink(missing_ok=True)
    try:
        if metadata_enabled:
            chosen_web = dict(web) if isinstance(web, dict) else {}
            chosen_web["pipeline_stage"] = "chosen"
            chosen_web["published_stage"] = "chosen"
            video_meta.merge_write_mp4_meta(
                final_video,
                web_meta=chosen_web,
                original_srt=original or None,
                translated_srt=translated or None,
                subtitle_status=video_meta.build_subtitle_status(
                    asr.get("outcome") or "empty",
                    audio_enhanced=enhanced,
                ),
            )
        else:
            _log("  [META] 已由開關停用")
    except Exception as exc:
        _log(f"  [!] meta 失敗：{exc}")
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    # 刪除 05_chosen 來源影片
    try:
        if video_path.exists() and video_path.resolve() != final_video.resolve():
            video_path.unlink()
            _log(f"  [清理] 已刪除 chosen 來源影片：{video_path.name}")
    except OSError as exc:
        _log(f"  [!] 無法刪除來源影片：{exc}")

    # 同名 srt 在 chosen 也清掉
    for side in video_path.parent.glob(video_path.stem + ".*"):
        if side.suffix.lower() in {".srt", ".ass"} and side.exists():
            side.unlink(missing_ok=True)

    _log(f"[DONE] chosen → {final_video}")
    return final_video


def process_chosen_item(path: Path, **options) -> Path:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in GRID_EXTS:
        return process_chosen_grid(path, **options)
    if ext in VIDEO_EXTS:
        options.pop("archive_grid", None)
        options.pop("archive_dir", None)
        return process_chosen_video(path, **options)
    raise ValueError(f"不支援的 chosen 項目：{path.name}")


def process_chosen_directory(
    chosen_dir: Path | None = None,
) -> tuple[int, int]:
    """處理 05_chosen 全部項目。回傳 (成功數, 失敗數)。"""
    ensure_output_directories()
    items = list_chosen_items(chosen_dir)
    if not items:
        _log("[chosen] 05_chosen 沒有九宮格或影片")
        return 0, 0
    _log(
        f"[chosen] 精選管線 1080P + MOSS + Grok 4.3 minimal（失敗改 Grok 4.5 minimal）+ enhance → 06_good，"
        f"共 {len(items)} 項"
    )
    ok = 0
    fail = 0
    for idx, item in enumerate(items, 1):
        _log(f"\n[chosen {idx}/{len(items)}] {item.name}")
        try:
            process_chosen_item(item)
            ok += 1
        except Exception as exc:
            _log(f"  [FAIL] {exc}")
            fail += 1
    _log(f"[chosen] 完成：成功 {ok} | 失敗 {fail}")
    return ok, fail


def process_chosen_items(
    items: list[Path],
    source_workers: int = 1,
    **options,
) -> tuple[int, int]:
    """處理控制器已盤點的 Chosen 項目；可與前片高畫質交疊下一片分析。"""
    ok = 0
    fail = 0
    workers = max(1, min(int(source_workers), 4))

    def process_one(index: int, item: Path) -> Path:
        _log(f"\n[chosen {index}/{len(items)}] {item.name}")
        return process_chosen_item(item, **dict(options))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chosen-source") as executor:
        futures = {
            executor.submit(process_one, index, item): item
            for index, item in enumerate(items, 1)
        }
        for future in as_completed(futures):
            try:
                future.result()
                ok += 1
            except Exception as exc:
                _log(f"  [FAIL] {futures[future].name}：{exc}")
                fail += 1
    return ok, fail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="05_chosen → 06_good 精選管線")
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="chosen 目錄（預設 output/05_chosen）",
    )
    parser.add_argument(
        "--item",
        type=Path,
        default=None,
        help="只處理單一檔案（九宮格或影片）",
    )
    args = parser.parse_args(argv)
    if args.item:
        ensure_output_directories()
        try:
            process_chosen_item(args.item.resolve())
            return 0
        except Exception as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 1
    ok, fail = process_chosen_directory(args.dir)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""02_preview_videos 預覽管線。

每次下載 3 分鐘低畫質 → MOSS ASR；累計對話達 30 秒才剪片，否則繼續取下一段。
若影片結束仍未達門檻則發布完整影片 → Grok 4.3 none 翻譯 → 自動 enhance → 軟 SRT。
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
import segment_cutter
from project_paths import PREVIEW_VIDEOS_DIR, TEMP_DIR, ensure_output_directories

PREVIEW_SECONDS = 180
PREVIEW_TRIM_THRESHOLD = 30.0
SEGMENT_GAP = 1.5


def _log(msg: str) -> None:
    print(msg, flush=True)


def _apply_preview_env() -> None:
    """只補直接呼叫此模組時的預設值，不覆寫控制器傳入設定。"""
    os.environ.setdefault(
        "ASR_BACKEND", os.getenv("PREVIEW_ASR_BACKEND", "moss")
    )
    os.environ.setdefault(
        "OPENROUTER_MODEL",
        os.getenv("PREVIEW_OPENROUTER_MODEL", "x-ai/grok-4.3"),
    )
    os.environ.setdefault(
        "TRANSLATE_REASONING_EFFORT",
        os.getenv("PREVIEW_TRANSLATE_REASONING", "none"),
    )


def cut_local_segments(
    source: Path,
    segments: list[tuple[float, float]],
    out_path: Path,
) -> Path:
    """精準剪出並重建時間軸，避免 H.264 stream copy 造成音畫變速。"""

    if not segments:
        raise RuntimeError("沒有可剪的語音區段")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)
    filters: list[str] = []
    maps: list[str] = []
    for index, (start, end) in enumerate(segments):
        filters.extend(
            [
                (
                    f"[0:v]trim=start={start:.3f}:end={end:.3f},"
                    f"setpts=PTS-STARTPTS[v{index}]"
                ),
                (
                    f"[0:a]atrim=start={start:.3f}:end={end:.3f},"
                    f"asetpts=PTS-STARTPTS[a{index}]"
                ),
            ]
        )
        maps.append(f"[v{index}][a{index}]")
    filters.append(
        f"{''.join(maps)}concat=n={len(segments)}:v=1:a=1[outv][outa]"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[outv]",
        "-map",
        "[outa]",
        "-c:v",
        "libx264",
        "-crf",
        "18",
        "-preset",
        "fast",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"預覽剪片失敗：{(proc.stderr or '')[-500:]}")
    return out_path


def download_preview_range(
    video_url: str,
    out_path: Path,
    start: float,
    end: float,
) -> Path:
    """下載指定範圍的最低畫質預覽片段。"""
    import yt_dlp
    import full_video_pipeline as fvp

    out_path.unlink(missing_ok=True)
    start = max(0.0, float(start))
    end = max(start + 0.05, float(end))

    def _ranges(_info_dict, _ydl):
        yield {"start_time": start, "end_time": end}

    opts = fvp._base_ydl_opts(out_path, "download_low", video_url)
    opts["format"] = (
        "worstvideo[height<=240]+worstaudio/"
        "worst[height<=240]/"
        "worstvideo+worstaudio/worst"
    )
    opts["download_ranges"] = _ranges
    opts["force_keyframes_at_cuts"] = True
    _log(
        f"  [1/4] 下載低畫質預覽 {start:.0f}–{end:.0f}s → {out_path.name}"
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([video_url])
    if not out_path.exists() or fvp.has_video_stream(out_path) is not True:
        raise RuntimeError(f"預覽下載失敗：{out_path}")
    return out_path


def download_preview_clip(video_url: str, out_path: Path) -> Path:
    """相容舊呼叫：下載第一段 PREVIEW_SECONDS 秒低畫質預覽。"""
    seconds = float(os.getenv("PREVIEW_SECONDS", str(PREVIEW_SECONDS)))
    return download_preview_range(video_url, out_path, 0.0, seconds)


def collect_preview_until_dialogue(
    video_url: str,
    work_dir: Path,
    video_stem: str,
    dialogue_threshold: float,
    *,
    moss_worker=None,
) -> tuple[dict, Path, float, bool, bool]:
    """每 3 分鐘取樣與 ASR；達對話門檻即停止，否則取到影片結束。"""
    import full_video_pipeline as fvp
    from translate_srt_openrouter import format_srt

    try:
        chunk_seconds = float(os.getenv("PREVIEW_SECONDS", str(PREVIEW_SECONDS)))
    except ValueError:
        chunk_seconds = float(PREVIEW_SECONDS)
    chunk_seconds = max(1.0, chunk_seconds)
    remote_duration = fvp.remote_duration(video_url)
    range_count = (
        max(1, math.ceil(remote_duration / chunk_seconds))
        if remote_duration is not None
        else 1
    )
    parts: list[Path] = []
    merged_cues: list[dict] = []
    languages: list[str] = []
    threshold_reached = False
    source_ended = False

    for index in range(range_count):
        start = index * chunk_seconds
        end = min(remote_duration, start + chunk_seconds) if remote_duration else start + chunk_seconds
        part = work_dir / f"{video_stem}.preview{index:03d}.mp4"
        download_preview_range(video_url, part, start, end)
        parts.append(part)
        asr_work = work_dir / f"preview-asr-{index:03d}"
        result = (
            fvp.run_asr_batch([part], asr_work)
            if moss_worker is None
            else fvp.run_asr_batch([part], asr_work, moss_worker=moss_worker)
        )[0]
        cues = result.get("cues") or []
        merged_cues.extend(fvp._offset_asr_cues(cues, start, len(merged_cues) + 1))
        language = result.get("language")
        if language and language not in languages:
            languages.append(str(language))
        entries = fvp.cues_to_entries(merged_cues)
        net_dialogue = segment_cutter.calculate_net_dialogue_duration(entries)
        _log(
            f"  [Preview ASR] 第 {index + 1} 段完成；累計對話 {net_dialogue:.1f}s"
            f"（門檻 {dialogue_threshold:.1f}s）"
        )
        if net_dialogue >= dialogue_threshold:
            threshold_reached = True
            break
        if remote_duration is not None and end >= remote_duration - 0.01:
            source_ended = True
            break
        actual_duration = fvp.probe_duration(part) or 0.0
        if actual_duration + 0.5 < chunk_seconds:
            source_ended = True
            break

    if not parts:
        raise RuntimeError("Preview 沒有成功下載任何片段")
    part_duration_total = sum(fvp.probe_duration(part) or 0.0 for part in parts)
    clip_path = work_dir / f"{video_stem}.clip.mp4"
    clip_path.unlink(missing_ok=True)
    fvp.concat_videos(parts, clip_path)
    duration = fvp.probe_duration(clip_path) or 0.0
    for part in parts:
        if part.exists() and part.resolve() != clip_path.resolve():
            part.unlink(missing_ok=True)
    if duration <= 0:
        duration = part_duration_total
    return (
        {
            "language": ",".join(languages) or "multilingual",
            "original_srt": format_srt(merged_cues) if merged_cues else "",
            "translated_srt": "",
            "outcome": "transcribed" if merged_cues else "empty",
            "cues": merged_cues,
            "source_duration": duration,
            "preview_threshold_reached": threshold_reached,
            "preview_source_ended": source_ended,
        },
        clip_path,
        duration,
        threshold_reached,
        source_ended,
    )


def process_preview_from_grid(
    jpg_path: Path,
    final_dir: Path | None = None,
    keep_work: bool = False,
    enable_asr: bool | None = None,
    export_subtitles: bool | None = None,
    enable_dialogue_trim: bool | None = None,
    enable_selective_download: bool | None = None,
    enable_edge_padding: bool | None = None,
    enable_enhance: bool | None = None,
    enable_metadata: bool | None = None,
    archive_grid_on_done: bool = False,
    dialogue_trim_threshold: float = PREVIEW_TRIM_THRESHOLD,
    segment_gap: float = SEGMENT_GAP,
    force: bool = False,
    moss_worker=None,
    audio_worker=None,
) -> Path:
    """單支預覽：3 分鐘低畫質 → MOSS → 精選翻譯 → 語音剪片 → enhance → 軟 SRT。"""
    import full_video_pipeline as fvp
    import video_meta

    _apply_preview_env()
    if enable_asr is None:
        enable_asr = os.getenv(
            "ENABLE_ASR", "1"
        ).strip().casefold() not in {"0", "false", "no", "off"}
    if export_subtitles is None:
        export_subtitles = os.getenv(
            "EXPORT_SUBTITLES", "1"
        ).strip().casefold() not in {"0", "false", "no", "off"}
    if enable_dialogue_trim is None:
        enable_dialogue_trim = os.getenv(
            "ENABLE_DIALOGUE_TRIM", "1"
        ).strip().casefold() not in {"0", "false", "no", "off"}
    if enable_metadata is None:
        enable_metadata = os.getenv(
            "ENABLE_METADATA", "1"
        ).strip().casefold() not in {"0", "false", "no", "off"}
    if enable_enhance is None:
        enable_enhance = os.getenv(
            "AUDIO_AUTO_ENHANCE", "1"
        ).strip().casefold() not in {"0", "false", "no", "off"}
    translation_enabled = os.getenv(
        "ENABLE_TRANSLATION", "1"
    ).strip().casefold() not in {"0", "false", "no", "off"}
    if enable_selective_download is None:
        enable_selective_download = fvp.selective_download_enabled()
    if enable_selective_download and not translation_enabled:
        _log("  [精選] 翻譯已關閉，精選翻譯自動改為 OFF")
        enable_selective_download = False
    if enable_edge_padding is None:
        enable_edge_padding = fvp.edge_padding_enabled()
    edge_pad = fvp.resolve_edge_padding_seconds(enable_edge_padding)
    os.environ["ENABLE_SELECTIVE_DOWNLOAD"] = (
        "1" if enable_selective_download else "0"
    )
    os.environ["ENABLE_EDGE_PADDING"] = "1" if enable_edge_padding else "0"
    ensure_output_directories()
    jpg_path = Path(jpg_path).resolve()
    source_suffix = jpg_path.suffix.casefold()
    source_is_grid = source_suffix in {".jpg", ".jpeg", ".png"}
    final_dir = Path(final_dir or PREVIEW_VIDEOS_DIR).resolve()
    final_dir.mkdir(parents=True, exist_ok=True)

    raw_stem = jpg_path.stem
    # 預覽與九宮格同名（含編號前綴）
    video_stem = raw_stem
    final_video = final_dir / f"{video_stem}.mp4"
    final_srt = final_dir / f"{video_stem}.srt"

    if (
        not force
        and final_video.exists()
        and fvp.has_video_stream(final_video) is True
    ):
        meta = video_meta.read_mp4_meta(final_video)
        status = meta.get("subtitle_status") or {}
        source_meta = meta.get("web_meta") or {}
        same_stage_video = (
            source_suffix in {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
            and source_meta.get("pipeline_stage") == "preview"
        )
        if (
            (source_is_grid or same_stage_video)
            and (
                meta.get("original_srt_present")
                and meta.get("translated_srt_present")
                and status.get("outcome") != "failed"
                and (not export_subtitles or final_srt.exists())
                and (not enable_enhance or status.get("audio_enhanced"))
            )
        ):
            _log(f"[SKIP] 預覽已完成：{final_video.name}")
            return final_video

    video_url = fvp.get_video_url_from_source(jpg_path)
    if not video_url:
        raise RuntimeError(f"來源沒有可用 URL：{jpg_path.name}")

    work_dir = TEMP_DIR / "pipeline" / "02_preview_videos" / f"_work_{video_stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    clip_path = work_dir / f"{video_stem}.clip.mp4"
    cut_path = work_dir / f"{video_stem}.cut.mp4"
    asr_cache = work_dir / "asr_result.json"
    reuse_cache = os.getenv(
        "REUSE_ASR_RESULT", "1"
    ).strip().casefold() not in {"0", "false", "no", "off"}
    if (
        not enable_asr
        and (translation_enabled or enable_dialogue_trim)
        and not (reuse_cache and asr_cache.is_file())
    ):
        raise RuntimeError(
            "ASR 已關閉且沒有 Preview 字幕快取；翻譯與剪片無資料來源。"
        )

    _log("=" * 60)
    _log(f"預覽管線：{video_stem}")
    _log(f"URL：{video_url}")
    _log("=" * 60)

    # 1) 每段 3 分鐘下載與 ASR；對話未達門檻就繼續取下一段。
    cached_asr: dict | None = None
    if reuse_cache and asr_cache.is_file():
        try:
            candidate = json.loads(asr_cache.read_text(encoding="utf-8"))
            if (
                "preview_threshold_reached" in candidate
                and clip_path.exists()
                and fvp.has_video_stream(clip_path) is True
            ):
                cached_asr = candidate
        except Exception:
            cached_asr = None

    if cached_asr is not None:
        asr = cached_asr
        duration = fvp.probe_duration(clip_path) or float(
            asr.get("source_duration") or 0.0
        )
        _log(f"  [1/4] 重用已完成 Preview ASR 快取：{asr_cache.name}")
    elif enable_asr:
        asr, clip_path, duration, threshold_reached, source_ended = (
            collect_preview_until_dialogue(
                video_url,
                work_dir,
                video_stem,
                dialogue_trim_threshold,
                moss_worker=moss_worker,
            )
        )
        _log(
            f"  [OK] Preview 取樣時長 {duration:.1f}s；"
            f"對話門檻={'已達' if threshold_reached else '未達'}；"
            f"影片結束={'是' if source_ended else '否'}"
        )
        asr_cache.write_text(
            json.dumps(asr, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        if translation_enabled or enable_dialogue_trim:
            raise RuntimeError(
                "ASR 已關閉且沒有可用 Preview 字幕快取；翻譯與剪片無資料來源。"
            )
        download_preview_clip(video_url, clip_path)
        duration = fvp.probe_duration(clip_path) or 0.0
        _log("  [2/4] ASR 已由開關停用")
        asr = {
            "original_srt": "",
            "translated_srt": "",
            "outcome": "disabled",
            "cues": [],
            "preview_threshold_reached": False,
            "preview_source_ended": False,
        }

    # 所有 Preview ASR 完成後再翻譯；預設精選翻譯，以保留對白淨長判斷 >30s。
    if not translation_enabled:
        asr["translated_srt"] = ""
        if asr.get("outcome") != "disabled":
            asr["outcome"] = "transcribed"
    elif asr.get("original_srt") or asr.get("cues"):
        if enable_selective_download:
            _log("  [2/4] 精選翻譯（劇情 + 選擇性譯文；歌詞則完整）…")
            try:
                asr = fvp.complete_cached_translation(
                    asr, asr_cache, selective=True
                )
                is_full = bool(asr.get("selective_is_full"))
                _log(
                    f"  [精選] {'完整/歌詞' if is_full else '精選省略'}："
                    f"保留 {len(asr.get('selective_kept_ids') or [])} 條"
                )
            except Exception as exc:
                _log(f"  [!] 精選翻譯失敗，回退一般翻譯：{exc}")
                asr = fvp.complete_cached_translation(
                    asr, asr_cache, selective=False
                )
        else:
            asr = fvp.complete_cached_translation(
                asr, asr_cache, selective=False
            )
    original_srt = asr.get("original_srt") or ""
    translated_srt = asr.get("translated_srt") or ""
    outcome = asr.get("outcome") or "empty"

    srt_for_segments = translated_srt.strip() or original_srt
    entries = (
        fvp.srt_text_to_entries(srt_for_segments) if srt_for_segments else []
    )
    if not entries and asr.get("cues"):
        entries = fvp.cues_to_entries(asr["cues"])

    # 精選後的保留對白淨長（歌詞完整時=全句）用來判斷是否 > 門檻
    net_dur = segment_cutter.calculate_net_dialogue_duration(entries)
    duration_source = (
        "精選對白"
        if (
            enable_selective_download
            and asr.get("selective_kept_ids") is not None
            and not bool(asr.get("selective_is_full"))
        )
        else (
            "完整/歌詞對白"
            if (
                enable_selective_download
                and bool(asr.get("selective_is_full"))
            )
            else "ASR/譯文對白"
        )
    )
    _log(
        f"  [2/4] {duration_source}淨長 {net_dur:.1f}s"
        f"（門檻 {dialogue_trim_threshold}s）"
    )

    # 3) 只有精選（或完整）對白淨長 > 門檻才剪片
    publish_src = clip_path
    segments: list[tuple[float, float]] | None = None
    if enable_dialogue_trim and net_dur > dialogue_trim_threshold and entries:
        segments = segment_cutter.build_continuous_segments(
            entries,
            max_gap=segment_gap,
            max_dur=duration if duration > 0 else 99999.0,
            edge_padding=edge_pad,
        )
        trimmed = sum(e - s for s, e in segments)
        _log(
            f"  [3/4] 停頓≥{segment_gap}s 剪掉；前後備援={edge_pad}s；"
            f"{duration_source} {net_dur:.1f}s "
            f"> {dialogue_trim_threshold}s → 剪成 {len(segments)} 段"
            f"（約 {trimmed:.1f}s）"
        )
        cut_local_segments(clip_path, segments, cut_path)
        publish_src = cut_path
        # retime 字幕
        if original_srt.strip():
            orig_e = fvp.srt_text_to_entries(original_srt)
            original_srt = fvp._entries_to_srt(
                segment_cutter.retime_subtitles(orig_e, segments)
            )
        if translated_srt.strip():
            tr_e = fvp.srt_text_to_entries(translated_srt)
            translated_srt = fvp._entries_to_srt(
                segment_cutter.retime_subtitles(tr_e, segments)
            )
    else:
        if asr.get("preview_source_ended") and entries:
            _log(
                f"  [3/4] 影片已結束且 {duration_source} "
                f"{net_dur:.1f}s ≤ {dialogue_trim_threshold}s"
                " → 發布完整影片（不剪）"
            )
        else:
            _log(
                f"  [3/4] 剪片關閉、{duration_source} "
                f"未達 {dialogue_trim_threshold}s 或無字幕"
                " → 保留目前完整 Preview（不剪）"
            )

    enhanced = False
    if enable_enhance:
        _log("  [增強] Preview 音訊增強已開啟")
        publish_src, enhanced = (
            fvp.enhance_full_video(publish_src)
            if audio_worker is None
            else fvp.enhance_full_video(publish_src, audio_worker)
        )

    # 4) 發布 + 軟 SRT
    _log(f"  [4/4] 發布 → {final_video.name} + .srt")
    if final_video.exists():
        final_video.unlink()
    shutil.move(str(publish_src), str(final_video))

    if export_subtitles and translated_srt.strip():
        fvp.write_compatible_srt(final_srt, translated_srt)
    elif export_subtitles and original_srt.strip():
        fvp.write_compatible_srt(final_srt, original_srt)
    else:
        final_srt.unlink(missing_ok=True)

    try:
        import sites

        if enable_metadata:
            adapter = sites.get_adapter_for_url(video_url)
            resolved = sites.resolve_playable(
                adapter, video_url, purpose="info", prefer_lowest=True
            )
            info = dict(resolved.get("info") or {})
            info.setdefault("webpage_url", video_url)
            web_meta = video_meta.build_web_meta(info)
            web_meta = dict(web_meta)
            web_meta["pipeline_stage"] = "preview"
            web_meta["published_stage"] = "preview"
            if segments is not None:
                web_meta["preview_trimmed_segments"] = [
                    [round(s, 3), round(e, 3)] for s, e in segments
                ]
                web_meta["preview_net_speech_seconds"] = round(net_dur, 3)
            video_meta.merge_write_mp4_meta(
                final_video,
                web_meta=web_meta,
                original_srt=original_srt or None,
                translated_srt=translated_srt or None,
                subtitle_status=video_meta.build_subtitle_status(
                    outcome,
                    audio_enhanced=enhanced,
                ),
            )
            if source_is_grid and jpg_path.exists():
                video_meta.write_grid_jpg_web_meta(
                    str(jpg_path), web_meta, url=video_url
                )
        else:
            _log("  [META] 已由開關停用")
    except Exception as exc:
        _log(f"  [!] Meta 寫入失敗：{exc}")

    if archive_grid_on_done and source_is_grid and jpg_path.exists():
        from project_paths import DOWNLOADED_DIR

        fvp.archive_grid(jpg_path, DOWNLOADED_DIR)

    if not keep_work:
        shutil.rmtree(work_dir, ignore_errors=True)

    _log(f"[DONE] 預覽 {final_video}")
    return final_video


def process_preview_directory(preview_dir: Path | None = None) -> int:
    """處理 02_preview_videos 全部九宮格。回傳失敗數。"""
    import glob

    root = Path(preview_dir or PREVIEW_VIDEOS_DIR)
    jpgs = sorted(glob.glob(str(root / "*.jpg")))
    if not jpgs:
        return 0
    _log(
        f"[+] 預覽管線 [{root}/] 共 {len(jpgs)} 張"
        f"（前 {PREVIEW_SECONDS}s 低畫質 → MOSS → 語音剪片 → enhance → 軟 SRT）"
    )
    fail = 0
    for idx, jpg in enumerate(jpgs, 1):
        _log(f"\n[preview {idx}/{len(jpgs)}] {Path(jpg).name}")
        try:
            process_preview_from_grid(Path(jpg), final_dir=root)
        except Exception as exc:
            _log(f"  [FAIL] {exc}")
            fail += 1
    return fail

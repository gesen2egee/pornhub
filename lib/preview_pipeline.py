# -*- coding: utf-8 -*-
"""02_preview_videos 預覽管線。

下載前 3 分鐘低畫質 → Whisper ASR → Grok 4.3 none 翻譯
→ 有語音段落剪片（淨語音 ≤30s 則保留整段 3 分鐘）
→ 軟 SRT（不硬字幕、不 enhance）
"""

from __future__ import annotations

import json
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
        "ASR_BACKEND", os.getenv("PREVIEW_ASR_BACKEND", "whisper")
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
    """從本機影片依區段剪出並拼接。"""
    import full_video_pipeline as fvp

    if not segments:
        raise RuntimeError("沒有可剪的語音區段")
    if len(segments) == 1:
        s, e = segments[0]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{s:.3f}",
            "-to",
            f"{e:.3f}",
            "-i",
            str(source),
            "-c",
            "copy",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0 or not out_path.exists():
            # copy 失敗改 re-encode
            cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                f"{s:.3f}",
                "-to",
                f"{e:.3f}",
                "-i",
                str(source),
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(out_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if proc.returncode != 0 or not out_path.exists():
                raise RuntimeError(f"預覽剪片失敗：{proc.stderr[-400:]}")
        return out_path

    parts: list[Path] = []
    work = out_path.parent
    for i, (s, e) in enumerate(segments):
        part = work / f"{out_path.stem}.p{i:03d}{out_path.suffix}"
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{s:.3f}",
            "-to",
            f"{e:.3f}",
            "-i",
            str(source),
            "-c",
            "copy",
            str(part),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0 or not part.exists():
            cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                f"{s:.3f}",
                "-to",
                f"{e:.3f}",
                "-i",
                str(source),
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(part),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if proc.returncode != 0 or not part.exists():
                raise RuntimeError(
                    f"預覽分段 {i} 剪失敗：{(proc.stderr or '')[-300:]}"
                )
        parts.append(part)
    fvp.concat_videos(parts, out_path)
    for part in parts:
        part.unlink(missing_ok=True)
    return out_path


def download_preview_clip(video_url: str, out_path: Path) -> Path:
    """下載前 PREVIEW_SECONDS 秒、最低畫質。"""
    import yt_dlp
    import full_video_pipeline as fvp

    if out_path.exists():
        out_path.unlink()
    end = float(os.getenv("PREVIEW_SECONDS", str(PREVIEW_SECONDS)))

    def _ranges(info_dict, ydl):
        dur = info_dict.get("duration")
        try:
            dur_f = float(dur) if dur is not None else end
        except (TypeError, ValueError):
            dur_f = end
        clip_end = min(end, max(1.0, dur_f))
        ydl.to_screen(f"[info] PREVIEW 取樣：0–{clip_end:.1f}s")
        yield {"start_time": 0.0, "end_time": clip_end}

    opts = fvp._base_ydl_opts(out_path, "download_low", video_url)
    opts["format"] = (
        "worstvideo[height<=240]+worstaudio/"
        "worst[height<=240]/"
        "worstvideo+worstaudio/worst"
    )
    opts["download_ranges"] = _ranges
    opts["force_keyframes_at_cuts"] = True
    _log(f"  [1/4] 下載前 {end:.0f}s 低畫質預覽 → {out_path.name}")
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([video_url])
    if not out_path.exists() or fvp.has_video_stream(out_path) is not True:
        raise RuntimeError(f"預覽下載失敗：{out_path}")
    return out_path


def process_preview_from_grid(
    jpg_path: Path,
    final_dir: Path | None = None,
    keep_work: bool = False,
    enable_asr: bool | None = None,
    export_subtitles: bool | None = None,
    enable_dialogue_trim: bool | None = None,
    enable_enhance: bool | None = None,
    enable_metadata: bool | None = None,
    archive_grid_on_done: bool = False,
    dialogue_trim_threshold: float = PREVIEW_TRIM_THRESHOLD,
    segment_gap: float = SEGMENT_GAP,
    force: bool = False,
) -> Path:
    """單支預覽：3 分鐘低畫質 → MOSS → 語音剪片 → 自動 enhance → 軟 SRT。"""
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

    # 1) 下載 3 分鐘
    if clip_path.exists() and fvp.has_video_stream(clip_path) is True:
        _log(f"  [1/4] 重用既有預覽片段：{clip_path.name}")
    else:
        download_preview_clip(video_url, clip_path)
    duration = fvp.probe_duration(clip_path) or 0.0
    _log(f"  [OK] 預覽片段時長 {duration:.1f}s")

    # 2) ASR + 翻譯，可依開關重用快取
    if reuse_cache and asr_cache.is_file():
        asr = json.loads(asr_cache.read_text(encoding="utf-8"))
        _log(f"  [2/4] 重用字幕快取：{asr_cache.name}")
        if not translation_enabled:
            asr["translated_srt"] = ""
            asr["outcome"] = "transcribed"
        else:
            asr = fvp.complete_cached_translation(asr, asr_cache)
    elif enable_asr:
        asr = fvp.run_asr_translate(clip_path, work_dir)
        asr_cache.write_text(
            json.dumps(asr, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        if translation_enabled or enable_dialogue_trim:
            raise RuntimeError(
                "ASR 已關閉且沒有 Preview 字幕快取；翻譯與剪片無資料來源。"
            )
        _log("  [2/4] ASR 已由開關停用")
        asr = {
            "original_srt": "",
            "translated_srt": "",
            "outcome": "disabled",
            "cues": [],
        }
    original_srt = asr.get("original_srt") or ""
    translated_srt = asr.get("translated_srt") or ""
    outcome = asr.get("outcome") or "empty"

    srt_for_segments = translated_srt.strip() or original_srt
    entries = (
        fvp.srt_text_to_entries(srt_for_segments) if srt_for_segments else []
    )
    if not entries and asr.get("cues"):
        entries = fvp.cues_to_entries(asr["cues"])

    net_dur = segment_cutter.calculate_net_dialogue_duration(entries)
    _log(
        f"  [2/4] 語音淨長 {net_dur:.1f}s"
        f"（門檻 {dialogue_trim_threshold}s）"
    )

    # 3) 剪片：語音 >30s 才剪；否則保留全部 3 分鐘
    publish_src = clip_path
    segments: list[tuple[float, float]] | None = None
    if enable_dialogue_trim and net_dur > dialogue_trim_threshold and entries:
        segments = segment_cutter.build_continuous_segments(
            entries,
            max_gap=segment_gap,
            max_dur=duration if duration > 0 else 99999.0,
        )
        trimmed = sum(e - s for s, e in segments)
        _log(
            f"  [3/4] 語音充足 → 剪成 {len(segments)} 段"
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
        _log(
            f"  [3/4] 剪片關閉、語音 ≤ {dialogue_trim_threshold}s 或無字幕"
            f" → 保留整段預覽（不剪）"
        )

    enhanced = False
    if enable_enhance:
        _log("  [增強] Preview 音訊增強已開啟")
        publish_src, enhanced = fvp.enhance_full_video(publish_src)

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

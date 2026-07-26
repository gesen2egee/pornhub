# -*- coding: utf-8 -*-
"""對單支九宮格跑正式片管線，並輸出階段時間 + CPU/GPU/磁碟用量報告。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from full_video_pipeline import process_full_video_from_grid, set_pipeline_metrics
from pipeline_metrics import PipelineMetrics, print_stage_table
from project_paths import TASKS_DIR, ensure_output_directories


def _probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.check_output(
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
            text=True,
            timeout=30,
        ).strip()
        return float(out)
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grid", type=Path, required=True)
    parser.add_argument("--keep-proxy", action="store_true")
    parser.add_argument(
        "--sample-interval",
        type=float,
        default=2.0,
        help="資源取樣間隔秒數",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="metrics JSON 輸出路徑",
    )
    args = parser.parse_args()

    ensure_output_directories()
    grid = args.grid.resolve()
    if not grid.is_file():
        print(f"[FAIL] 找不到九宮格：{grid}", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = args.report or (
        TASKS_DIR / "benchmarks" / f"pipeline-{grid.stem[:40]}-{stamp}.json"
    )

    os.environ.setdefault("ASR_BACKEND", "moss")
    # 新片預設重跑 ASR；若已有 asr_result 仍可 REUSE_ASR_RESULT=1
    os.environ.setdefault("REUSE_ASR_RESULT", "0")
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    metrics = PipelineMetrics(sample_interval=args.sample_interval)
    set_pipeline_metrics(metrics)
    metrics.start_sampler()
    final = None
    err = None
    try:
        print(
            f"[BENCH] grid={grid}\n"
            f"[BENCH] ASR_BACKEND={os.getenv('ASR_BACKEND')} "
            f"REUSE_ASR_RESULT={os.getenv('REUSE_ASR_RESULT')}",
            flush=True,
        )
        final = process_full_video_from_grid(
            grid,
            keep_proxy=args.keep_proxy,
        )
    except Exception as exc:
        err = str(exc)
        print(f"[FAIL] {exc}", file=sys.stderr)
    finally:
        metrics.stop_sampler()
        set_pipeline_metrics(None)

    extra = {
        "grid": str(grid),
        "error": err,
        "final_video": str(final) if final else None,
        "asr_backend": os.getenv("ASR_BACKEND"),
        "reuse_asr": os.getenv("REUSE_ASR_RESULT"),
    }
    if final and Path(final).is_file():
        extra["final_duration_sec"] = _probe_duration(Path(final))
        extra["final_size_mb"] = round(Path(final).stat().st_size / (1024 * 1024), 2)
        # proxy duration if kept
        work = (
            Path(os.getenv("PORN_OUTPUT_DIR", "output"))
            if False
            else None
        )
        # locate proxy under temp work dir
        from project_paths import TEMP_DIR

        stem = final.stem
        proxy = (
            TEMP_DIR
            / "pipeline"
            / "03_videos"
            / f"_work_{stem}"
            / f"{stem}.proxy.mp4"
        )
        if proxy.is_file():
            extra["proxy_duration_sec"] = _probe_duration(proxy)
            if extra.get("proxy_duration_sec") and extra.get(
                "final_duration_sec"
            ):
                saved = (
                    float(extra["proxy_duration_sec"])
                    - float(extra["final_duration_sec"])
                )
                extra["saved_duration_sec"] = round(saved, 2)

    report = metrics.report(
        title=f"pipeline:{grid.name}",
        output_path=report_path,
        extra=extra,
    )
    print_stage_table(report)
    if extra.get("saved_duration_sec") is not None:
        print(
            f"\n片長：源 {extra.get('proxy_duration_sec')}s → "
            f"成品 {extra.get('final_duration_sec')}s，"
            f"少 {extra['saved_duration_sec']}s",
            flush=True,
        )
    print(f"\n報告：{report_path}", flush=True)
    return 1 if err else 0


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())

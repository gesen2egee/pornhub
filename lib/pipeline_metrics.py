# -*- coding: utf-8 -*-
"""管線階段計時 + CPU/GPU/磁碟取樣。"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


@dataclass
class StageRecord:
    name: str
    started_at: str
    ended_at: str | None = None
    duration_sec: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Sample:
    t: float
    wall: str
    cpu_percent: float | None
    ram_used_mb: float | None
    ram_total_mb: float | None
    gpu_util: float | None
    gpu_mem_used_mb: float | None
    gpu_mem_total_mb: float | None
    gpu_power_w: float | None
    disk_read_mb: float | None = None
    disk_write_mb: float | None = None


class PipelineMetrics:
    def __init__(self, sample_interval: float = 2.0) -> None:
        self.sample_interval = max(0.5, sample_interval)
        self.stages: list[StageRecord] = []
        self.samples: list[Sample] = []
        self.t0 = time.perf_counter()
        self.wall0 = datetime.now().astimezone().isoformat(timespec="seconds")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._psutil = None
        self._proc = None
        self._io0 = None
        try:
            import psutil  # type: ignore

            self._psutil = psutil
            self._proc = psutil.Process(os.getpid())
            self._proc.cpu_percent(None)
            self._io0 = psutil.disk_io_counters()
        except Exception:
            self._psutil = None

    def start_sampler(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._sample_loop,
            name="pipeline-metrics-sampler",
            daemon=True,
        )
        self._thread.start()

    def stop_sampler(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _sample_loop(self) -> None:
        while not self._stop.wait(self.sample_interval):
            self.samples.append(self._take_sample())

    def _take_sample(self) -> Sample:
        cpu = ram_u = ram_t = None
        disk_r = disk_w = None
        if self._psutil is not None:
            try:
                cpu = float(self._psutil.cpu_percent(interval=None))
                vm = self._psutil.virtual_memory()
                ram_u = round(vm.used / (1024 * 1024), 1)
                ram_t = round(vm.total / (1024 * 1024), 1)
                io = self._psutil.disk_io_counters()
                if io and self._io0:
                    disk_r = round(
                        (io.read_bytes - self._io0.read_bytes) / (1024 * 1024),
                        1,
                    )
                    disk_w = round(
                        (io.write_bytes - self._io0.write_bytes)
                        / (1024 * 1024),
                        1,
                    )
            except Exception:
                pass
        gpu_u = gpu_mu = gpu_mt = gpu_p = None
        try:
            out = subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,memory.total,power.draw",
                    "--format=csv,noheader,nounits",
                ],
                text=True,
                timeout=3,
            ).strip()
            if out:
                parts = [p.strip() for p in out.split(",")]
                if len(parts) >= 4:
                    gpu_u = float(parts[0])
                    gpu_mu = float(parts[1])
                    gpu_mt = float(parts[2])
                    try:
                        gpu_p = float(parts[3])
                    except ValueError:
                        gpu_p = None
        except Exception:
            pass
        return Sample(
            t=round(time.perf_counter() - self.t0, 3),
            wall=datetime.now().astimezone().isoformat(timespec="seconds"),
            cpu_percent=cpu,
            ram_used_mb=ram_u,
            ram_total_mb=ram_t,
            gpu_util=gpu_u,
            gpu_mem_used_mb=gpu_mu,
            gpu_mem_total_mb=gpu_mt,
            gpu_power_w=gpu_p,
            disk_read_mb=disk_r,
            disk_write_mb=disk_w,
        )

    @contextmanager
    def stage(self, name: str, **extra: Any) -> Iterator[StageRecord]:
        rec = StageRecord(
            name=name,
            started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            extra=dict(extra),
        )
        t0 = time.perf_counter()
        print(f"  [METRICS] ▶ {name}", flush=True)
        try:
            yield rec
        finally:
            rec.duration_sec = round(time.perf_counter() - t0, 3)
            rec.ended_at = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            self.stages.append(rec)
            print(
                f"  [METRICS] ■ {name}  {rec.duration_sec:.2f}s",
                flush=True,
            )

    def summarize_resources(self) -> dict[str, Any]:
        if not self.samples:
            return {}
        def series(attr: str) -> list[float]:
            vals = []
            for s in self.samples:
                v = getattr(s, attr)
                if v is not None:
                    vals.append(float(v))
            return vals

        def stats(vals: list[float]) -> dict[str, float] | None:
            if not vals:
                return None
            return {
                "min": round(min(vals), 2),
                "max": round(max(vals), 2),
                "avg": round(sum(vals) / len(vals), 2),
                "last": round(vals[-1], 2),
            }

        return {
            "sample_count": len(self.samples),
            "sample_interval_sec": self.sample_interval,
            "cpu_percent": stats(series("cpu_percent")),
            "ram_used_mb": stats(series("ram_used_mb")),
            "gpu_util_percent": stats(series("gpu_util")),
            "gpu_mem_used_mb": stats(series("gpu_mem_used_mb")),
            "gpu_power_w": stats(series("gpu_power_w")),
            "disk_read_mb_cum": stats(series("disk_read_mb")),
            "disk_write_mb_cum": stats(series("disk_write_mb")),
        }

    def report(
        self,
        *,
        title: str,
        output_path: Path,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        total = round(time.perf_counter() - self.t0, 3)
        payload = {
            "title": title,
            "started_at": self.wall0,
            "ended_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "total_wall_sec": total,
            "stages": [asdict(s) for s in self.stages],
            "resources": self.summarize_resources(),
            "samples": [asdict(s) for s in self.samples],
            "extra": extra or {},
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  [METRICS] 報告已寫入：{output_path}", flush=True)
        return payload


def print_stage_table(report: dict[str, Any]) -> None:
    stages = report.get("stages") or []
    total = float(report.get("total_wall_sec") or 0)
    print("\n========== 階段耗時 ==========", flush=True)
    for s in stages:
        dur = float(s.get("duration_sec") or 0)
        pct = (dur / total * 100) if total > 0 else 0
        print(
            f"  {s['name']:<28} {dur:8.2f}s  ({pct:5.1f}%)",
            flush=True,
        )
    print(f"  {'TOTAL':<28} {total:8.2f}s", flush=True)
    res = report.get("resources") or {}
    print("\n========== 設備用量（取樣） ==========", flush=True)
    for key in (
        "cpu_percent",
        "ram_used_mb",
        "gpu_util_percent",
        "gpu_mem_used_mb",
        "gpu_power_w",
        "disk_read_mb_cum",
        "disk_write_mb_cum",
    ):
        st = res.get(key)
        if not st:
            print(f"  {key}: n/a", flush=True)
            continue
        print(
            f"  {key}: min={st['min']} avg={st['avg']} max={st['max']}",
            flush=True,
        )

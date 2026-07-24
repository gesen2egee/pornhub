"""下載流程使用的長駐字幕工作者。"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import run_subtitle
from asr_backends import create_backend
from audio_enhance_stage import (
    ENHANCE_MARKER,
    auto_enhance_enabled,
    prepare_audio_media,
)
from translate_srt_openrouter import DEFAULT_MODEL

try:
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
except Exception:
    pass


RESULT_MARKER = "__SUBTITLE_JOB_RESULT__"
DEFAULT_LOW_JOB_TIMEOUT = 15 * 60
DEFAULT_JOB_TIMEOUT = 2 * 60 * 60
DEFAULT_HIGH_MAX_TOKENS = 8192


def _positive_timeout(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(1, value)


def subtitle_job_timeout(job: dict[str, Any]) -> int:
    if job.get("is_low_quality"):
        return _positive_timeout(
            "SUBTITLE_LOW_JOB_TIMEOUT_SECONDS",
            DEFAULT_LOW_JOB_TIMEOUT,
        )
    return _positive_timeout(
        "SUBTITLE_JOB_TIMEOUT_SECONDS",
        DEFAULT_JOB_TIMEOUT,
    )


def archive_grid(grid: Path, archive_dir: Path) -> Path | None:
    """整支影片完成後才歸檔九宮格，並避免同名檔互相覆蓋。"""
    if not grid.exists():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_dir / grid.name
    if destination.exists():
        destination = archive_dir / (
            f"{grid.stem}-{grid.parent.name}{grid.suffix}"
        )
    counter = 2
    while destination.exists():
        destination = archive_dir / (
            f"{grid.stem}-{grid.parent.name}-{counter}{grid.suffix}"
        )
        counter += 1
    shutil.move(str(grid), str(destination))
    print(f"  [歸檔] 完整流程成功，九宮格已移至：{destination}", flush=True)
    return destination


def finalize_video(video: Path, final_video: Path) -> Path:
    """字幕與 Meta 完整後，才把暫存影片移入正式資料夾。"""
    if video == final_video:
        return final_video
    final_video.parent.mkdir(parents=True, exist_ok=True)
    if final_video.exists():
        raise RuntimeError(f"正式影片路徑已存在，拒絕覆寫：{final_video}")
    shutil.move(str(video), str(final_video))
    print(f"  [完成] 影片已移至正式路徑：{final_video}", flush=True)
    return final_video


def finish_grid(
    grid: Path,
    archive_dir: Path,
    should_archive: bool,
) -> None:
    if should_archive:
        archive_grid(grid, archive_dir)
    elif grid.exists():
        print(f"  [保留] 九宮格保留原位：{grid}", flush=True)


class SubtitleRuntime:
    """重用同一個 MOSS backend，逐支處理下載完成的影片。"""

    def __init__(self) -> None:
        self.backend = None
        self.api_key = (
            os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
        )
        self.model_name = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        self.use_audio_enhance = auto_enhance_enabled()
        self.configured_max_tokens = os.getenv("MOSS_MAX_NEW_TOKENS")

    def _load_backend(self):
        if self.backend is None:
            self.backend = create_backend().load()
            print("[字幕管線] MOSS 已載入，後續影片共用同一模型", flush=True)
        return self.backend

    @staticmethod
    def _finalize_failed_subtitle(
        video: Path,
        media,
        original_meta: dict[str, Any],
        error: Exception,
    ) -> None:
        """字幕失敗仍產出原片；若已增強則產出增強片並寫明狀態。"""
        enhanced_used = bool(
            media
            and media.enhanced
            and media.media_input.exists()
        )
        base_comment = original_meta.get("raw_comment") or ""
        if enhanced_used and ENHANCE_MARKER not in base_comment:
            base_comment = f"{ENHANCE_MARKER}\n{base_comment}".rstrip()
        if enhanced_used:
            os.replace(media.media_input, video)
            media.media_input = media.source
            print("  [保留] 字幕失敗，改採已完成的音訊增強影片", flush=True)
        try:
            run_subtitle.video_meta.merge_write_mp4_meta(
                video,
                web_meta=original_meta.get("web_meta"),
                original_srt="",
                translated_srt="",
                subtitle_status=(
                    run_subtitle.video_meta.build_subtitle_status(
                        "failed",
                        audio_enhanced=enhanced_used,
                        error=str(error),
                    )
                ),
                base_comment=base_comment,
            )
        except Exception as exc:
            print(f"  [!] 字幕失敗狀態 Meta 寫入失敗：{exc}", flush=True)

    def process(self, job: dict[str, Any]) -> None:
        video = Path(job["video"]).resolve()
        final_video = Path(job["final_video"]).resolve()
        grid = Path(job["grid"]).resolve()
        archive_dir = Path(job["archive_dir"]).resolve()
        should_archive_grid = bool(job.get("archive_grid"))
        if job.get("is_low_quality") and self.configured_max_tokens is None:
            os.environ["MOSS_MAX_NEW_TOKENS"] = "1024"
        elif self.configured_max_tokens is None:
            os.environ["MOSS_MAX_NEW_TOKENS"] = str(
                DEFAULT_HIGH_MAX_TOKENS
            )
        else:
            os.environ["MOSS_MAX_NEW_TOKENS"] = self.configured_max_tokens
        print(f"\n[字幕管線] 接手：{video.name}", flush=True)

        if run_subtitle._subtitle_complete(video):
            print("[字幕管線] 已有舊 SRT 或影片字幕 Meta，直接略過字幕", flush=True)
            finalize_video(video, final_video)
            finish_grid(grid, archive_dir, should_archive_grid)
            return
        legacy_srt = run_subtitle._subtitle_path(video)
        if not legacy_srt.exists() and not self.api_key:
            raise RuntimeError("找不到 OPENROUTER_API_KEY 環境變數。")

        prepared = {}
        media = None
        original_meta = run_subtitle._read_video_meta(video)
        try:
            if self.use_audio_enhance:
                print("[字幕管線] 自動判斷音訊是否需要增強", flush=True)
                prepared = prepare_audio_media([video])
                media = prepared.get(video)
            backend = None if legacy_srt.exists() else self._load_backend()
            run_subtitle.process_video(
                video,
                backend,
                self.api_key,
                self.model_name,
                False,
                media_input=media.media_input if media else video,
                audio_enhanced=media.enhanced if media else False,
            )
            subtitle_meta = run_subtitle._read_video_meta(video)
            if not (
                subtitle_meta.get("original_srt_present")
                and subtitle_meta.get("translated_srt_present")
            ):
                raise RuntimeError("字幕流程結束但影片內沒有完整雙字幕 Meta。")
        except Exception as exc:
            print(
                f"  [保留] 字幕處理失敗，影片仍視為流程完成：{exc}",
                flush=True,
            )
            self._finalize_failed_subtitle(
                video,
                media,
                original_meta,
                exc,
            )
        finally:
            for item in prepared.values():
                item.cleanup()

        finalize_video(video, final_video)
        finish_grid(grid, archive_dir, should_archive_grid)


def runtime_main() -> int:
    """實際執行字幕工作的常駐子程序；每支完成後回報 supervisor。"""
    runtime: SubtitleRuntime | None = None
    for raw_line in sys.stdin:
        job: dict[str, Any] = {}
        if not raw_line.strip():
            continue
        try:
            job = json.loads(raw_line.lstrip("\ufeff"))
            if runtime is None:
                runtime = SubtitleRuntime()
            runtime.process(job)
            result = {"ok": True}
        except Exception as exc:
            name = job.get("video", "未知影片")
            print(f"[字幕管線失敗] {name}：{exc}", file=sys.stderr, flush=True)
            result = {"ok": False, "error": str(exc)}
        print(
            RESULT_MARKER + json.dumps(result, ensure_ascii=False),
            flush=True,
        )
    return 0


class RuntimeSupervisor:
    """監督可重用 MOSS runtime；單支卡住時只重啟 runtime。"""

    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.output: queue.Queue[str | None] = queue.Queue()
        self.reader: threading.Thread | None = None

    def _start(self) -> None:
        self.output = queue.Queue()
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--runtime"],
            cwd=str(Path(__file__).resolve().parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=os.environ.copy(),
        )
        self.process = process

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                self.output.put(line)
            self.output.put(None)

        self.reader = threading.Thread(target=read_output, daemon=True)
        self.reader.start()

    def _stop(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    def process_job(self, job: dict[str, Any]) -> tuple[bool, str | None]:
        if self.process is None or self.process.poll() is not None:
            self._start()
        assert self.process is not None
        assert self.process.stdin is not None
        timeout = subtitle_job_timeout(job)
        self.process.stdin.write(json.dumps(job, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._stop()
                return False, f"單支字幕工作超過 {timeout} 秒，已終止並繼續下一支"
            try:
                line = self.output.get(timeout=min(1.0, remaining))
            except queue.Empty:
                if self.process is None or self.process.poll() is not None:
                    self._stop()
                    return False, "字幕 runtime 意外結束"
                continue
            if line is None:
                self._stop()
                return False, "字幕 runtime 未回報結果便結束"
            if line.startswith(RESULT_MARKER):
                try:
                    result = json.loads(line[len(RESULT_MARKER):])
                except json.JSONDecodeError:
                    return False, "字幕 runtime 回報格式錯誤"
                return bool(result.get("ok")), result.get("error")
            print(line, end="", flush=True)
            deadline = time.monotonic() + timeout

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self._stop()
        self.process = None


def supervisor_main() -> int:
    failures = 0
    supervisor = RuntimeSupervisor()
    print("[字幕管線] 背景工作者已啟動，等待下載完成影片", flush=True)
    try:
        for raw_line in sys.stdin:
            if not raw_line.strip():
                continue
            job = json.loads(raw_line.lstrip("\ufeff"))
            ok, error = supervisor.process_job(job)
            if not ok:
                failures += 1
                name = job.get("video", "未知影片")
                print(
                    f"[字幕監督失敗] {name}：{error}",
                    file=sys.stderr,
                    flush=True,
                )
    finally:
        supervisor.close()
    print(f"[字幕管線] 全部佇列完成；失敗 {failures} 支", flush=True)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    args = parser.parse_args()
    return runtime_main() if args.runtime else supervisor_main()


if __name__ == "__main__":
    raise SystemExit(main())

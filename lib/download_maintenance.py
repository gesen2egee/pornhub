import os
import sys
import re
import argparse
import json
import glob
import shutil
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path
import yt_dlp
import video_meta
import sites
from project_paths import (
    CHOSEN_DIR,
    DOWNLOADED_DIR,
    LIB_DIR,
    MOSS_VENV_DIR,
    OUTPUT_ROOT,
    PREVIEW_VIDEOS_DIR,
    TEMP_DIR,
    VIDEOS_DIR,
    ensure_output_directories,
)

MAX_VIDEO_WIDTH = 1920
MAX_VIDEO_HEIGHT = 1080
HIGH_VIDEO_FORMAT = "bestvideo*+bestaudio/best"
HIGH_VIDEO_FORMAT_SORT = ["res:1080"]

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from PIL import Image


ROOT = str(LIB_DIR)
DEFAULT_MOSS_PYTHON = str(MOSS_VENV_DIR / "Scripts" / "python.exe")
LOW_VIDEO_DIR = str(PREVIEW_VIDEOS_DIR)
VIDEO_DIR = str(VIDEOS_DIR)
ARCHIVE_DIR = str(DOWNLOADED_DIR)
WORK_TEMP_DIR = str(TEMP_DIR)
DOWNLOAD_SOCKET_TIMEOUT = 30
DOWNLOAD_RETRIES = 3


def _pipeline_dir(target_dir):
    """依正式目錄名稱建立固定的下載／字幕暫存路徑。"""
    return os.path.join(
        WORK_TEMP_DIR,
        "pipeline",
        os.path.basename(os.path.normpath(target_dir)),
    )


def publish_official_video(video_path, final_video_path):
    """正式影片下載驗證完成便發布，不等待字幕或音訊增強。"""
    source = os.path.abspath(video_path)
    destination = os.path.abspath(final_video_path)
    if source == destination:
        return destination
    if os.path.exists(destination):
        raise RuntimeError(f"正式影片已存在，拒絕覆寫：{destination}")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.move(source, destination)
    staged_srt = os.path.splitext(source)[0] + ".srt"
    final_srt = os.path.splitext(destination)[0] + ".srt"
    if os.path.exists(staged_srt):
        os.replace(staged_srt, final_srt)
    print(
        f"   [PUBLISH] 下載完成，正式影片已立即發布："
        f"{os.path.basename(destination)}"
    )
    return destination


def is_http_416_error(error):
    """辨識遠端拒絕既有續傳範圍的錯誤。"""
    message = str(error).casefold()
    return (
        "http error 416" in message
        or "requested range not satisfiable" in message
    )


def clear_yt_dlp_resume_files(temp_dir, output_basename):
    """只移除指定輸出檔所屬的 yt-dlp 續傳狀態。"""
    output_stem = os.path.splitext(output_basename)[0]
    removed = []
    try:
        entries = os.scandir(temp_dir)
    except FileNotFoundError:
        return removed

    with entries:
        for entry in entries:
            name = entry.name
            if not entry.is_file() or not name.startswith(f"{output_stem}."):
                continue
            if ".part" not in name and not name.endswith(".ytdl"):
                continue
            os.remove(entry.path)
            removed.append(entry.path)
    return removed


def download_with_416_recovery(
    video_url,
    ydl_opts,
    temp_dir,
    output_basename,
):
    """遇到失效續傳範圍時，清除該影片狀態並僅從零重試一次。"""
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
        return
    except Exception as error:
        if not is_http_416_error(error):
            raise

    removed = clear_yt_dlp_resume_files(temp_dir, output_basename)
    print(
        "   [416 RECOVERY] 遠端影片已變更，已清除 "
        f"{len(removed)} 個舊續傳檔並從零重試一次。"
    )
    retry_opts = dict(ydl_opts)
    retry_opts["continuedl"] = False
    with yt_dlp.YoutubeDL(retry_opts) as ydl:
        ydl.download([video_url])


def positive_env_seconds(name, default):
    """讀取正整數秒數；設定錯誤時安全退回預設值。"""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(1, value)


def has_video_stream(path):
    """只有 ffprobe 確認含可播放 video stream 才算下載成功。"""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_type",
                "-of", "default=nw=1:nk=1",
                os.path.abspath(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return result.returncode == 0 and "video" in result.stdout.split()


def probe_video_dimensions(path):
    """回傳第一條 video stream 的 (width, height)，無法確認時回傳 None。"""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json",
                os.path.abspath(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout or "{}")
        stream = (payload.get("streams") or [])[0]
        width = int(stream["width"])
        height = int(stream["height"])
        return width, height
    except (
        FileNotFoundError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        IndexError,
        json.JSONDecodeError,
    ):
        return None


def is_within_1080p_dimensions(width, height):
    """接受橫式 1920×1080 與直式 1080×1920 的等效 1080P 範圍。"""
    return (
        width <= MAX_VIDEO_WIDTH
        and height <= MAX_VIDEO_HEIGHT
    ) or (
        width <= MAX_VIDEO_HEIGHT
        and height <= MAX_VIDEO_WIDTH
    )


def is_video_within_1080p(path):
    dimensions = probe_video_dimensions(path)
    if dimensions is None:
        return None
    return is_within_1080p_dimensions(*dimensions)


def remove_invalid_video(path, label):
    """移除沒有 video stream 的空殼，讓該九宮格重新下載。"""
    stream_state = has_video_stream(path) if os.path.exists(path) else None
    if stream_state is not False:
        return False
    try:
        os.remove(path)
        print(f"   [INVALID] {label} 沒有 video stream，已移除並重新下載")
    except OSError as exc:
        print(f"   [!] 無法移除無效的 {label}：{exc}")
    return True


def _backup_over_1080_video(video_path):
    """把超標原檔移到 output/00_temp 備份，避免重下載前遺失。"""
    relative_parent = os.path.relpath(
        os.path.dirname(os.path.abspath(video_path)),
        str(OUTPUT_ROOT),
    )
    backup_dir = os.path.join(
        WORK_TEMP_DIR,
        "over-1080-backup",
        relative_parent,
    )
    os.makedirs(backup_dir, exist_ok=True)
    destination = os.path.join(backup_dir, os.path.basename(video_path))
    if os.path.exists(destination):
        stem, ext = os.path.splitext(destination)
        destination = (
            f"{stem}.{datetime.now():%Y%m%d-%H%M%S}{ext}"
        )
    shutil.move(video_path, destination)
    return destination


def _grid_for_video_in_directory(video_path, directory):
    """依去編號後檔名尋找同名九宮格。"""
    video_stem = os.path.splitext(os.path.basename(video_path))[0].casefold()
    for grid_path in glob.glob(os.path.join(directory, "*.jpg")):
        grid_stem = os.path.splitext(os.path.basename(grid_path))[0]
        normalized = re.sub(r"^\d{4}-", "", grid_stem).casefold()
        if normalized == video_stem:
            return grid_path
    return None


def prepare_over_1080_redownloads():
    """備份現有超標影片、還原九宮格，回傳只需重下載的九宮格。"""
    selected_grids = []
    search_dirs = [
        VIDEO_DIR,
        _pipeline_dir(VIDEO_DIR),
    ]
    for directory in search_dirs:
        for video_path in sorted(glob.glob(os.path.join(directory, "*.mp4"))):
            if os.path.basename(video_path).startswith("."):
                continue
            dimensions = probe_video_dimensions(video_path)
            if (
                dimensions is None
                or is_within_1080p_dimensions(*dimensions)
            ):
                continue

            archived_grid = _archived_grid_for_video(video_path)
            local_grid = _grid_for_video_in_directory(
                video_path,
                VIDEO_DIR,
            ) or os.path.join(
                VIDEO_DIR, os.path.basename(archived_grid)
            )
            source_grid = (
                archived_grid
                if os.path.exists(archived_grid)
                else local_grid
            )
            if (
                not os.path.exists(source_grid)
                or not get_video_url_from_image(source_grid)
            ):
                print(
                    f"[!] 發現 {dimensions[0]}×{dimensions[1]} 超標影片，"
                    f"但找不到含 URL 的九宮格，為安全起見先保留："
                    f"{os.path.basename(video_path)}"
                )
                continue

            backup_path = _backup_over_1080_video(video_path)
            if os.path.abspath(source_grid) != os.path.abspath(local_grid):
                os.makedirs(os.path.dirname(local_grid), exist_ok=True)
                shutil.move(source_grid, local_grid)
            if local_grid not in selected_grids:
                selected_grids.append(local_grid)

            partial_hardsub = os.path.join(
                os.path.dirname(video_path),
                f".{os.path.splitext(os.path.basename(video_path))[0]}"
                ".hardsub.tmp.mp4",
            )
            if os.path.exists(partial_hardsub):
                os.remove(partial_hardsub)
                print(
                    "   [CLEAN] 已移除停止工作留下的未完成硬字幕暫存檔"
                )
            print(
                f"[REQUEUE] {dimensions[0]}×{dimensions[1]} 已備份至 "
                f"{backup_path}，九宮格已排入 1080P 重下載"
            )

    backup_root = os.path.join(WORK_TEMP_DIR, "over-1080-backup")
    for backup_video in glob.glob(
        os.path.join(backup_root, "**", "*.mp4"),
        recursive=True,
    ):
        basename = os.path.basename(backup_video)
        if os.path.exists(os.path.join(VIDEO_DIR, basename)):
            continue
        if os.path.exists(
            os.path.join(_pipeline_dir(VIDEO_DIR), basename)
        ):
            continue
        local_grid = _grid_for_video_in_directory(
            backup_video,
            VIDEO_DIR,
        )
        if (
            local_grid
            and get_video_url_from_image(local_grid)
            and local_grid not in selected_grids
        ):
            selected_grids.append(local_grid)
            print(
                f"[RESUME] 接續先前未成功的 1080P 重下載：{basename}"
            )
    return selected_grids


class SubtitleWorker:
    """以獨立 MOSS 程序處理字幕，下載主流程可持續抓下一支。"""

    def __init__(self):
        python = os.getenv("MOSS_PYTHON", DEFAULT_MOSS_PYTHON)
        if not os.path.exists(python):
            raise RuntimeError(
                f"找不到 MOSS 字幕環境：{python}。請先執行 00_setup_or_update.bat。"
            )
        worker_env = os.environ.copy()
        worker_env["PYTHONUTF8"] = "1"
        self.process = subprocess.Popen(
            [python, os.path.join(ROOT, "subtitle_worker.py")],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=worker_env,
        )
        self.closed = False
        self.queued = set()

    def enqueue(
        self,
        video_path,
        final_video_path,
        grid_path,
        is_low_quality=False,
        archive_grid=None,
    ):
        if self.closed or self.process.stdin is None:
            raise RuntimeError("字幕工作者已關閉。")
        if self.process.poll() is not None:
            raise RuntimeError(
                f"字幕工作者已提前結束，ExitCode={self.process.returncode}。"
            )
        queue_key = os.path.normcase(os.path.abspath(final_video_path))
        if queue_key in self.queued:
            print(
                f"   [QUEUE SKIP] 已在字幕佇列："
                f"{os.path.basename(final_video_path)}",
                flush=True,
            )
            return
        staged_srt = os.path.splitext(video_path)[0] + ".srt"
        final_srt = os.path.splitext(final_video_path)[0] + ".srt"
        if (
            os.path.abspath(staged_srt) != os.path.abspath(final_srt)
            and os.path.exists(final_srt)
            and not os.path.exists(staged_srt)
        ):
            shutil.move(final_srt, staged_srt)
            print(
                f"   [MIGRATE] 舊 SRT 已移至字幕暫存："
                f"{os.path.basename(staged_srt)}",
                flush=True,
            )
        job = {
            "video": os.path.abspath(video_path),
            "final_video": os.path.abspath(final_video_path),
            "grid": os.path.abspath(grid_path),
            "archive_dir": ARCHIVE_DIR,
            "archive_grid": (
                not bool(is_low_quality)
                if archive_grid is None
                else bool(archive_grid)
            ),
            "is_low_quality": bool(is_low_quality),
        }
        self.process.stdin.write(json.dumps(job, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        self.queued.add(queue_key)
        print(
            f"   [QUEUE] 已交給背景字幕管線：{os.path.basename(final_video_path)}",
            flush=True,
        )

    def close(self):
        if self.closed:
            return self.process.returncode or 0
        self.closed = True
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
        return self.process.wait()


def get_low_video_sample_range(duration):
    """預覽取片頭最多 3 分鐘（180 秒）。"""
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 180.0
    end = min(180.0, max(1.0, duration if duration > 0 else 180.0))
    return (0.0, end)


def low_video_download_ranges(info_dict, ydl):
    """依 yt-dlp 取得的影片總長度選擇預覽下載區間（前 3 分鐘）。"""
    start, end = get_low_video_sample_range(info_dict.get("duration"))
    ydl.to_screen(f"[info] PREVIEW 取樣區間：{start}–{end} 秒")
    yield {"start_time": start, "end_time": end}


def probe_stream_duration(stream_url, headers):
    """供 FFmpeg 備援路徑查詢直連影片長度；失敗時回傳 None。"""
    command = [
        "ffprobe",
        "-v", "error",
        "-headers", headers,
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        stream_url,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        pass
    return None


def is_http_video_url(url):
    """接受可交由 yt-dlp 處理的完整 HTTP/HTTPS 網址。"""
    if not isinstance(url, str):
        return False
    parsed = urllib.parse.urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def get_video_url_from_image(jpg_path):
    """直接從九宮格 JPG 圖片檔案的 EXIF Metadata (ImageDescription 0x010e) 中讀取影片 URL"""
    try:
        with Image.open(jpg_path) as img:
            exif = img.getexif()
            url = exif.get(0x010e)
            if is_http_video_url(url):
                return url.strip()
    except Exception:
        pass
    return None


def has_completed_subtitle(video_path, require_srt=False):
    """只有非 failed 的雙字幕 Meta，及必要外掛 SRT，才是完整成品。"""
    try:
        meta = video_meta.read_mp4_meta(video_path)
        status = meta.get("subtitle_status") or {}
        if status.get("outcome") == "failed":
            return False
        meta_complete = bool(
            meta.get("original_srt_present")
            and meta.get("translated_srt_present")
        )
        translated = meta.get("translated_srt") or ""
        srt_complete = (
            not require_srt
            or not translated.strip()
            or os.path.exists(os.path.splitext(video_path)[0] + ".srt")
        )
        return meta_complete and srt_complete
    except Exception:
        return False


def needs_subtitle_retry(video_path, require_srt=None):
    """舊 SRT、failed Meta 或缺少雙字幕 Meta 都需要重新處理。"""
    if require_srt is None:
        # 預覽與正式片皆輸出軟 SRT
        require_srt = True
    return not has_completed_subtitle(video_path, require_srt=require_srt)


def _archived_grid_for_video(video_path):
    stem = os.path.splitext(os.path.basename(video_path))[0].casefold()
    for grid in glob.glob(os.path.join(ARCHIVE_DIR, "*.jpg")):
        grid_stem = os.path.splitext(os.path.basename(grid))[0]
        normalized = re.sub(r"^\d{4}-", "", grid_stem).casefold()
        if normalized == stem:
            return grid
    return os.path.join(ARCHIVE_DIR, f"{stem}.jpg")


def enqueue_official_subtitle_retries(
    target_dir,
    is_low_quality,
    subtitle_worker,
):
    """排入舊 SRT／failed 影片；正式影片留在 03_videos。"""
    pipeline_dir = _pipeline_dir(target_dir)
    os.makedirs(pipeline_dir, exist_ok=True)
    queued = 0
    for final_video in sorted(glob.glob(os.path.join(target_dir, "*.mp4"))):
        if not needs_subtitle_retry(final_video):
            continue
        staged_video = os.path.join(pipeline_dir, os.path.basename(final_video))
        if is_low_quality:
            if os.path.exists(staged_video):
                print(
                    f"   [RETRY SKIP] 暫存影片已存在："
                    f"{os.path.basename(staged_video)}"
                )
                continue
            shutil.move(final_video, staged_video)
            job_video = staged_video
        else:
            job_video = final_video
        grid = (
            os.path.splitext(final_video)[0] + ".jpg"
            if is_low_quality
            else _archived_grid_for_video(final_video)
        )
        subtitle_worker.enqueue(
            job_video,
            final_video,
            grid,
            is_low_quality=is_low_quality,
            archive_grid=False,
        )
        queued += 1
    return queued


def enqueue_staged_subtitle_retries(
    target_dir,
    is_low_quality,
    subtitle_worker,
):
    """接手舊 pipeline；正式影片先發布，預覽影片維持暫存。"""
    pipeline_dir = _pipeline_dir(target_dir)
    queued = 0
    for staged_video in sorted(
        glob.glob(os.path.join(pipeline_dir, "*.mp4"))
    ):
        final_video = os.path.abspath(
            os.path.join(target_dir, os.path.basename(staged_video))
        )
        if os.path.exists(final_video):
            print(
                f"   [RETRY CONFLICT] 正式與暫存影片同時存在，跳過："
                f"{os.path.basename(staged_video)}"
            )
            continue
        job_video = (
            staged_video
            if is_low_quality
            else publish_official_video(staged_video, final_video)
        )
        grid = (
            os.path.splitext(final_video)[0] + ".jpg"
            if is_low_quality
            else _archived_grid_for_video(final_video)
        )
        subtitle_worker.enqueue(
            job_video,
            final_video,
            grid,
            is_low_quality=is_low_quality,
            archive_grid=False,
        )
        queued += 1
    return queued


def upgrade_media_web_meta(jpg_path, mp4_path, video_url, info=None):
    """補齊影片與九宮格 WEB_META；失敗不影響下載結果。"""
    try:
        if info is None:
            adapter = sites.get_adapter_for_url(video_url)
            resolved = sites.resolve_playable(
                adapter,
                video_url,
                purpose="info",
                prefer_lowest=False,
            )
            info = resolved.get("info")
        info = dict(info or {})
        info.setdefault("webpage_url", video_url)
        web_meta = video_meta.build_web_meta(info)
        if os.path.exists(mp4_path):
            video_meta.merge_write_mp4_meta(mp4_path, web_meta=web_meta)
        was_legacy = video_meta.is_legacy_grid_jpg(jpg_path)
        video_meta.write_grid_jpg_web_meta(jpg_path, web_meta, url=video_url)
        label = "舊格式→已升級" if was_legacy else "已同步"
        print(f"   [META] 九宮格 {label} WEB_META，影片 metadata 已補齊")
    except Exception as exc:
        print(f"   [!] 補齊 WEB_META 失敗（不影響影片）：{exc}")

def process_official_directory(selected_jpgs=None):
    """03_videos：480P、Whisper 字幕／剪片估計、不 enhance。"""
    import full_video_pipeline

    jpg_files = (
        sorted(selected_jpgs)
        if selected_jpgs is not None
        else sorted(glob.glob(os.path.join(VIDEO_DIR, "*.jpg")))
    )
    if not jpg_files:
        return 0

    # 480P：Whisper ASR（剪片依據）+ GLM 5.2 minimal 翻譯；關閉 enhance
    os.environ["ASR_BACKEND"] = os.getenv("STANDARD_ASR_BACKEND", "whisper")
    os.environ["OPENROUTER_MODEL"] = os.getenv(
        "STANDARD_OPENROUTER_MODEL", "z-ai/glm-5.2"
    )
    os.environ["TRANSLATE_REASONING_EFFORT"] = os.getenv(
        "STANDARD_TRANSLATE_REASONING", "minimal"
    )
    os.environ["HIGH_VIDEO_HEIGHT"] = os.getenv("STANDARD_VIDEO_HEIGHT", "480")
    os.environ["AUDIO_AUTO_ENHANCE"] = "0"
    os.environ["ENABLE_DIALOGUE_TRIM"] = "1"

    print(
        f"[+] 標準片管線 [{VIDEO_DIR}/] 共 {len(jpg_files)} 張 "
        f"（代理 → Whisper + GLM 5.2 minimal → 480P 剪片，不 enhance）\n"
    )
    success_count = 0
    failed_count = 0
    for idx, jpg_path in enumerate(jpg_files, 1):
        print(f"\n[{idx}/{len(jpg_files)}] {os.path.basename(jpg_path)}")
        try:
            full_video_pipeline.process_full_video_from_grid(
                Path(jpg_path),
                final_dir=Path(VIDEO_DIR),
                archive_dir=Path(ARCHIVE_DIR),
                max_height=int(os.environ["HIGH_VIDEO_HEIGHT"]),
                enable_enhance=False,
                enable_dialogue_trim=True,
                work_bucket="03_videos",
            )
            success_count += 1
        except Exception as exc:
            print(f"  [FAIL] {exc}")
            failed_count += 1
    print(
        f"[*] 標準片完成: 成功 {success_count} 部 | 失敗 {failed_count} 部"
    )
    return failed_count


def process_single_directory(
    target_dir,
    is_low_quality,
    subtitle_worker,
    selected_jpgs=None,
):
    """處理預覽或標準目錄。預覽改走循序 preview_pipeline。"""
    if not is_low_quality:
        return process_official_directory(selected_jpgs=selected_jpgs)

    import preview_pipeline

    jpg_files = (
        sorted(selected_jpgs)
        if selected_jpgs is not None
        else sorted(glob.glob(os.path.join(target_dir, "*.jpg")))
    )
    if not jpg_files:
        return 0

    print(
        f"[+] 預覽管線 [{target_dir}/] 共 {len(jpg_files)} 張 "
        f"（前 3 分鐘低畫質 → Whisper → 語音剪片 → 軟 SRT，不硬字幕/不 enhance）\n"
    )
    success_count = 0
    failed_count = 0
    for idx, jpg_path in enumerate(jpg_files, 1):
        print(f"\n[{idx}/{len(jpg_files)}] {os.path.basename(jpg_path)}")
        try:
            preview_pipeline.process_preview_from_grid(
                Path(jpg_path),
                final_dir=Path(target_dir),
            )
            success_count += 1
        except Exception as exc:
            print(f"  [FAIL] {exc}")
            failed_count += 1
    print(
        f"[*] 預覽完成: 成功 {success_count} 部 | 失敗 {failed_count} 部"
    )
    return failed_count

def run_download_process(
    retry_subtitles=False,
    repair_over_1080=False,
):
    """主下載流程控制"""
    print(f"==================================================")
    print(f"   多站雙畫質原影片下載器 (EXIF URL + site registry)")
    print(f"==================================================")

    ensure_output_directories()

    over_1080_jpgs = (
        []
        if retry_subtitles
        else prepare_over_1080_redownloads()
    )
    low_jpgs = glob.glob(os.path.join(LOW_VIDEO_DIR, "*.jpg"))
    high_jpgs = glob.glob(os.path.join(VIDEO_DIR, "*.jpg"))
    try:
        import chosen_pipeline

        chosen_items = chosen_pipeline.list_chosen_items(Path(CHOSEN_DIR))
    except Exception:
        chosen_items = []

    if repair_over_1080 and not over_1080_jpgs:
        print("[OK] 沒有發現需要重下載的超過 1080P 影片。")
        return 0

    if (
        not retry_subtitles
        and not repair_over_1080
        and not low_jpgs
        and not high_jpgs
        and not chosen_items
    ):
        print(
            "[!] 找不到待處理項目。請放入："
            "02_preview_videos（3 分鐘預覽+語音剪片）／"
            "03_videos（480P）／"
            "05_chosen（精選 1080P）"
        )
        return 0

    print(
        f"[+] 檢測到 02_preview ({len(low_jpgs)}) | "
        f"03_videos 480P ({len(high_jpgs)}) | "
        f"05_chosen ({len(chosen_items)})\n"
    )

    if repair_over_1080:
        failures = process_official_directory(selected_jpgs=over_1080_jpgs)
        return 3 if failures else 0

    download_failures = 0
    try:
        # 預覽 worker 不 enhance；精選管線會在自己流程內再打開
        os.environ["AUDIO_AUTO_ENHANCE"] = "0"
        subtitle_worker = SubtitleWorker()
    except Exception as exc:
        print(f"[錯誤] 無法啟動字幕管線：{exc}", file=sys.stderr)
        return 2

    try:
        if retry_subtitles:
            queued = 0
            for target_dir, is_low in (
                (LOW_VIDEO_DIR, True),
                (VIDEO_DIR, False),
            ):
                queued += enqueue_official_subtitle_retries(
                    target_dir,
                    is_low,
                    subtitle_worker,
                )
                queued += enqueue_staged_subtitle_retries(
                    target_dir,
                    is_low,
                    subtitle_worker,
                )
            print(f"[*] 字幕修復模式共排入 {queued} 支影片")
        # 【階段 1】預覽：前 3 分鐘 → Whisper → 語音剪片 → 軟 SRT
        if not retry_subtitles and not repair_over_1080 and low_jpgs:
            print("==================================================")
            print(
                " [階段 1/3] 02_preview_videos"
                "（3 分鐘低畫質 → Whisper 語音剪片 → 軟 SRT）"
            )
            print("==================================================")
            download_failures += process_single_directory(
                LOW_VIDEO_DIR, is_low_quality=True,
                subtitle_worker=subtitle_worker,
            ) or 0

        # 標準片舊檔字幕補跑
        if not retry_subtitles and not repair_over_1080:
            enqueue_official_subtitle_retries(
                VIDEO_DIR,
                False,
                subtitle_worker,
            )
            enqueue_staged_subtitle_retries(
                VIDEO_DIR,
                False,
                subtitle_worker,
            )

        # 【階段 2】標準全片：480P，不 enhance
        selected_high_jpgs = (
            over_1080_jpgs if repair_over_1080 else high_jpgs
        )
        if not retry_subtitles and selected_high_jpgs:
            print("\n==================================================")
            print(
                " [階段 2/3] 03_videos"
                "（Whisper+GLM 5.2 minimal→480P 剪片，不 enhance）"
            )
            print("==================================================")
            download_failures += process_official_directory(
                selected_jpgs=(
                    selected_high_jpgs if repair_over_1080 else None
                ),
            ) or 0

        # 【階段 3】精選：1080P + MOSS + GLM 5.2 + enhance → 06_good
        if not retry_subtitles and not repair_over_1080 and chosen_items:
            print("\n==================================================")
            print(
                " [階段 3/3] 05_chosen"
                "（1080P + MOSS + GLM 5.2 minimal + enhance → 06_good）"
            )
            print("==================================================")
            import chosen_pipeline

            _ok, chosen_fail = chosen_pipeline.process_chosen_directory(
                Path(CHOSEN_DIR)
            )
            download_failures += chosen_fail
    finally:
        print("\n[*] 預覽字幕佇列完成，等待背景字幕管線…")
        subtitle_exit = subtitle_worker.close()

    print("\n==================================================")
    if subtitle_exit:
        print("[未完成] 部分字幕流程失敗，相關九宮格保留在原資料夾。")
        return subtitle_exit
    if download_failures:
        print(
            f"[未完成] 有 {download_failures} 支影片下載失敗，"
            "九宮格已保留供下次重試。"
        )
        return 3
    print("[ALL DONE] 下載、完整字幕與九宮格歸檔全數完成！")
    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--retry-subtitles",
        action="store_true",
        help="只重跑舊 SRT、failed Meta 與未完成字幕，不下載新影片",
    )
    parser.add_argument(
        "--repair-over-1080",
        action="store_true",
        help="只備份並重下載現有超過等效 1080P 的影片",
    )
    parser.add_argument(
        "--grid",
        type=str,
        default=None,
        help="只對指定九宮格 JPG 跑正式片循序管線（不掃描目錄）",
    )
    parser.add_argument(
        "--keep-proxy",
        action="store_true",
        help="搭配 --grid：保留代理與分段暫存",
    )
    args = parser.parse_args()
    if args.grid:
        import full_video_pipeline

        ensure_output_directories()
        try:
            full_video_pipeline.process_full_video_from_grid(
                Path(args.grid),
                final_dir=Path(VIDEO_DIR),
                archive_dir=Path(ARCHIVE_DIR),
                keep_proxy=args.keep_proxy,
            )
        except Exception as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            raise SystemExit(1)
        raise SystemExit(0)
    raise SystemExit(
        run_download_process(
            retry_subtitles=args.retry_subtitles,
            repair_over_1080=args.repair_over_1080,
        )
    )

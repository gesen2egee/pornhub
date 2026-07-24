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
import yt_dlp
import video_meta

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

def select_pornhub_mp4_url(html, max_quality=1080, prefer_lowest=False):
    """從 Pornhub 頁面選出不超過指定解析度的 MP4 直連。"""
    candidates = []
    for quality, url in re.findall(
        r'"quality_(\d+)p"\s*:\s*"([^"]+)"',
        html,
    ):
        candidates.append((int(quality), url))

    for block in re.findall(r"\{[^{}]*\}", html, re.DOTALL):
        quality_match = re.search(
            r'"quality"\s*:\s*"?(\d+)(?:p)?"?',
            block,
        )
        url_match = re.search(r'"videoUrl"\s*:\s*"([^"]+)"', block)
        if quality_match and url_match:
            candidates.append(
                (int(quality_match.group(1)), url_match.group(1))
            )

    eligible = [
        (quality, url)
        for quality, url in candidates
        if quality <= max_quality
    ]
    if not eligible:
        return None
    quality, url = (
        min(eligible, key=lambda item: item[0])
        if prefer_lowest
        else max(eligible, key=lambda item: item[0])
    )
    del quality
    return url.replace("\\/", "/").replace("\\u0026", "&")


def direct_fetch_pornhub_mp4_stream(
    webpage_url,
    max_quality=1080,
    prefer_lowest=False,
):
    """備用解析器也嚴格限制畫質，避免 yt-dlp 失敗時偷抓回 4K。"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
        'Cookie': 'age_verified=1; platform=pc',
        'Referer': 'https://cn.pornhub.com/'
    }
    try:
        req = urllib.request.Request(webpage_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
            
        return select_pornhub_mp4_url(
            html,
            max_quality=max_quality,
            prefer_lowest=prefer_lowest,
        )
    except Exception as e:
        print(f"  [!] 原生備用解析器抓取失敗: {e}")
    return None

from PIL import Image


ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MOSS_PYTHON = os.path.join(ROOT, "moss", ".venv", "Scripts", "python.exe")
DOWNLOAD_SOCKET_TIMEOUT = 30
DOWNLOAD_RETRIES = 3
FALLBACK_DOWNLOAD_TIMEOUT = 2 * 60 * 60


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
    """把超標原檔移到 ignored temp 備份，避免重下載前遺失。"""
    relative_parent = os.path.relpath(
        os.path.dirname(os.path.abspath(video_path)),
        ROOT,
    )
    backup_dir = os.path.join(
        ROOT,
        "temp",
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
        os.path.join(ROOT, "videos"),
        os.path.join(ROOT, "temp", "pipeline", "videos"),
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
                os.path.join(ROOT, "videos"),
            ) or os.path.join(
                ROOT, "videos", os.path.basename(archived_grid)
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

    backup_root = os.path.join(ROOT, "temp", "over-1080-backup")
    for backup_video in glob.glob(
        os.path.join(backup_root, "**", "*.mp4"),
        recursive=True,
    ):
        basename = os.path.basename(backup_video)
        if os.path.exists(os.path.join(ROOT, "videos", basename)):
            continue
        if os.path.exists(
            os.path.join(ROOT, "temp", "pipeline", "videos", basename)
        ):
            continue
        local_grid = _grid_for_video_in_directory(
            backup_video,
            os.path.join(ROOT, "videos"),
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
                f"找不到 MOSS 字幕環境：{python}。請先執行 install_moss.bat。"
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
            "archive_dir": os.path.abspath("downloaded"),
            "archive_grid": (
                not bool(is_low_quality)
                if archive_grid is None
                else bool(archive_grid)
            ),
            "is_low_quality": bool(is_low_quality),
        }
        self.process.stdin.write(json.dumps(job, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
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
    """超過一分鐘取 30–60 秒，否則維持取影片開頭 30 秒。"""
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0
    return (30, 60) if duration > 60 else (0, 30)


def low_video_download_ranges(info_dict, ydl):
    """依 yt-dlp 取得的影片總長度動態選擇 LOW VIDEO 下載區間。"""
    start, end = get_low_video_sample_range(info_dict.get("duration"))
    ydl.to_screen(f"[info] LOW VIDEO 取樣區間：{start}–{end} 秒")
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


def is_pornhub_url(url):
    """判斷網址是否適用 Pornhub 專用的原生備用解析器。"""
    if not is_http_video_url(url):
        return False
    hostname = (urllib.parse.urlparse(url.strip()).hostname or "").lower()
    return hostname == "pornhub.com" or hostname.endswith(".pornhub.com")


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
        parent_name = os.path.basename(
            os.path.dirname(os.path.abspath(video_path))
        ).casefold()
        require_srt = parent_name not in {"low_videos", "low_video"}
    return not has_completed_subtitle(video_path, require_srt=require_srt)


def _archived_grid_for_video(video_path):
    stem = os.path.splitext(os.path.basename(video_path))[0].casefold()
    for grid in glob.glob(os.path.join("downloaded", "*.jpg")):
        grid_stem = os.path.splitext(os.path.basename(grid))[0]
        normalized = re.sub(r"^\d{4}-", "", grid_stem).casefold()
        if normalized == stem:
            return grid
    return os.path.join("downloaded", f"{stem}.jpg")


def enqueue_official_subtitle_retries(
    target_dir,
    is_low_quality,
    subtitle_worker,
):
    """把正式資料夾中的舊 SRT／failed 影片重新移回字幕暫存。"""
    pipeline_dir = os.path.abspath(
        os.path.join("temp", "pipeline", target_dir)
    )
    os.makedirs(pipeline_dir, exist_ok=True)
    queued = 0
    for final_video in sorted(glob.glob(os.path.join(target_dir, "*.mp4"))):
        if not needs_subtitle_retry(final_video):
            continue
        staged_video = os.path.join(
            pipeline_dir,
            os.path.basename(final_video),
        )
        if os.path.exists(staged_video):
            print(
                f"   [RETRY SKIP] 暫存影片已存在："
                f"{os.path.basename(staged_video)}"
            )
            continue
        shutil.move(final_video, staged_video)
        grid = (
            os.path.splitext(final_video)[0] + ".jpg"
            if is_low_quality
            else _archived_grid_for_video(final_video)
        )
        subtitle_worker.enqueue(
            staged_video,
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
    """只重跑模式也接手先前留在 temp/pipeline 的影片。"""
    pipeline_dir = os.path.abspath(
        os.path.join("temp", "pipeline", target_dir)
    )
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
        grid = (
            os.path.splitext(final_video)[0] + ".jpg"
            if is_low_quality
            else _archived_grid_for_video(final_video)
        )
        subtitle_worker.enqueue(
            staged_video,
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
            with yt_dlp.YoutubeDL({
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
            }) as ydl:
                info = ydl.extract_info(video_url, download=False)
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

def process_single_directory(
    target_dir,
    is_low_quality,
    subtitle_worker,
    selected_jpgs=None,
):
    """處理單一目錄 (low_videos/ 或 videos/) 中 JPG 圖片內嵌 EXIF 網址的下載邏輯"""
    # 從名字為順序開始下載 (字母/數字自然排序)
    jpg_files = (
        sorted(selected_jpgs)
        if selected_jpgs is not None
        else sorted(glob.glob(os.path.join(target_dir, "*.jpg")))
    )
    if not jpg_files:
        return

    mode_label = (
        "最低解析度/動態30秒取樣"
        if is_low_quality
        else "最高 1080P"
    )
    print(f"[+] 開始為 [{target_dir}/] 依檔名順序讀取圖片內嵌 EXIF 網址並進行 {mode_label} 下載 (共 {len(jpg_files)} 張預覽圖)...\n")

    success_count = 0
    skipped_count = 0
    failed_count = 0
    prompted_upgrade = False

    for idx, jpg_path in enumerate(jpg_files, 1):
        image_name = os.path.basename(jpg_path)
        
        raw_name_no_ext = os.path.splitext(image_name)[0]
        base_name_without_num = re.sub(r'^\d{4}-', '', raw_name_no_ext)
        
        # low_videos 檔名與九宮格同名(保留數字編號)；videos 則去除前綴數字
        if is_low_quality:
            video_file_basename = raw_name_no_ext + ".mp4"
        else:
            video_file_basename = base_name_without_num + ".mp4"
            
        final_video_file = os.path.join(target_dir, video_file_basename)
        pipeline_dir_abs = os.path.abspath(
            os.path.join("temp", "pipeline", target_dir)
        )
        os.makedirs(pipeline_dir_abs, exist_ok=True)
        staged_video_file = os.path.join(
            pipeline_dir_abs, video_file_basename
        )
        os.makedirs("downloaded", exist_ok=True)
        video_url = get_video_url_from_image(jpg_path)
        remove_invalid_video(staged_video_file, "暫存影片")
        remove_invalid_video(final_video_file, "正式影片")

        if os.path.exists(staged_video_file):
            if os.path.exists(final_video_file):
                if has_completed_subtitle(final_video_file):
                    print(
                        f"[{idx}/{len(jpg_files)}] [EXISTS] 正式成品已存在，"
                        "暫存檔保留不覆寫"
                    )
                    subtitle_worker.enqueue(
                        final_video_file,
                        final_video_file,
                        jpg_path,
                        is_low_quality=is_low_quality,
                    )
                else:
                    print(
                        f"[{idx}/{len(jpg_files)}] [CONFLICT] 暫存與正式位置"
                        "同時存在未完成影片，為避免覆寫已跳過"
                    )
                skipped_count += 1
                continue
            print(
                f"[{idx}/{len(jpg_files)}] [RESUME] 找到未完成暫存影片："
                f"{os.path.basename(staged_video_file)}"
            )
            if video_url:
                upgrade_media_web_meta(
                    jpg_path, staged_video_file, video_url
                )
            subtitle_worker.enqueue(
                staged_video_file,
                final_video_file,
                jpg_path,
                is_low_quality=is_low_quality,
            )
            skipped_count += 1
            continue

        if os.path.exists(final_video_file):
            print(f"[{idx}/{len(jpg_files)}] [EXISTS] 影片已存在: {os.path.basename(final_video_file)}")
            if has_completed_subtitle(final_video_file):
                if is_low_quality:
                    print("   [DONE] low video 已完整完成，九宮格保留原位")
                    skipped_count += 1
                    continue
                subtitle_worker.enqueue(
                    final_video_file,
                    final_video_file,
                    jpg_path,
                    is_low_quality=False,
                )
                skipped_count += 1
                continue
            if video_url:
                upgrade_media_web_meta(jpg_path, final_video_file, video_url)
            shutil.move(final_video_file, staged_video_file)
            print("   [STAGE] 舊未完成影片已移至 temp/pipeline 繼續處理")
            subtitle_worker.enqueue(
                staged_video_file,
                final_video_file,
                jpg_path,
                is_low_quality=is_low_quality,
            )
            skipped_count += 1
            continue

        video_title = base_name_without_num

        if not video_url:
            print(f"[{idx}/{len(jpg_files)}] [SKIP] 九宮格圖片未內嵌影片 URL Metadata，跳過該圖片: {image_name}\n")
            skipped_count += 1
            continue

        print(f"[{idx}/{len(jpg_files)}] 正在啟動下載 ({mode_label}): {video_title}")
        print(f"   - 圖片 Metadata 讀取網址: {video_url}")
        print(f"   - 暫存路徑: {staged_video_file}")
        print(f"   - 完成路徑: {final_video_file}")

        fmt_spec = (
            "worstvideo+worstaudio/worst"
            if is_low_quality
            else HIGH_VIDEO_FORMAT
        )
        temp_dir_abs = os.path.abspath("temp")
        os.makedirs(temp_dir_abs, exist_ok=True)
        temp_thumb_template = os.path.join(temp_dir_abs, f"thumb_{idx}_%(id)s.%(ext)s")

        ydl_opts = {
            'format': fmt_spec,
            'paths': {
                'home': pipeline_dir_abs,
                'temp': temp_dir_abs,
            },
            'outtmpl': {
                'default': video_file_basename,
                'thumbnail': temp_thumb_template
            },
            'quiet': False,
            'no_warnings': True,
            'socket_timeout': DOWNLOAD_SOCKET_TIMEOUT,
            'retries': DOWNLOAD_RETRIES,
            'fragment_retries': DOWNLOAD_RETRIES,
            'extractor_retries': DOWNLOAD_RETRIES,
            'file_access_retries': DOWNLOAD_RETRIES,
        }
        if not is_low_quality:
            ydl_opts["format_sort"] = HIGH_VIDEO_FORMAT_SORT

        # videos 模式 (最高畫質) 內嵌縮圖封面
        if not is_low_quality:
            ydl_opts['writethumbnail'] = True
            ydl_opts['postprocessors'] = [{
                'key': 'EmbedThumbnail',
                'already_have_thumbnail': False,
            }]
        else:
            ydl_opts['writethumbnail'] = False

        # low_videos 模式：超過 60 秒下載 30-60 秒，否則下載 0-30 秒
        if is_low_quality:
            ydl_opts['download_ranges'] = low_video_download_ranges

        download_success = False
        try:
            download_with_416_recovery(
                video_url,
                ydl_opts,
                temp_dir_abs,
                video_file_basename,
            )
            download_success = (
                os.path.exists(staged_video_file)
                and has_video_stream(staged_video_file) is True
                and (
                    is_low_quality
                    or is_video_within_1080p(staged_video_file) is True
                )
            )
            if not download_success:
                print(
                    "   [!] yt-dlp 結束但影片無效，或解析度超過 1080P。"
                )
                remove_invalid_video(staged_video_file, "yt-dlp 暫存影片")
                if (
                    os.path.exists(staged_video_file)
                    and not is_low_quality
                    and is_video_within_1080p(staged_video_file) is False
                ):
                    os.remove(staged_video_file)
                    print("   [LIMIT] 已移除超過 1080P 的下載暫存檔")
                raise RuntimeError("yt-dlp 未產生有效 video stream")
        except Exception as e:
            err_str = str(e)
            print(f"   [!] yt-dlp 下載過程觸發異常: {e}")
            if "410" in err_str or "Gone" in err_str:
                if not prompted_upgrade:
                    print("=" * 65)
                    print("[!] 警告: 本機的 yt-dlp 套件版本過舊，觸發了 HTTP Error 410 錯誤！")
                    print("[!] 請在控制台 (CMD/PowerShell) 執行以下指令進行升級：")
                    print("    pip install --upgrade yt-dlp")
                    print("=" * 65)
                    prompted_upgrade = True
            
            if not is_pornhub_url(video_url):
                print("   [SKIP FALLBACK] 此來源不是 Pornhub，不使用 Pornhub 專用備用解析器。")
                direct_mp4 = None
            else:
                print(f"   [FALLBACK] 嘗試啟動 Pornhub 原生備用解析器繞過異常...")
                direct_mp4 = direct_fetch_pornhub_mp4_stream(
                    video_url,
                    max_quality=1080,
                    prefer_lowest=is_low_quality,
                )
            if direct_mp4:
                print(f"   [+] 成功解析直連 MP4 串流，發起 FFmpeg 極速下載 (帶認證 Header，暫存於 temp/)...")
                temp_ffmpeg_file = os.path.join(temp_dir_abs, f"ffmpeg_{idx}_{video_file_basename}")
                ff_headers = (
                    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36\r\n"
                    "Referer: https://www.pornhub.com/\r\n"
                    "Cookie: age_verified=1; platform=pc\r\n"
                )
                ff_base_opts = [
                    "ffmpeg",
                    "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
                    "-allowed_segment_extensions", "ALL",
                    "-rw_timeout", str(DOWNLOAD_SOCKET_TIMEOUT * 1_000_000),
                    "-headers", ff_headers,
                    "-y"
                ]
                if is_low_quality:
                    duration = probe_stream_duration(direct_mp4, ff_headers)
                    start, end = get_low_video_sample_range(duration)
                    print(f"   [i] LOW VIDEO 取樣區間：{start}–{end} 秒")
                    ffmpeg_cmd = ff_base_opts + [
                        "-ss", str(start),
                        "-i", direct_mp4,
                        "-t", str(end - start),
                        "-c", "copy",
                        temp_ffmpeg_file,
                    ]
                else:
                    ffmpeg_cmd = ff_base_opts + ["-i", direct_mp4, "-c", "copy", temp_ffmpeg_file]
                try:
                    fallback_timeout = positive_env_seconds(
                        "DOWNLOAD_JOB_TIMEOUT_SECONDS",
                        FALLBACK_DOWNLOAD_TIMEOUT,
                    )
                    res_ff = subprocess.run(
                        ffmpeg_cmd,
                        timeout=fallback_timeout,
                    )
                    if (
                        res_ff.returncode == 0
                        and os.path.exists(temp_ffmpeg_file)
                        and has_video_stream(temp_ffmpeg_file) is True
                        and (
                            is_low_quality
                            or is_video_within_1080p(temp_ffmpeg_file) is True
                        )
                    ):
                        shutil.move(temp_ffmpeg_file, staged_video_file)
                        download_success = True
                    elif os.path.exists(temp_ffmpeg_file):
                        remove_invalid_video(
                            temp_ffmpeg_file,
                            "FFmpeg 暫存影片",
                        )
                except subprocess.TimeoutExpired:
                    print(
                        "   [!] FFmpeg 單支下載超時，保留暫存並繼續下一支。"
                    )

        if download_success:
            print(f"  [OK] 影片下載至暫存 -> {os.path.basename(staged_video_file)}")
            upgrade_media_web_meta(jpg_path, staged_video_file, video_url)
            subtitle_worker.enqueue(
                staged_video_file,
                final_video_file,
                jpg_path,
                is_low_quality=is_low_quality,
            )
            success_count += 1
        else:
            print(f"  [FAIL] 影片下載失敗: {video_url}\n")
            failed_count += 1

    print(
        f"[*] [{target_dir}/] 處理完成: 成功下載 {success_count} 部 | "
        f"已存在/跳過 {skipped_count} 部 | 失敗 {failed_count} 部"
    )
    return failed_count

def run_download_process(
    retry_subtitles=False,
    repair_over_1080=False,
):
    """主下載流程控制"""
    print(f"==================================================")
    print(f"   Pornhub 雙畫質原影片下載器 (純 EXIF 圖片讀取版)")
    print(f"==================================================")

    os.makedirs("low_videos", exist_ok=True)
    os.makedirs("videos", exist_ok=True)

    over_1080_jpgs = (
        []
        if retry_subtitles
        else prepare_over_1080_redownloads()
    )
    low_jpgs = glob.glob(os.path.join("low_videos", "*.jpg"))
    high_jpgs = glob.glob(os.path.join("videos", "*.jpg"))

    if repair_over_1080 and not over_1080_jpgs:
        print("[OK] 沒有發現需要重下載的超過 1080P 影片。")
        return 0

    if (
        not retry_subtitles
        and not repair_over_1080
        and not low_jpgs
        and not high_jpgs
    ):
        print(f"[!] low_videos/ 與 videos/ 資料夾中均找不到任何被移入的九宮格 JPG 圖片！")
        print(f"[i] 請將預覽圖片移動至 low_videos/ (最低畫質/極速) 或 videos/ (最高畫質) 後再次執行。")
        return 0

    print(f"[+] 檢測到 low_videos/ ({len(low_jpgs)} 張圖片) | videos/ ({len(high_jpgs)} 張圖片)\n")

    download_failures = 0
    try:
        subtitle_worker = SubtitleWorker()
    except Exception as exc:
        print(f"[錯誤] 無法啟動字幕管線：{exc}", file=sys.stderr)
        return 2

    try:
        if retry_subtitles:
            queued = 0
            for target_dir, is_low in (
                ("low_videos", True),
                ("videos", False),
            ):
                queued += enqueue_staged_subtitle_retries(
                    target_dir,
                    is_low,
                    subtitle_worker,
                )
                queued += enqueue_official_subtitle_retries(
                    target_dir,
                    is_low,
                    subtitle_worker,
                )
            print(f"[*] 字幕修復模式共排入 {queued} 支影片")
        # 【階段一】優先處理 low_videos/ 目錄 (最低畫質)
        if not retry_subtitles and not repair_over_1080 and low_jpgs:
            print("==================================================")
            print(" [階段 1/2] 開始處理 low_videos/ (最低解析度/動態30秒取樣)")
            print("==================================================")
            download_failures += process_single_directory(
                "low_videos", is_low_quality=True,
                subtitle_worker=subtitle_worker,
            ) or 0

        if not retry_subtitles and not repair_over_1080:
            enqueue_official_subtitle_retries(
                "videos",
                False,
                subtitle_worker,
            )

        # 【階段二】處理完 low_videos/ 後，處理 videos/ 目錄 (最高畫質)
        selected_high_jpgs = (
            over_1080_jpgs if repair_over_1080 else high_jpgs
        )
        if not retry_subtitles and selected_high_jpgs:
            print("\n==================================================")
            print(" [階段 2/2] 開始處理 videos/ (最高畫質下載)")
            print("==================================================")
            download_failures += process_single_directory(
                "videos", is_low_quality=False,
                subtitle_worker=subtitle_worker,
                selected_jpgs=(
                    selected_high_jpgs
                    if repair_over_1080
                    else None
                ),
            ) or 0
    finally:
        print("\n[*] 下載佇列完成，等待背景字幕管線處理剩餘影片...")
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
    args = parser.parse_args()
    raise SystemExit(
        run_download_process(
            retry_subtitles=args.retry_subtitles,
            repair_over_1080=args.repair_over_1080,
        )
    )

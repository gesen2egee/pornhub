import os
import sys
import re
import json
import glob
import shutil
import subprocess
import urllib.request
import urllib.parse
import yt_dlp

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

def direct_fetch_pornhub_mp4_stream(webpage_url):
    """備用原生解析器：當 yt-dlp 因版本較舊報 410 錯誤時，直接分析網頁結構擷取最高畫質 MP4 串流 URL"""
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
            
        m = re.search(r'var\s+flashvars_\d+\s*=\s*({.*?});', html, re.DOTALL)
        if not m:
            m = re.search(r'mediaDefinitions\s*:\s*(\[.*?\]),', html, re.DOTALL)
            
        if m:
            json_str = m.group(1)
            quality_urls = re.findall(r'"quality_(\d+p)"\s*:\s*"([^"]+)"', json_str)
            if not quality_urls:
                quality_urls = re.findall(r'"videoUrl"\s*:\s*"([^"]+)"', json_str)
                
            if quality_urls:
                best_url = quality_urls[0][1].replace('\\/', '/') if isinstance(quality_urls[0], tuple) else quality_urls[0].replace('\\/', '/')
                return best_url
    except Exception as e:
        print(f"  [!] 原生備用解析器抓取失敗: {e}")
    return None

from PIL import Image


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

def process_single_directory(target_dir, is_low_quality):
    """處理單一目錄 (low_videos/ 或 videos/) 中 JPG 圖片內嵌 EXIF 網址的下載邏輯"""
    # 從名字為順序開始下載 (字母/數字自然排序)
    jpg_files = sorted(glob.glob(os.path.join(target_dir, "*.jpg")))
    if not jpg_files:
        return

    mode_label = "最低解析度/動態30秒取樣" if is_low_quality else "最高畫質"
    print(f"[+] 開始為 [{target_dir}/] 依檔名順序讀取圖片內嵌 EXIF 網址並進行 {mode_label} 下載 (共 {len(jpg_files)} 張預覽圖)...\n")

    success_count = 0
    skipped_count = 0
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
            
        target_video_file = os.path.join(target_dir, video_file_basename)
        dest_downloads_jpg = os.path.join("downloads", image_name)
        os.makedirs("downloads", exist_ok=True)

        if os.path.exists(target_video_file):
            print(f"[{idx}/{len(jpg_files)}] [EXISTS] 影片已存在: {os.path.basename(target_video_file)}")
            if not is_low_quality:
                try:
                    shutil.move(jpg_path, dest_downloads_jpg)
                    print(f"   [Move] 已自動將九宮格圖片移動至 downloads/ 資料夾: {image_name}\n")
                except Exception as e:
                    print(f"   [!] 移動圖片至 downloads/ 失敗: {e}\n")
            else:
                print(f"   [Keep] 九宮格圖片保留於 {target_dir}/ 原位: {image_name}\n")
            skipped_count += 1
            continue

        video_url = get_video_url_from_image(jpg_path)
        video_title = base_name_without_num

        if not video_url:
            print(f"[{idx}/{len(jpg_files)}] [SKIP] 九宮格圖片未內嵌影片 URL Metadata，跳過該圖片: {image_name}\n")
            skipped_count += 1
            continue

        print(f"[{idx}/{len(jpg_files)}] 正在啟動下載 ({mode_label}): {video_title}")
        print(f"   - 圖片 Metadata 讀取網址: {video_url}")
        print(f"   - 輸出路徑: {target_video_file}")

        fmt_spec = 'worstvideo+worstaudio/worst' if is_low_quality else 'bestvideo+bestaudio/best'
        target_dir_abs = os.path.abspath(target_dir)
        temp_dir_abs = os.path.abspath("temp")
        os.makedirs(temp_dir_abs, exist_ok=True)
        temp_thumb_template = os.path.join(temp_dir_abs, f"thumb_{idx}_%(id)s.%(ext)s")

        ydl_opts = {
            'format': fmt_spec,
            'paths': {
                'home': target_dir_abs,
                'temp': temp_dir_abs,
            },
            'outtmpl': {
                'default': video_file_basename,
                'thumbnail': temp_thumb_template
            },
            'quiet': False,
            'no_warnings': True,
        }

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
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
            download_success = True
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
                direct_mp4 = direct_fetch_pornhub_mp4_stream(video_url)
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
                res_ff = subprocess.run(ffmpeg_cmd)
                if res_ff.returncode == 0 and os.path.exists(temp_ffmpeg_file):
                    shutil.move(temp_ffmpeg_file, target_video_file)
                    download_success = True

        if download_success:
            print(f"  [OK] 影片下載成功 -> {os.path.basename(target_video_file)}")
            if not is_low_quality:
                try:
                    shutil.move(jpg_path, dest_downloads_jpg)
                    print(f"   [Move] 已自動將九宮格圖片移動至 downloads/ 資料夾: {image_name}\n")
                except Exception as e:
                    print(f"   [!] 移動圖片至 downloads/ 失敗: {e}\n")
            else:
                print(f"  [Keep] 九宮格圖片保留於 {target_dir}/ 原位: {image_name}\n")
            success_count += 1
        else:
            print(f"  [FAIL] 影片下載失敗: {video_url}\n")

    print(f"[*] [{target_dir}/] 處理完成: 成功下載 {success_count} 部 | 已存在/跳過 {skipped_count} 部")

def run_download_process():
    """主下載流程控制"""
    print(f"==================================================")
    print(f"   Pornhub 雙畫質原影片下載器 (純 EXIF 圖片讀取版)")
    print(f"==================================================")

    os.makedirs("low_videos", exist_ok=True)
    os.makedirs("videos", exist_ok=True)

    low_jpgs = glob.glob(os.path.join("low_videos", "*.jpg"))
    high_jpgs = glob.glob(os.path.join("videos", "*.jpg"))

    if not low_jpgs and not high_jpgs:
        print(f"[!] low_videos/ 與 videos/ 資料夾中均找不到任何被移入的九宮格 JPG 圖片！")
        print(f"[i] 請將預覽圖片移動至 low_videos/ (最低畫質/極速) 或 videos/ (最高畫質) 後再次執行。")
        return

    print(f"[+] 檢測到 low_videos/ ({len(low_jpgs)} 張圖片) | videos/ ({len(high_jpgs)} 張圖片)\n")

    # 【階段一】優先處理 low_videos/ 目錄 (最低畫質)
    if low_jpgs:
        print("==================================================")
        print(" [階段 1/2] 開始處理 low_videos/ (最低解析度/動態30秒取樣)")
        print("==================================================")
        process_single_directory("low_videos", is_low_quality=True)

    # 【階段二】處理完 low_videos/ 後，處理 videos/ 目錄 (最高畫質)
    if high_jpgs:
        print("\n==================================================")
        print(" [階段 2/2] 開始處理 videos/ (最高畫質下載)")
        print("==================================================")
        process_single_directory("videos", is_low_quality=False)

    print("\n==================================================")
    print(f"[ALL DONE] 雙階段下載作業全數完畢！")

if __name__ == "__main__":
    run_download_process()

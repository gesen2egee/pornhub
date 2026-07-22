import os
import sys
import re
import io
import json
import glob
import shutil
import subprocess
import numpy as np
from PIL import Image
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
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0',
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

def find_info_in_map(image_name, preview_map):
    """4 級雙向容錯查詢"""
    if image_name in preview_map:
        return preview_map[image_name]
        
    no_num = re.sub(r'^\d{4}-', '', image_name)
    if no_num in preview_map:
        return preview_map[no_num]
        
    clean_title = os.path.splitext(no_num)[0]
    for k, v in preview_map.items():
        if isinstance(v, dict):
            if v.get("title") == clean_title or v.get("title") == os.path.splitext(image_name)[0]:
                return v
    return None

def is_frame_border_solid(img, threshold_ratio=0.5):
    """分析影格四個邊緣是否大多數 (> 50%) 為同一數值純色/黑邊"""
    if not img:
        return False
    try:
        arr = np.array(img)
        h, w, _ = arr.shape
        top_row, bottom_row = arr[0, :, :], arr[h-1, :, :]
        left_col, right_col = arr[:, 0, :], arr[:, w-1, :]
        
        solid_borders_count = 0
        for border in [top_row, bottom_row, left_col, right_col]:
            pixels = [tuple(p) for p in border]
            if not pixels:
                continue
            most_common_cnt = max(pixels.count(p) for p in set(pixels))
            if (most_common_cnt / len(pixels)) >= threshold_ratio:
                solid_borders_count += 1
        return solid_borders_count >= 2
    except Exception:
        return False

def on_the_fly_stream_download_and_crop(stream_url, http_headers, target_video_file):
    """單管道即時串流下載與邊下載邊檢測"""
    user_agent = http_headers.get('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0')
    headers_list = [f"{k}: {v}\r\n" for k, v in http_headers.items() if k.lower() != 'user-agent']
    headers_str = "".join(headers_list)

    cmd_check = ["ffmpeg", "-y", "-loglevel", "error"]
    if '.m3u8' in stream_url.lower() or 'hls' in stream_url.lower():
        cmd_check.extend([
            "-extension_picky", "0",
            "-allowed_segment_extensions", "ALL,none,*",
            "-allowed_extensions", "ALL,none,*",
            "-protocol_whitelist", "file,crypto,stream,httpproxy,http,https,tcp,tls,rtp,hls",
        ])
    cmd_check.extend(["-user_agent", user_agent])
    if headers_str:
        cmd_check.extend(["-headers", headers_str])

    cmd_10s = cmd_check + ["-ss", "10.0", "-i", stream_url, "-vframes", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    cut_seconds = 0.0
    try:
        res_10s = subprocess.run(cmd_10s, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)
        if res_10s.stdout:
            img_10s = Image.open(io.BytesIO(res_10s.stdout)).convert("RGB")
            if is_frame_border_solid(img_10s):
                cut_seconds = 0.0
            else:
                cmd_1s = cmd_check + ["-ss", "1.0", "-i", stream_url, "-vframes", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
                res_1s = subprocess.run(cmd_1s, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)
                if res_1s.stdout:
                    img_1s = Image.open(io.BytesIO(res_1s.stdout)).convert("RGB")
                    if is_frame_border_solid(img_1s):
                        low, high = 1.0, 10.0
                        for _ in range(3):
                            mid = (low + high) / 2.0
                            cmd_mid = cmd_check + ["-ss", str(mid), "-i", stream_url, "-vframes", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
                            res_m = subprocess.run(cmd_mid, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)
                            if res_m.stdout and is_frame_border_solid(Image.open(io.BytesIO(res_m.stdout)).convert("RGB")):
                                low = mid
                            else:
                                high = mid
                        cut_sec = round(low, 1)
                        if 0.5 <= cut_sec <= 10.0:
                            cut_seconds = cut_sec
    except Exception:
        cut_seconds = 0.0

    return cut_seconds

def run_download_process(videos_dir="videos", map_json_path="preview_map.json"):
    """
    掃描 videos/ 資料夾中被移入的九宮格圖片，
    發起單管道即時串流下載，若檢測到 410 錯誤提示使用者執行 pip 指令升級。
    """
    print("==================================================")
    print("        Pornhub 最高畫質原影片下載器 (run_download)")
    print("==================================================")
    print()

    target_json_path = map_json_path
    if not os.path.exists(target_json_path):
        if os.path.exists(os.path.join("previews", "preview_map.json")):
            target_json_path = os.path.join("previews", "preview_map.json")
        elif os.path.exists(os.path.join("downloads", "preview_map.json")):
            target_json_path = os.path.join("downloads", "preview_map.json")

    if not os.path.exists(target_json_path):
        print(f"[!] 錯誤: 找不到網址對照檔 {map_json_path}！請先執行截圖工具產出九宮格圖片。")
        return

    try:
        with open(target_json_path, "r", encoding="utf-8") as f:
            preview_map = json.load(f)
        print(f"[+] 成功載入網址對照檔: {target_json_path}")
    except Exception as e:
        print(f"[!] 讀取 {target_json_path} 失敗: {e}")
        return

    os.makedirs(videos_dir, exist_ok=True)
    jpg_files = glob.glob(os.path.join(videos_dir, "*.jpg"))
    if not jpg_files:
        print(f"[!] {videos_dir}/ 資料夾中找不到任何被移入的九宮格 JPG 圖片！")
        print(f"[i] 請將滿意的九宮格圖片從 previews/ 或 downloads/ 移動至 videos/ 資料夾後再次執行。")
        return

    print(f"[+] 於 {videos_dir}/ 資料夾中掃描到 {len(jpg_files)} 張被移入的九宮格預覽圖。")
    print(f"[+] 開始最高畫質下載原影片...\n")

    success_count = 0
    skipped_count = 0
    prompted_upgrade = False

    for idx, jpg_path in enumerate(jpg_files, 1):
        image_name = os.path.basename(jpg_path)
        base_name_without_num = re.sub(r'^\d{4}-', '', os.path.splitext(image_name)[0])
        target_video_file = os.path.join(videos_dir, f"{base_name_without_num}.mp4")
        dest_downloads_jpg = os.path.join("downloads", image_name)
        os.makedirs("downloads", exist_ok=True)

        if os.path.exists(target_video_file):
            print(f"[{idx}/{len(jpg_files)}] [EXISTS] 影片已存在: {os.path.basename(target_video_file)}")
            try:
                shutil.move(jpg_path, dest_downloads_jpg)
                print(f"   [Move] 已自動將九宮格圖片移動至 downloads/ 資料夾: {image_name}\n")
            except Exception as e:
                print(f"   [!] 移動圖片至 downloads/ 失敗: {e}\n")
            skipped_count += 1
            continue

        info = find_info_in_map(image_name, preview_map)
        if not info or not info.get("url"):
            print(f"[{idx}/{len(jpg_files)}] [!] 找不到 {image_name} 的對應 URL mapping，跳過。\n")
            continue

        video_url = info.get("url")
        video_title = info.get("title", base_name_without_num)

        print(f"[{idx}/{len(jpg_files)}] 正在啟動下載: {video_title}")
        print(f"   - 網頁網址: {video_url}")

        ydl_opts = {
            'format': 'bestvideo+bestaudio/best',
            'outtmpl': target_video_file,
            'quiet': False,
            'no_warnings': True,
        }

        # 嘗試下載
        download_success = False
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
            download_success = True
        except Exception as e:
            err_str = str(e)
            if "410" in err_str or "Gone" in err_str:
                if not prompted_upgrade:
                    print("=" * 65)
                    print("[!] 警告: 本機的 yt-dlp 套件版本過舊，觸發了 HTTP Error 410 錯誤！")
                    print("[!] 請在控制台 (CMD/PowerShell) 執行以下指令進行升級：")
                    print("    pip install --upgrade yt-dlp")
                    print("    或執行: pip install -r requirements.txt --upgrade")
                    print("=" * 65)
                    prompted_upgrade = True
                
                print(f"   [FALLBACK] 嘗試啟動原生備用解析器繞過 410 錯誤...")
                direct_mp4 = direct_fetch_pornhub_mp4_stream(video_url)
                if direct_mp4:
                    print(f"   [+] 成功解析直連 MP4 串流，發起 FFmpeg 極速下載...")
                    ffmpeg_cmd = ["ffmpeg", "-y", "-i", direct_mp4, "-c", "copy", target_video_file]
                    res_ff = subprocess.run(ffmpeg_cmd)
                    if res_ff.returncode == 0:
                        download_success = True

        if download_success:
            print(f"  [OK] 影片下載成功 -> {os.path.basename(target_video_file)}")
            try:
                shutil.move(jpg_path, dest_downloads_jpg)
                print(f"  [Move] 已將九宮格圖片移動至 downloads/: {image_name}\n")
            except Exception:
                pass
            success_count += 1
        else:
            print(f"  [FAIL] 影片下載失敗: {video_url}\n")

    print("==================================================")
    print(f"[DONE] 下載作業全數完成！成功: {success_count} 部 | 已存在/跳過: {skipped_count} 部")
    print(f"[+] 影片已儲存在: {os.path.abspath(videos_dir)}")
    print("==================================================")

if __name__ == "__main__":
    run_download_process()

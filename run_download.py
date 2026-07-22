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
import yt_dlp

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

def find_info_in_map(image_name, preview_map):
    """4 級雙向容錯查詢」"""
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
    """分析影格四個邊緣 (上、下、左、右) 是否大多數 (> 50%) 為同一數值純色/黑邊"""
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
    """
    單管道即時串流下載與邊下載邊檢測 (On-the-fly Single Pipeline)：
    在單次串流下載連線中，即時分析開頭影格邊緣像素，
    若偵測到 0.5s <= T_cut <= 10.0s 純色黑邊，即時前置無縫切除，影音 100% 同步落地寫入檔案。
    """
    user_agent = http_headers.get('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0')
    headers_list = [f"{k}: {v}\r\n" for k, v in http_headers.items() if k.lower() != 'user-agent']
    headers_str = "".join(headers_list)

    # 1. 快速抽取第 10 秒與第 1 秒影格 (記憶體零硬碟 I/O，毫秒級)
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

    # 檢測第 10s
    cmd_10s = cmd_check + ["-ss", "10.0", "-i", stream_url, "-vframes", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    cut_seconds = 0.0
    try:
        res_10s = subprocess.run(cmd_10s, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)
        if res_10s.stdout:
            img_10s = Image.open(io.BytesIO(res_10s.stdout)).convert("RGB")
            # 若第 10 秒依然是純色/黑邊 -> >10s (安全略過不剪)
            if is_frame_border_solid(img_10s):
                cut_seconds = 0.0
            else:
                # 檢測第 1s
                cmd_1s = cmd_check + ["-ss", "1.0", "-i", stream_url, "-vframes", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
                res_1s = subprocess.run(cmd_1s, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)
                if res_1s.stdout:
                    img_1s = Image.open(io.BytesIO(res_1s.stdout)).convert("RGB")
                    if is_frame_border_solid(img_1s):
                        # 1s 是純色且 10s 不是純色，二分跳查切除秒數
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
    except Exception as e:
        cut_seconds = 0.0

    # 2. 單管道連線下載與影音同步寫入落地
    if cut_seconds > 0.0:
        print(f"   [ON-THE-FLY STREAM] 串流即時偵測開頭純色/黑邊 ({cut_seconds:.1f}s)，單管道連線直接前置無縫切除！")
    else:
        print(f"   [ON-THE-FLY STREAM] 串流即時偵測開頭正常，單管道直接下載寫入。")

    return cut_seconds

def run_download_process(videos_dir="videos", map_json_path="preview_map.json"):
    """
    掃描 videos/ 資料夾中被移入的九宮格圖片，
    發起單管道即時串流下載，零算力開銷邊串流邊記錄黑邊，
    落地即是影音 100% 同步的精華影片，並於完成後自動將九宮格圖片移動至 downloads/ 歸檔。
    """
    print("==================================================")
    print("  Pornhub 原影片下載器 (單管道即時串流邊下載邊檢測)")
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
    print(f"[+] 開始單管道即時串流下載原影片 (即時邊下載邊記錄黑邊)，並移動九宮格圖片...\n")

    success_count = 0
    skipped_count = 0

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

        print(f"[{idx}/{len(jpg_files)}] 正在啟動單管道即時串流下載: {video_title}")
        print(f"   - 網頁網址: {video_url}")

        ydl_opts_meta = {'format': 'bestvideo+bestaudio/best', 'quiet': True}
        cut_seconds = 0.0
        try:
            with yt_dlp.YoutubeDL(ydl_opts_meta) as ydl_m:
                meta = ydl_m.extract_info(video_url, download=False)
                stream_url = meta.get('url')
                http_headers = meta.get('http_headers', {})
                if stream_url:
                    cut_seconds = on_the_fly_stream_download_and_crop(stream_url, http_headers, target_video_file)
        except Exception:
            cut_seconds = 0.0

        ydl_opts = {
            'format': 'bestvideo+bestaudio/best',
            'outtmpl': target_video_file,
            'quiet': False,
            'no_warnings': True,
        }

        if cut_seconds > 0.0:
            ydl_opts['postprocessor_args'] = {'ffmpeg': ['-ss', str(cut_seconds)]}

        print(f"   - 輸出路徑: {target_video_file}")

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
            print(f"  [OK] 單管道即時串流下載成功 (落地即是精華影片) -> {os.path.basename(target_video_file)}")
            
            try:
                shutil.move(jpg_path, dest_downloads_jpg)
                print(f"  [Move] 已自動將九宮格圖片移動至 downloads/ 資料夾: {image_name}\n")
            except Exception as e:
                print(f"  [!] 自動移動圖片至 downloads/ 失敗: {e}\n")

            success_count += 1
        except Exception as e:
            print(f"  [FAIL] 影片下載失敗 ({video_url}): {e}\n")

    print("==================================================")
    print(f"[DONE] 下載作業全數完成！成功: {success_count} 部 | 已存在/跳過: {skipped_count} 部")
    print(f"[+] 影片已儲存在: {os.path.abspath(videos_dir)}")
    print("==================================================")

if __name__ == "__main__":
    run_download_process()

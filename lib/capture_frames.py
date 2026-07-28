import os
import sys
import re
import json
import argparse
import subprocess
import concurrent.futures
import urllib.request
import urllib.parse
import shutil
import datetime
import video_meta
import sites
from multilabel_tagger import DEFAULT_REPO_ID, MobileNetV4Tagger, score_for
from project_paths import PREVIEW_IMAGES_DIR, TEMP_DIR
from PIL import Image, ImageDraw, ImageFont, ImageChops, ImageStat

EPORNER_DEFAULT_URL = sites.EPORNER_DEFAULT_URL
DEFAULT_PREVIEW_OUTPUT = str(PREVIEW_IMAGES_DIR)

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

def log_event(text, output_dir=DEFAULT_PREVIEW_OUTPUT):
    """寫入 log 紀錄至 process.log"""
    os.makedirs(output_dir, exist_ok=True)
    log_file = os.path.join(output_dir, "process.log")
    timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    divider = "=" * 80
    formatted_entry = f"{divider}\n[{timestamp_str}]\n{text}\n"
    
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(formatted_entry)
    except Exception as e:
        print(f"[!] 寫入 Log 失敗: {e}")

def get_start_page_from_url(url):
    """依站點 adapter 解析起始頁碼。"""
    adapter = sites.get_adapter_for_url(url or "")
    return adapter.get_start_page(url or "")

def generate_output_folder_name(
    target,
    pages=1,
    base_output_dir=DEFAULT_PREVIEW_OUTPUT,
):
    """根據時間、搜尋關鍵字/URL 標籤及頁數區間生成動態子資料夾名稱"""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    start_page = get_start_page_from_url(target)
    end_page = start_page + max(1, int(pages)) - 1
    page_tag = f"p{start_page}" if start_page == end_page else f"p{start_page}-{end_page}"
    tag = sites.folder_tag_for_target(target).strip("_")
    folder_name = f"{timestamp}_{tag}_{page_tag}"
    full_path = os.path.join(base_output_dir, folder_name)
    os.makedirs(full_path, exist_ok=True)
    return full_path

def format_time(seconds):
    """將秒數格式化為 00:00 或 00:00:00 格式"""
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def calculate_grid_timestamps(duration, grid_size):
    """避開片頭與片尾，在影片中後段均勻選取指定宮格的時間點。"""
    duration = float(duration)
    if duration <= 30:
        start = max(1.0, duration * 0.1)
        end = max(start + 1.0, duration * 0.9)
    else:
        start = max(15.0, duration * 0.08)
        end = duration * 0.95
        
    count = grid_size * grid_size
    step = (end - start) / max(1, count - 1)
    timestamps = [start + i * step for i in range(count)]
    return [int(ts) for ts in timestamps]

def build_page_url(url, page_num):
    """依站點 adapter 構建分頁 URL。"""
    adapter = sites.get_adapter_for_url(url)
    return adapter.build_page_url(url, page_num)

def extract_single_page_urls(target_url):
    """提取單一網頁主要列表中的影片網址"""
    print(f"[*] 正在分析網頁，擷取頁面中的影片清單: {target_url} ...")
    adapter = sites.get_adapter_for_url(target_url)
    urls = adapter.extract_list_urls(target_url)
    if urls:
        print(f"[+] 透過 {adapter.name} 成功分析出 {len(urls)} 部影片！")
        return urls
    print(f"[!] 網頁影片清單解析失敗: {adapter.name}")
    if adapter.is_single_video_url(target_url):
        return [target_url]
    return []


def extract_urls_from_target(target, pages=1):
    """從目標 (網頁 URL、關鍵字搜尋、檔案或單一影片 URL) 解析出所有影片 URL 列表 (支援多頁爬取)"""
    return sites.extract_urls_from_target(target, pages=pages)

def extract_video_info(video_url, quality="720p"):
    """透過共用 resolve_playable 獲取時長、標題、串流 URL、Headers 與 WEB_META"""
    if quality == "best":
        fmt = "bestvideo[protocol!=m3u8_native]/best[protocol!=m3u8_native]/bestvideo/best"
    elif quality.endswith('p') and quality[:-1].isdigit():
        h = quality[:-1]
        fmt = f"bestvideo[height<={h}][protocol!=m3u8_native]/best[height<={h}][protocol!=m3u8_native]/bestvideo[height<={h}]/best"
    else:
        fmt = quality

    print(f"[*] 正在解析影片資訊 ({quality} 畫質): {video_url} ...")
    adapter = sites.get_adapter_for_url(video_url)
    resolved = sites.resolve_playable(
        adapter,
        video_url,
        purpose="info",
        prefer_lowest=False,
        base_ydl_opts={"format": fmt},
    )
    info = resolved.get("info") or {}
    info.setdefault("webpage_url", video_url)
    stream_url = resolved.get("stream_url")
    http_headers = resolved.get("http_headers") or {}

    duration = info.get("duration")
    title = info.get("title", "video_frames")
    format_id = info.get("format_id", "unknown")
    ext = info.get("ext", "unknown")
    protocol = info.get("protocol", "unknown")
    width = info.get("width", 0)
    height = info.get("height", 0)
    vcodec = info.get("vcodec", "unknown")
    acodec = info.get("acodec", "unknown")

    diagnostic_info = {
        "format_id": format_id,
        "ext": ext,
        "protocol": protocol,
        "resolution": f"{width}x{height}",
        "codecs": f"{vcodec}/{acodec}",
        "source": resolved.get("source"),
    }

    if not stream_url:
        raise ValueError("無法獲取影片串流 URL")

    return {
        "title": title,
        "duration": duration,
        "stream_url": stream_url,
        "http_headers": http_headers,
        "diagnostic_info": diagnostic_info,
        "web_meta": video_meta.build_web_meta(info),
    }

def capture_single_frame(timestamp, stream_url, http_headers, output_file):
    """執行 ffmpeg 雙重 Seek 截取指定秒數的單張畫面。"""
    os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
    
    is_local_file = os.path.exists(stream_url) or not (stream_url.startswith("http://") or stream_url.startswith("https://"))
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    
    if not is_local_file:
        user_agent = http_headers.get('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0')
        headers_list = []
        for k, v in http_headers.items():
            if k.lower() != 'user-agent':
                headers_list.append(f"{k}: {v}\r\n")
        headers_str = "".join(headers_list)
        
        if '.m3u8' in stream_url.lower() or 'hls' in stream_url.lower():
            cmd.extend([
                "-extension_picky", "0",
                "-allowed_segment_extensions", "ALL,none,*",
                "-allowed_extensions", "ALL,none,*",
                "-protocol_whitelist", "file,crypto,stream,httpproxy,http,https,tcp,tls,rtp,hls",
            ])
            
        cmd.extend(["-user_agent", user_agent])
        if headers_str:
            cmd.extend(["-headers", headers_str])
        
    if timestamp >= 10:
        pre_seek = timestamp - 10
        post_seek = 10
        cmd.extend(["-ss", str(pre_seek), "-i", stream_url, "-ss", str(post_seek)])
    else:
        cmd.extend(["-i", stream_url, "-ss", str(timestamp)])
        
    cmd.extend([
        "-frames:v", "1",
        "-strict", "unofficial",
        "-pix_fmt", "yuvj420p",
        "-update", "1",
        "-q:v", "2",
        output_file
    ])
    
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return True, timestamp, output_file
    except subprocess.CalledProcessError as e:
        return False, timestamp, e.stderr

def check_images_has_duplicates(pil_images, threshold=2.5):
    """
    檢查傳入的 PIL Image 列表中是否有重複或高度相似的圖片
    threshold: 像素平均絕對差值閾值，低於此值認定為重複圖片
    """
    num_imgs = len(pil_images)
    for i in range(num_imgs):
        for j in range(i + 1, num_imgs):
            ts1, img1 = pil_images[i]
            ts2, img2 = pil_images[j]
            
            if img1.size != img2.size:
                img2_resized = img2.resize(img1.size)
            else:
                img2_resized = img2
                
            diff = ImageChops.difference(img1, img2_resized)
            stat = ImageStat.Stat(diff)
            diff_score = sum(stat.mean) / len(stat.mean)
            
            if diff_score < threshold:
                return True, i, j, ts1, ts2, diff_score
                
    return False, -1, -1, 0, 0, 0.0

def create_grid_image(image_data_list, title, duration, video_url, stream_url, diag_info, output_file, grid_size, output_root=DEFAULT_PREVIEW_OUTPUT, web_meta=None):
    """
    將指定數量截圖合成宮格圖片，並標註時間標籤。
    """
    valid_images = [(ts, p) for ts, p in image_data_list if os.path.exists(p)]
    expected_count = grid_size * grid_size
    if len(valid_images) < expected_count:
        msg = f"[FAIL] 圖片擷取不足 {expected_count} 張 (成功: {len(valid_images)} 張) - 影片: {title} ({video_url})"
        print(f"\n[!] {msg}")
        log_event(msg, output_dir=output_root)
        return False

    pil_images = []
    for ts, path in valid_images:
        try:
            img = Image.open(path).convert("RGB")
            pil_images.append((ts, img))
        except Exception as e:
            print(f"[!] 讀取圖片失敗 {path}: {e}")

    if not pil_images:
        return False

    # 自動檢查重複圖片
    has_dup, idx1, idx2, ts1, ts2, score = check_images_has_duplicates(pil_images)
    if has_dup:
        similarity_pct = max(0.0, min(100.0, (1.0 - (score / 255.0)) * 100.0))
        truncated_stream_url = stream_url[:120] + "..." if len(stream_url) > 120 else stream_url
        
        dup_log_text = (
            f"[SKIP] 影片任務跳過 (檢測到畫面重複)\n"
            f"  - 影片標題: {title}\n"
            f"  - 影片網址: {video_url}\n"
            f"  - 串流診斷: Format ID: [{diag_info.get('format_id')}] | Ext: [{diag_info.get('ext')}] | Protocol: [{diag_info.get('protocol')}] | Res: [{diag_info.get('resolution')}] | Codecs: [{diag_info.get('codecs')}]\n"
            f"  - 串流網址: {truncated_stream_url}\n"
            f"  - 重複狀況: 第 {idx1+1} 張 [{format_time(ts1)}] 與 第 {idx2+1} 張 [{format_time(ts2)}] 畫面重複\n"
            f"  - 畫面相似度: {similarity_pct:.2f}% (像素差異分: {score:.2f} / 臨界值: 2.50)\n"
            f"  - 處理動作: 已自動清理暫存檔，跳過不輸出任何圖片檔。"
        )
        print(f"\n{dup_log_text}")
        log_event(dup_log_text, output_dir=output_root)
        return False

    sample_img = pil_images[0][1]
    w, h = sample_img.size

    padding = 10
    header_h = 70
    bg_color = (20, 20, 20)

    canvas_w = grid_size * w + (grid_size + 1) * padding
    canvas_h = grid_size * h + (grid_size + 1) * padding + header_h

    grid_img = Image.new("RGB", (canvas_w, canvas_h), color=bg_color)
    draw = ImageDraw.Draw(grid_img)

    try:
        title_font = ImageFont.truetype("arial.ttf", 26)
        sub_font = ImageFont.truetype("arial.ttf", 18)
        tag_font = ImageFont.truetype("arial.ttf", 56)
    except IOError:
        title_font = ImageFont.load_default()
        sub_font = ImageFont.load_default()
        tag_font = ImageFont.load_default()

    display_title = title if len(title) <= 60 else title[:57] + "..."
    dur_str = format_time(duration)
    header_text = f"Title: {display_title}"
    info_text = f"Duration: {dur_str} | {grid_size}x{grid_size} Preview Sheet"

    draw.text((padding + 5, 12), header_text, fill=(255, 204, 0), font=title_font)
    draw.text((padding + 5, 42), info_text, fill=(180, 180, 180), font=sub_font)

    # 貼上 9 張圖片，並在「右上角」繪製「超大鮮綠色標籤 + 3px 黑色邊框」
    green_color = (0, 255, 64)
    stroke_color = (0, 0, 0)
    stroke_width = 3

    for idx, (ts, img) in enumerate(pil_images[:expected_count]):
        row = idx // grid_size
        col = idx % grid_size
        x = padding + col * (w + padding)
        y = header_h + padding + row * (h + padding)

        grid_img.paste(img, (x, y))

        time_tag = format_time(ts)
        
        # 計算字體大小以對齊右上角
        bbox = draw.textbbox((0, 0), time_tag, font=tag_font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        # 放在子格圖片的「右上角」 (Margin 15px)
        tag_x = x + w - text_w - 20
        tag_y = y + 15

        # 繪製 8 方向 3px 黑色描邊外框
        for dx in range(-stroke_width, stroke_width + 1):
            for dy in range(-stroke_width, stroke_width + 1):
                if dx != 0 or dy != 0:
                    draw.text((tag_x + dx, tag_y + dy), time_tag, fill=stroke_color, font=tag_font)

        # 繪製超大鮮綠色主字體
        draw.text((tag_x, tag_y), time_tag, fill=green_color, font=tag_font)

    exif = grid_img.getexif()
    exif[0x010e] = video_url  # 將影片網址寫入 JPG 圖片 EXIF Metadata (ImageDescription)
    grid_img.save(output_file, quality=95, exif=exif)
    if web_meta is not None:
        video_meta.write_grid_jpg_web_meta(output_file, web_meta, url=video_url)

    succ_log_text = (
        f"[SUCCESS] {grid_size}x{grid_size} 宮格圖片生成成功\n"
        f"  - 影片標題: {title}\n"
        f"  - 影片長度: {dur_str}\n"
        f"  - 影片網址: {video_url}\n"
        f"  - 串流診斷: Format ID: [{diag_info.get('format_id')}] | Protocol: [{diag_info.get('protocol')}] | Res: [{diag_info.get('resolution')}]\n"
        f"  - 輸出檔案: {output_file}\n"
        f"  - {expected_count}張時間點: {', '.join([format_time(ts) for ts, _ in pil_images[:expected_count]])}"
    )
    log_event(succ_log_text, output_dir=output_root)
    return True

def summarize_tag_batch(image_paths, tagger):
    """以 batch size 5 取得每張畫面最可能的 RATING 與 smile 信心值。"""
    results = []
    for image_path, tags in zip(image_paths, tagger.predict_many(image_paths)):
        top_rating = max(tags["rating"].items(), key=lambda item: item[1])[0] if tags["rating"] else ""
        results.append((image_path, top_rating, score_for(tags["general"], "smile")))
    return results


def process_single_video(video_url, args, tagger, index=1, total=1):
    """下載 25 張畫面，同時 TAGGER 篩選；全部通過才輸出 5x5 宮格。"""
    print(f"\n==================================================")
    print(f"[{index}/{total}] 開始處理影片: {video_url}")
    print(f"==================================================")
    
    try:
        info = extract_video_info(video_url, args.quality)
    except Exception as e:
        fail_msg = f"[FAIL] 解析影片資訊失敗: {video_url} - 錯誤: {e}"
        print(f"[!] {fail_msg}")
        log_event(fail_msg, output_dir=args.output)
        return False

    title = info['title']
    duration = info['duration']
    stream_url = info['stream_url']
    http_headers = info['http_headers']
    diag_info = info['diagnostic_info']
    
    if not duration:
        fail_msg = f"[FAIL] 無法取得影片總長度: {video_url}"
        print(f"[!] {fail_msg}")
        log_event(fail_msg, output_dir=args.output)
        return False

    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '_', '-')).rstrip()
    os.makedirs(args.output, exist_ok=True)
    
    # 檔名加入 4 位數順序編號 (例如 0001-影片標題.jpg)
    final_output_file = os.path.join(args.output, f"{index:04d}-{safe_title}.jpg")
    
    temp_dir = os.path.join(str(TEMP_DIR), f".temp_{safe_title}")
    os.makedirs(temp_dir, exist_ok=True)

    timestamps = calculate_grid_timestamps(duration, args.grid_size)
    expected_count = args.grid_size * args.grid_size
    
    print(f"[+] 影片標題: {title}")
    print(f"[+] 影片長度: {duration} 秒 ({format_time(duration)})")
    print(f"[+] 串流格式: Format ID: [{diag_info['format_id']}] | Protocol: [{diag_info['protocol']}] | Res: [{diag_info['resolution']}]")
    print(f"[+] 避開片頭全片平均選取 {expected_count} 個時間點: {[format_time(t) for t in timestamps]}")
    print(f"[+] 開始 {args.workers} 線程同步擷取；TAGGER 每批 5 張立即以 GPU 判斷 ...")

    captured_image_data = []
    tag_futures = []
    pending_tag_images = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as tag_executor:
            for idx, ts in enumerate(timestamps):
                temp_file_name = f"frame_{idx+1:02d}_{ts}s.jpg"
                temp_file_path = os.path.join(temp_dir, temp_file_name)
                future = executor.submit(capture_single_frame, ts, stream_url, http_headers, temp_file_path)
                futures[future] = (ts, temp_file_path)

            for future in concurrent.futures.as_completed(futures):
                ts, file_path = futures[future]
                success, timestamp, res = future.result()
                if success:
                    captured_image_data.append((timestamp, file_path))
                    pending_tag_images.append((timestamp, file_path))
                    if len(pending_tag_images) == args.tagger_batch_size:
                        batch = pending_tag_images
                        pending_tag_images = []
                        tag_futures.append((batch, tag_executor.submit(
                            summarize_tag_batch, [path for _, path in batch], tagger
                        )))
                        print(f"  [OK] 已累積 5 張，送入 GPU TAGGER 批次判斷。")
                    else:
                        print(f"  [OK] 畫面擷取成功，等待湊滿 5 張 TAGGER 批次: {format_time(timestamp)}")
                else:
                    print(f"  [FAIL] 畫面擷取失敗: {format_time(timestamp)} - 錯誤: {res.strip()}")

            if pending_tag_images:
                tag_futures.append((pending_tag_images, tag_executor.submit(
                    summarize_tag_batch, [path for _, path in pending_tag_images], tagger
                )))
            tag_results = []
            tag_errors = []
            for batch, future in tag_futures:
                try:
                    results = future.result()
                except Exception as exc:
                    tag_errors.extend((timestamp, f"TAGGER 錯誤: {exc}") for timestamp, _ in batch)
                    continue
                timestamps_by_path = {path: timestamp for timestamp, path in batch}
                tag_results.extend((timestamps_by_path[path], rating, smile_score) for path, rating, smile_score in results)

    if tag_errors:
        print(f"[SKIP] TAGGER 有 {len(tag_errors)} 張錯誤，整個 {args.grid_size}x{args.grid_size} 宮格不儲存。")
        for timestamp, reason in tag_errors:
            print(f"  [FILTER] {format_time(timestamp)}：{reason}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False

    general_count = sum(rating.strip().lower() == "general" for _, rating, _ in tag_results)
    smile_count = sum(score >= args.smile_min_confidence for _, _, score in tag_results)
    print(f"[TAGGER] 最可能 RATING=general：{general_count} 張（需少於 {args.max_general_count + 1} 張）；smile：{smile_count} 張（需介於 {args.min_smile_count}～{args.max_smile_count} 張）。")
    if general_count > args.max_general_count or not args.min_smile_count <= smile_count <= args.max_smile_count:
        print(f"[SKIP] 宮格統計條件未通過，整個 {args.grid_size}x{args.grid_size} 宮格不儲存。")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False

    captured_image_data.sort(key=lambda x: x[0])

    print(f"\n[*] 全部 {expected_count} 張通過，正在進行重複圖片檢測與 {args.grid_size}x{args.grid_size} 宮格合成 ...")
    success = create_grid_image(
        captured_image_data, title, duration, video_url, stream_url, diag_info,
        final_output_file, args.grid_size, output_root=args.output, web_meta=info.get('web_meta')
    )

    shutil.rmtree(temp_dir, ignore_errors=True)

    if success:
        print(f"[DONE] 成功產出 {args.grid_size}x{args.grid_size} 宮格圖片！")
        print(f"[+] 檔案儲存路徑: {os.path.abspath(final_output_file)}")
        return True
    else:
        print(f"[SKIP] 任務已跳過 (已將詳細診斷寫入 Log，不產生圖片檔)。")
        return False

def main():
    parser = argparse.ArgumentParser(description="影片 5x5 宮格定時截圖工具（下載時同步 GPU TAGGER 篩選）")
    parser.add_argument("target", nargs="?", default=EPORNER_DEFAULT_URL, help="影片網址、網站列表/分類/搜尋 URL、Eporner 關鍵字或包含網址的 txt 檔案路徑")
    parser.add_argument("-p", "--pages", type=int, default=1, help="連續擷取的頁數 (預設: 1 頁)")
    parser.add_argument("-q", "--quality", default="480p", help="畫質選擇（預設 480p，可選 best、1080p、720p 等）")
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_PREVIEW_OUTPUT,
        help="輸出根目錄（預設：output/01_preview_images）",
    )
    parser.add_argument("-w", "--workers", type=int, default=25, help="每部影片並行截圖線程數（預設 25）")
    parser.add_argument("-m", "--max-videos", type=int, default=0, help="最多處理的影片數量 (預設: 0 代表無限制，全數處理)")
    parser.add_argument("--grid-size", type=int, default=5, choices=(5,), help="宮格邊長（固定 5，合計 25 張）")
    parser.add_argument("--max-general-count", type=int, default=4, help="最可能 RATING=general 的最多張數（預設 4）")
    parser.add_argument("--min-smile-count", type=int, default=2, help="smile TAG 的最少張數（預設 2）")
    parser.add_argument("--max-smile-count", type=int, default=8, help="smile TAG 的最多張數（預設 8）")
    parser.add_argument("--smile-min-confidence", type=float, default=0.25, help="smile TAG 信心值門檻（0~1，預設 0.25，與 Trainer GENERAL 門檻相同）")
    parser.add_argument("--tagger-repo", default=DEFAULT_REPO_ID, help="Hugging Face TAGGER repo")
    parser.add_argument("--tagger-batch-size", type=int, default=5, choices=(5,), help="TAGGER GPU 批次大小（固定 5）")
    
    args = parser.parse_args()
    
    target_url = args.target.strip() if (args.target and args.target.strip()) else EPORNER_DEFAULT_URL
    
    # 自動產生時間 + 關鍵字/網址標籤 + 頁碼區間的子資料夾
    sub_output_dir = generate_output_folder_name(target_url, pages=args.pages, base_output_dir=args.output)
    args.output = sub_output_dir
    print(f"[*] 5x5 截圖將儲存至專屬子目錄: {args.output}")
    
    video_urls = extract_urls_from_target(target_url, pages=args.pages)
    
    if not video_urls:
        print("[!] 找不到任何可處理的影片網址。")
        sys.exit(1)
        
    if args.max_videos > 0 and len(video_urls) > args.max_videos:
        print(f"[*] 設定最多處理 {args.max_videos} 部影片 (原始發現 {len(video_urls)} 部)")
        video_urls = video_urls[:args.max_videos]
        
    total_videos = len(video_urls)
    print(f"\n[START] 載入 MobileNetV4 TAGGER 至 GPU，並執行 5x5（25 張）批次作業，共計 {total_videos} 部影片...")
    tagger = MobileNetV4Tagger(args.tagger_repo)
    tagger.preload()
    log_event(f"[BATCH START] 啟動批次作業，目標網址: {args.target}，頁數: {args.pages}，預計處理影片數: {total_videos}，輸出目錄: {args.output}", output_dir=args.output)
    
    completed_videos = 0
    skipped_videos = 0
    for idx, url in enumerate(video_urls, 1):
        if process_single_video(url, args, tagger, index=idx, total=total_videos):
            completed_videos += 1
        else:
            skipped_videos += 1
            
    summary_text = f"[BATCH DONE] 批次作業全數完成！成功: {completed_videos} 部 | 跳過/失敗: {skipped_videos} 部"
    print(f"\n[ALL DONE] {summary_text}")
    log_event(summary_text, output_dir=args.output)

if __name__ == "__main__":
    main()

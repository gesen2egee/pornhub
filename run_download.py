import os
import sys
import re
import json
import glob
import yt_dlp

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

def run_download_process(videos_dir="videos", map_json_path="preview_map.json"):
    """
    掃描 videos/ 資料夾中被移入的九宮格圖片 (0001-影片標題.jpg)，查詢 preview_map.json 取得原始網址，
    使用 yt-dlp 最高畫質下載原始影片至 videos/ 資料夾 (下載檔名為原名: 影片標題.mp4)，
    並於下載完成後自動刪除該張九宮格 JPG 圖檔。
    """
    print("==================================================")
    print("        Pornhub 最高畫質原影片下載器 (run_download)")
    print("==================================================")
    print()

    # 1. 檢查 preview_map.json 是否存在
    if not os.path.exists(map_json_path):
        print(f"[!] 錯誤: 找不到網址對照檔 {map_json_path}！請先執行截圖工具產出九宮格圖片。")
        return

    try:
        with open(map_json_path, "r", encoding="utf-8") as f:
            preview_map = json.load(f)
    except Exception as e:
        print(f"[!] 讀取 {map_json_path} 失敗: {e}")
        return

    # 2. 掃描 videos/ 資料夾中由用戶移入的所有 .jpg 九宮格檔案
    os.makedirs(videos_dir, exist_ok=True)
    
    jpg_files = glob.glob(os.path.join(videos_dir, "*.jpg"))
    if not jpg_files:
        print(f"[!] {videos_dir}/ 資料夾中找不到任何被移入的九宮格 JPG 圖片！")
        print(f"[i] 請將滿意的九宮格圖片從 previews/ 移動至 videos/ 資料夾後再次執行。")
        return

    print(f"[+] 於 {videos_dir}/ 資料夾中掃描到 {len(jpg_files)} 張被移入的九宮格預覽圖。")
    print(f"[+] 開始最高畫質下載原影片 (輸出為原始影片檔名)，並於完成後自動刪除預覽圖...\n")

    success_count = 0
    skipped_count = 0

    for idx, jpg_path in enumerate(jpg_files, 1):
        image_name = os.path.basename(jpg_path)
        
        # 移除前面的順序數字前綴 (如 "0001-影片標題.jpg" -> "影片標題")
        base_name_without_num = re.sub(r'^\d{4}-', '', os.path.splitext(image_name)[0])
        
        target_video_file = os.path.join(videos_dir, f"{base_name_without_num}.mp4")

        # 檢查影片是否已經下載過
        if os.path.exists(target_video_file):
            print(f"[{idx}/{len(jpg_files)}] [EXISTS] 影片已存在: {os.path.basename(target_video_file)}")
            # 已存在影片時，刪除對應的 JPG 九宮格
            try:
                os.remove(jpg_path)
                print(f"   [Clean] 已自動清理九宮格圖片: {image_name}\n")
            except Exception as e:
                print(f"   [!] 清理圖片失敗: {e}\n")
            skipped_count += 1
            continue

        # 查詢 preview_map.json (用包含 0001- 前綴的 image_name 查詢)
        info = preview_map.get(image_name)
        if not info or not info.get("url"):
            print(f"[{idx}/{len(jpg_files)}] [!] 找不到 {image_name} 的對應 URL mapping，跳過。\n")
            continue

        video_url = info.get("url")
        video_title = info.get("title", base_name_without_num)

        print(f"[{idx}/{len(jpg_files)}] 開始下載最高畫質影片: {video_title}")
        print(f"   - 原始網址: {video_url}")
        print(f"   - 輸出路徑: {target_video_file}")

        # 使用 yt-dlp 下載最高畫質 (bestvideo+bestaudio/best)
        ydl_opts = {
            'format': 'bestvideo+bestaudio/best',
            'outtmpl': target_video_file,
            'quiet': False,
            'no_warnings': True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
            print(f"  [OK] 影片下載成功 -> {os.path.basename(target_video_file)}")
            
            # 下載成功後自動刪除對應的九宮格 JPG
            try:
                os.remove(jpg_path)
                print(f"  [Clean] 已自動刪除對應九宮格圖片: {image_name}\n")
            except Exception as e:
                print(f"  [!] 自動刪除圖片失敗: {e}\n")

            success_count += 1
        except Exception as e:
            print(f"  [FAIL] 影片下載失敗 ({video_url}): {e}\n")

    print("==================================================")
    print(f"[DONE] 下載作業全數完成！成功: {success_count} 部 | 已存在/跳過: {skipped_count} 部")
    print(f"[+] 影片已儲存在: {os.path.abspath(videos_dir)}")
    print("==================================================")

if __name__ == "__main__":
    run_download_process()

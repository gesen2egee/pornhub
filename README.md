# 影片 3x3 九宮格定時截圖與原影片下載工具

本專案提供 **Windows 雙擊 Bat 檔** 與 **Python 命令行工具**。包含 3x3 九宮格產出、自動 URL JSON 索引、右上角鮮綠色放大時間標籤，以及二階段原影片下載功能 (`run_download`)。

---

## ⚡ 核心功能亮點

1. **多頁連續擷取 (Multi-page Scraping)**：
   - 執行時自動詢問需要連續擷取的頁數 (預設 1 頁)。
   - 輸入 `2` 或 `3` 即可自動透過 `?page=1`, `?page=2`, `?page=3` 自動跨頁連續收集影片並合成九宮格！
2. **關鍵字自動轉搜尋**：
   - 輸入非網址內容（例如 `joi` 或 `cosplay`）會自動轉化為 Pornhub 搜尋網址！
3. **九宮格右上角 56pt 巨型鮮綠色標籤 (帶 3px 黑色描邊)**：
   - 時間標籤字體巨型化，配合 3px 黑色外框包覆，預覽縮圖時一眼即可無比清晰看清時間點。
4. **二階段原影片最高畫質下載 (`run_download`)**：
   - 將滿意的九宮格圖片移至 `videos/` 資料夾後，雙擊 **[run_download.bat](file:///c:/workspace/pornhub/run_download.bat)**，即可一鍵下載原影片，並於下載完成後自動刪除該九宮格 `.jpg` 圖檔。

---

## 💻 最簡使用方式 (雙擊執行工作流程)

1. **步驟一：生成 3x3 九宮格圖片**
   - 雙擊執行 **[run_capture.bat](file:///c:/workspace/pornhub/run_capture.bat)**。
   - 輸入網址、關鍵字或直接 Enter (預設首頁)。
   - 輸入想要連續擷取的頁數 (直接 Enter 預設 1 頁)。

2. **步驟二：挑選滿意影片移至 `videos/`**
   - 開啟 `downloads/` 資料夾，瀏覽九宮格預覽圖。
   - **將滿意想下載的九宮格圖片 (`.jpg`)，直接「剪貼/移動」到 `videos/` 資料夾中**。

3. **步驟三：一鍵下載原影片並自動清理**
   - 雙擊執行 **[run_download.bat](file:///c:/workspace/pornhub/run_download.bat)**。
   - 程式會自動掃描 `videos/` 資料夾內的九宮格圖片，以下載最高畫質原始影片 (`videos/<同名影片>.mp4`)。
   - **當影片下載成功後，程式會自動刪除對應的九宮格 `.jpg` 圖檔**，只留下下載好的原影片！

---

## 💡 進階用法：Python 命令行工具

```bash
# 1. 處理單一影片網址 (輸出一張 3x3 九宮格圖片)
python capture_frames.py "https://www.pornhub.com/view_video.php?viewkey=6a4ea2003acb7"

# 2. 處理網頁 URL (例如 Pornhub 列表/搜尋/首頁，批次為每部影片各產出一張 3x3 九宮格圖片)
python capture_frames.py "https://cn.pornhub.com/video?o=mv" -m 5
```

### 1. 安裝需求
確保系統已安裝 Python 3、`ffmpeg` 與 `yt-dlp` 套件：
```bash
pip install yt-dlp
```

### 2. 給予網頁 URL，自動抓取該網頁內「每一部影片」並定時截圖
```bash
# 範例一：傳入 Pornhub 網頁 URL（自動掃描頁面上所有影片，預設最高畫質 best、每 60 秒抓一張圖）
python capture_frames.py "https://cn.pornhub.com/"

# 範例二：指定分類/搜尋頁面，並設定最多處理前 5 部影片 (-m 5)
python capture_frames.py "https://cn.pornhub.com/video?o=mv" -m 5

# 範例三：給予單一影片網址
python capture_frames.py "https://www.pornhub.com/view_video.php?viewkey=6a4ea2003acb7"
```

### 3. 進階參數與選項說明
- `target`: 影片 URL、網頁 URL (如 `https://cn.pornhub.com/...`)、或包含 URL 清單的 `.txt` 檔案路徑。
- `-m`, `--max-videos`: 最多處理的影片數量（預設 `0` 表示處理頁面上的全數影片）。
- `-i`, `--interval`: 截圖間隔秒數（預設 `60` 秒 = 1 分鐘）。
- `-q`, `--quality`: 畫質選擇（預設 `best` 最高清，可指定 `1080p`, `720p`, `480p`, `240p` 等）。
- `-w`, `--workers`: 每部影片並行處理線程數（預設 `4`）。
- `-o`, `--output`: 儲存根目錄（預設 `downloads`）。

---

## 💡 方法二：PowerShell 腳本 / 手動 Bash 迴圈

如果您習慣在 PowerShell 控制台中手動執行，以下是自動對整部影片「每 60 秒截圖一張」的 PowerShell 寫法：

```powershell
# 1. 取得 240p 串流網址
$url = python -m yt_dlp -g -f 240p "https://www.pornhub.com/view_video.php?viewkey=6a4ea2003acb7"

# 2. 假設影片長度 600 秒 (10 分鐘)，從第 0 秒到 600 秒，每 60 秒截一張圖
0..10 | ForEach-Object {
    $ss = $_ * 60
    $filename = "frame_$(${ss}s).jpg"
    
    ffmpeg -y `
      -user_agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0" `
      -headers "Referer: https://www.pornhub.com/`r`n" `
      -ss $ss -i $url `
      -frames:v 1 -update 1 -q:v 2 $filename
}
```

---

## ⚡ 關鍵技術注意事項
1. **HTTP Headers (User-Agent & Referer)**:
   Pornhub 的 CDN 有反防盜鏈與機器人檢測，ffmpeg 請求時**必須**加上 `User-Agent` 與 `Referer: https://www.pornhub.com/\r\n`，否則會回傳 `HTTP 410 Gone` 或 `HTTP 403 Forbidden`。
2. **遠端 Fast Seek (`-ss` 在 `-i` 前)**:
   `-ss <seconds>` 放在 `-i <url>` **前面**，ffmpeg 會直接跳轉到該時間點的關鍵幀開始讀取，不會從影片頭開始播，因此每次截圖僅需 1~3 秒、網路流量僅約幾十 KB。

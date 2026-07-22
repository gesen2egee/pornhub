# 影片 3x3 九宮格定時截圖與原影片下載工具

本專案提供 **Windows 雙擊 Bat 檔** 與 **Python 命令行工具**。包含 3x3 九宮格產出、自動 URL JSON 索引、右上角鮮綠色放大時間標籤（帶黑框描邊）、`previews/` 自動清空、`0001-` 順序編號、單管道即時串流下載剪裁，以及二階段原影片下載功能 (`run_download`)。

---

## 📦 環境與依賴套件安裝

### 1. 安裝 Python 依賴套件
在專案根目錄開啟終端機或命令提示字元 (CMD/PowerShell)，執行以下指令：

```bash
pip install -r requirements.txt
```

**包含的第三方套件說明**：
- **`yt-dlp`**：用於精確解析影片串流 URL 與進行最高畫質影片下載。
- **`Pillow`**：用於九宮格圖片的圖像合成、56pt 巨型鮮綠色標籤與 3px 黑色描邊繪製。
- **`numpy`**：用於影格四邊像素（>50% 同值純色/黑邊）的極速矩陣分析。

### 2. 系統工具依賴 (System Dependency)
- **FFmpeg**：本專案依賴 FFmpeg 進行極速影格抽取與無縫影音同步剪裁。
  *(請確保電腦已安裝 FFmpeg 並已將其加入系統的 PATH 環境變數中)*

---

## ⚡ 核心功能亮點

1. **九宮格儲存至 `previews/` 且開跑自動清空**：
   - 所有九宮格預覽圖儲存於 `previews/` 資料夾。
   - 每次執行截圖時，自動清理 `previews/` 舊圖檔與重置 `preview_map.json`！
2. **`0001-` 順序編號檔名 (原影片保持原名)**：
   - 九宮格圖檔名按下載順序編號：`0001-影片標題.jpg`、`0002-影片標題.jpg`。
   - 將圖檔移至 `videos/` 下載時，下載出的原影片維持**原始影片標題**：`videos/影片標題.mp4`！
3. **二階段原影片下載與九宮格搬移 (`run_download`)**：
   - 將滿意的九宮格圖片移至 `videos/` 資料夾後，雙擊 **[run_download.bat](file:///c:/workspace/pornhub/run_download.bat)**，即可一鍵下載原影片。
   - **下載成功後，自動將該九宮格 `.jpg` 移動至 `downloads/` 資料夾保留**，歸檔保存。
4. **下載即跳過開頭純色/黑邊 (Stream Direct Skip)**：
   - 在發起下載前 0.05 秒自動採樣分析遠端串流。若開頭純色/黑邊於 `0.5s ~ 10.0s` 之間，直接在下載時傳入前置跳過選項。
   - **落地的影片即為完美切除開頭、影音 100% 同步的精華影片**，零二次處理開銷！

---

## 💻 最簡使用方式 (雙擊執行工作流程)

1. **步驟一：生成 3x3 九宮格圖片**
   - 雙擊執行 **[run_capture.bat](file:///c:/workspace/pornhub/run_capture.bat)**。
   - 產出之 `0001-` 編號九宮格預覽圖將儲存於 `previews/` 資料夾中。

2. **步驟二：挑選滿意影片移至 `videos/`**
   - 開啟 `previews/` 資料夾，瀏覽九宮格預覽圖。
   - **將滿意想下載的九宮格圖片 (`0001-影片標題.jpg`)，直接「剪貼/移動」到 `videos/` 資料夾中**。

3. **步驟三：一鍵下載原影片並自動清理**
   - 雙擊執行 **[run_download.bat](file:///c:/workspace/pornhub/run_download.bat)**。
   - 程式會自動下載最高畫質原始影片 (`videos/影片標題.mp4`)。
   - **下載成功後自動刪除九宮格 `.jpg` 圖檔**，保持 `videos/` 資料夾乾淨！

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

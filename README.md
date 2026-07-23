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

## 🎙️ 字幕 ASR：MOSS

字幕流程固定使用 MOSS-Transcribe-Diarize，同一次推理產生時間戳；繁中翻譯會保留 `[S01]`、`[S02]` 等匿名說話者標籤與原時間軸。

九宮格與影片統一使用 `WEB_META_V1`。新九宮格會在 URL 之外寫入完整網頁 metadata；舊式只有 URL 的九宮格會在下載或偵測到影片已存在時自動補齊。字幕完成後，MP4 會另外封存完整 `ORIGINAL_SRT` 與 `TRANSLATED_SRT`。可用 `python video_meta.py show 檔案.mp4` 查看，或用 `python video_meta.py export 檔案.mp4 --out-dir 輸出資料夾` 匯出。

### Windows CUDA 安裝 MOSS

MOSS 模式需要 Windows、NVIDIA GPU／Driver、Python 3.12 與 CUDA 相容環境。雙擊或在 CMD 執行：

```bat
install_moss.bat
```

安裝器會建立 `moss/.venv`，從官方 CUDA 12.8 wheel index 安裝 PyTorch，固定安裝 MOSS 官方程式碼 commit `9990574e6ac62390a21bcce25a914d66ac92c25e`，並從 ModelScope 下載 `openmoss/MOSS-Transcribe-Diarize` 至 `moss/model-cache`。同時會安裝音訊分析依賴，並固定下載 `xmlans/asmr-enhancer` commit `ade1a82b4f8b97abf088280d22156448cc0a888f`。若 CUDA 不可用，安裝器會停止，不會改用 CPU。

### 使用 MOSS 推理

CMD：

```bat
run_subtitle.bat --force --limit 1
```

PowerShell：

```powershell
.\run_subtitle.bat --force --limit 1
```

字幕完成後不再保留獨立 SRT；MOSS 原文與繁中翻譯完整存入影片 Meta，`low_videos/` 與一般 `videos/` 都由 FFmpeg 將繁中字幕直接燒入畫面。燒錄所需 SRT 只會短暫建立於已忽略的 `tasks/subtitle-temp/`，成功或失敗後都會清理。舊資料若已有同名 SRT，或影片內已有 `TRANSLATED_SRT`（包含空字幕區段），未來執行會直接略過。只處理全部低畫質影片可執行：

硬字幕預設會比畫面底部提高約半個字體高度，降低被播放器進度條遮住的機會。

```bat
run_subtitle.bat --low-only
```

### 字幕前自動音訊增強

字幕流程預設會在載入 MOSS 前完成音訊判斷：

1. 避開固定片頭，從影片 25%、50%、75% 各取 4 秒。
2. 使用響度、峰均比、穩定度與 AudioSet AST 音樂分類結果判斷。
3. `pass` 影片保留原音軌。
4. `enhance` 與 `uncertain` 影片使用 ASMR Enhancer 產生暫存影片。
5. ASR 與最後字幕封裝使用選定音軌；成功後才覆蓋原影片。
6. 增強完成的影片會寫入 `ASMR Enhancer auto v1` metadata，`--force` 重跑時不會重複增強。

音訊分類器只載入一次，分類完成後會釋放 GPU，再執行 ASMR Enhancer，最後才載入 ASR 模型。第一次執行會下載 `MIT/ast-finetuned-audioset-10-10-0.4593` 至 `moss/audio-model-cache`。每次判斷報告寫入已忽略的 `tasks/audio-enhance-latest.json`。

可選設定：

- `AUDIO_AUTO_ENHANCE`：預設 `1`；設為 `0` 可關閉整段自動判斷與增強。
- `AUDIO_CLASSIFIER_MODEL`：預設 `MIT/ast-finetuned-audioset-10-10-0.4593`。
- `AUDIO_CLASSIFIER_REVISION`：預設固定為測試過的 Hugging Face commit `f826b80d28226b62986cc218e5cec390b1096902`。
- `AUDIO_CLASSIFIER_CACHE`：覆蓋音訊分類模型快取路徑。
- `AUDIO_STAGE_PYTHON`：字幕音訊處理子程序，預設使用 `moss/.venv/Scripts/python.exe`；即使 ASR 切換為 Whisper，也會使用此隔離環境。
- `AUDIO_GPU_RESERVE_MB`：預設保留 2048 MB；可用 VRAM 不足時分類器自動改用 CPU。
- `ASMR_ENHANCER_DEVICE`：預設 `auto`，也可指定 `cpu` 或 `cuda`。
- `ASMR_ENHANCER_SCRIPT`：覆蓋 ASMR Enhancer 程式路徑。

`OPENROUTER_API_KEY` 預設從 OS 環境變數取得，不寫入專案檔案。

MOSS 可選設定：

- `MOSS_MODEL`：預設 `openmoss/MOSS-Transcribe-Diarize`。
- `MOSS_DEVICE`：預設 `cuda:0`。
- `MOSS_DTYPE`：預設 `bfloat16`，也支援 `float16`。
- `MOSS_MAX_NEW_TOKENS`：一般流程預設 `65536`；`--low-only` 的 30 秒短片批次預設 `1024`，避免無語音片段長時間生成。
- `MOSS_HOTWORDS`：逗號分隔的專有名詞提示。

---

## 🔊 Confucius4-TTS 語音複製與跨語言推理

本專案整合 [NetEase Youdao Confucius4-TTS](https://github.com/netease-youdao/Confucius4-TTS)，可用一段參考 WAV 複製音色與情緒，再以同一音色合成繁中、英文、日文等 14 種語言。官方基礎環境為 Python 3.10、CUDA 12.6 與 NVIDIA GPU；TTS 使用獨立的 `confucius4_tts/` 環境，不會影響字幕 ASR。

### 一鍵安裝

先安裝 Python 3.10、Git for Windows 與最新版 NVIDIA Driver，再雙擊：

```bat
install_confucius4_tts.bat
```

安裝器會建立獨立 `.venv`、安裝 PyTorch 2.7.0 CUDA 12.6，並固定使用官方 commit `186983518e9e8ab9af69cabdda3436a76d6ccdfb`。模型權重會在第一次推理時從 Hugging Face 自動下載到 `confucius4_tts/model-cache/`，需要數 GB 磁碟空間。

安裝後可先檢查環境：

```bat
run_confucius4_tts.bat --check
```

### 語音合成

參考音訊必須是 WAV，建議使用 5～15 秒、單一人物、背景安靜且沒有音樂的清晰人聲：

```bat
run_confucius4_tts.bat ^
  --prompt-wav "samples\voice.wav" ^
  --text "這是一段使用參考音色合成的測試語音。" ^
  --lang zh ^
  --output "tts_output.wav"
```

可用語言代碼：`zh`、`en`、`ja`、`ko`、`de`、`fr`、`es`、`id`、`it`、`th`、`pt`、`ru`、`ms`、`vi`。若 Hugging Face 連線受限，可在執行前設定 `HF_ENDPOINT`；預設強制使用 CUDA，只有確定要進行非常慢的 CPU 推理時才加上 `--device cpu`。

若目錄內的影片都有同名繁中 SRT，可批次產生繁中配音版。程式預設讀取 `tasks/demucs-moss-retry/status.jsonl`，將影片對應到先前 Demucs 產生的純人聲 `vocals.wav`；每段字幕使用分離人聲的同時段作為 reference voice，短段落自動擴充前後 context。輸出預設放在 `low_videos/demucs_translated_zh/`，不會覆寫來源：

```bat
run_confucius4_dub.bat

rem 先測試一支
run_confucius4_dub.bat --limit 1
```

配音會按照 SRT 時間軸調整語速，並在繁中語音出現時自動壓低原音。可用 `--force` 重新產生既有輸出、用 `--output-dir` 指定其他輸出目錄，或用 `--reference-manifest` 指定另一份 Demucs `status.jsonl`。

> 請只複製你本人或已取得明確授權的聲音，並向聽眾揭露內容由 AI 合成。

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

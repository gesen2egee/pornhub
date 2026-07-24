# 影片九宮格、下載與字幕管線

這是 Windows 專案，提供九宮格預覽、雙畫質下載、MOSS 字幕辨識、OpenRouter 繁中翻譯與音訊自動增強。下載與字幕使用不同程序：每支影片下載完成後會立刻進入字幕佇列，同時下載程序繼續處理下一支。

## 根目錄入口

根目錄只保留日常需要操作的檔案：

```text
00_setup_or_update.bat  建立／更新完整執行環境
01_run_capture.bat      產生九宮格預覽圖
02_run_download.bat     下載影片並同步處理字幕
.gitignore
README.md
requirements.txt
```

專案程式、測試、文件、MOSS 與其他輔助工具都放在 `lib/`。測試、計畫與暫存工作資料放在已忽略的 `tasks/`。

## 輸出目錄

```text
output/
├── 00_temp/             下載、字幕與 FFmpeg 暫存
├── 01_preview_images/   01_run_capture.bat 產生的九宮格
├── 02_preview_videos/   最低畫質 30 秒預覽影片與硬字幕
├── 03_videos/           正式影片與同名相容 SRT
└── 04_downloaded/       已完成正式影片的九宮格歸檔
```

可用 `PORN_OUTPUT_DIR` 環境變數整體改寫 `output/` 位置，程式內的子目錄名稱由 `lib/project_paths.py` 統一管理。

## 第一次安裝或更新

需要：

- Windows
- Python 3.12
- Git for Windows
- FFmpeg／FFprobe，並加入 `PATH`
- NVIDIA GPU 與最新版 Driver

雙擊：

```bat
00_setup_or_update.bat
```

安裝程式會：

1. 建立 `lib/.venv`。
2. 安裝 `requirements.txt`。
3. 建立 `lib/moss/.venv`。
4. 安裝 CUDA 12.8 PyTorch、MOSS 與音訊處理依賴。
5. 固定 MOSS commit `9990574e6ac62390a21bcce25a914d66ac92c25e`。
6. 固定 ASMR Enhancer commit `ade1a82b4f8b97abf088280d22156448cc0a888f`。
7. 驗證 CUDA 並下載必要模型。

BAT 使用 ASCII 指令內容、CRLF 換行及 `chcp 65001`，可避免繁中 Windows 的批次檔編碼問題。環境檢查：

```bat
00_setup_or_update.bat --check
01_run_capture.bat --check
02_run_download.bat --check
```

## 使用流程

### 1. 產生九宮格

雙擊 `01_run_capture.bat`，貼上影片、關鍵字或列表網址。輸出會放在：

```text
output/01_preview_images/
```

### 2. 選擇下載方式

將九宮格 JPG 移到：

- `output/02_preview_videos/`：下載最低畫質的動態 30 秒取樣，輸出繁中硬字幕。
- `output/03_videos/`：下載最高等效 1080P 正式影片，不燒錄畫面字幕，輸出同名外掛 SRT。

### 3. 下載與字幕

雙擊 `02_run_download.bat`。

影片會先進入 `output/00_temp/pipeline/`。每支下載完成後立即交給背景字幕 worker；字幕完成才移入正式目錄。正式影片完成後，九宮格移到 `output/04_downloaded/`；預覽影片的九宮格則保留在 `output/02_preview_videos/`。

維護模式：

```bat
02_run_download.bat --retry-subtitles
02_run_download.bat --repair-over-1080
```

## MOSS 與字幕輸出

長影片固定以 **7.5 分鐘（450 秒）**切段辨識，再把完整時間軸一次交給 LLM 校正與翻譯。若單段發生 CUDA OOM，會繼續自動二分到最低 3 分鐘。

- `output/03_videos/`：保留原始畫面，輸出 UTF-8 BOM、CRLF、移除 `[Sxx]` 標籤的播放器相容 SRT。
- `output/02_preview_videos/`：使用 FFmpeg 燒錄繁中硬字幕。
- MP4 Meta 同時保存完整 `ORIGINAL_SRT`、`TRANSLATED_SRT` 與 `[S01]`、`[S02]` 說話者標籤。
- 已有完整 Meta 但缺少正式影片外掛 SRT 時，會由 Meta 補建，不重新執行 ASR 或翻譯。

OpenRouter 金鑰從環境變數讀取：

```text
OPENROUTER_API_KEY
```

常用選項：

- `MOSS_MODEL`：預設 `openmoss/MOSS-Transcribe-Diarize`
- `MOSS_DEVICE`：預設 `cuda:0`
- `MOSS_DTYPE`：預設 `bfloat16`
- `MOSS_MAX_NEW_TOKENS`：正式影片預設 `8192`，預覽影片預設 `1024`
- `MOSS_HOTWORDS`：逗號分隔的專有名詞
- `SUBTITLE_LOW_JOB_TIMEOUT_SECONDS`：預覽影片字幕 timeout，預設 900 秒
- `SUBTITLE_JOB_TIMEOUT_SECONDS`：正式影片字幕 timeout，預設 7200 秒

## 音訊自動增強

字幕前會分析影片 25%、50%、75% 三個位置。`pass` 保留原音；`enhance` 與 `uncertain` 會使用 ASMR Enhancer。分類器釋放 GPU 後才載入 MOSS，避免兩個模型同時占用 VRAM。

常用選項：

- `AUDIO_AUTO_ENHANCE=0`：關閉自動分析與增強
- `AUDIO_GPU_RESERVE_MB`：預設保留 2048 MB VRAM
- `ASMR_ENHANCER_DEVICE`：`auto`、`cpu` 或 `cuda`
- `AUDIO_ENHANCE_REPORT`：覆蓋分析報告路徑

## Python 與測試

直接執行程式：

```powershell
lib\.venv\Scripts\python.exe lib\capture_frames.py "影片網址"
lib\.venv\Scripts\python.exe lib\run_download.py
```

執行測試：

```powershell
lib\.venv\Scripts\python.exe -m pytest -q lib\tests
```

查看或匯出影片 Meta：

```powershell
lib\.venv\Scripts\python.exe lib\video_meta.py show "影片.mp4"
lib\.venv\Scripts\python.exe lib\video_meta.py export "影片.mp4" --out-dir "輸出資料夾"
```

Confucius4-TTS 等非主流程工具保留在 `lib/`，不會出現在根目錄的日常入口中。

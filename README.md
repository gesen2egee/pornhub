# 影片九宮格、下載與字幕管線

這是 Windows 專案，提供九宮格預覽、雙畫質下載、MOSS 字幕辨識、OpenRouter 繁中翻譯與音訊自動增強。下載與字幕使用不同程序：每支影片下載完成後會立刻進入字幕佇列，同時下載程序繼續處理下一支。

## 根目錄入口

根目錄只保留日常需要操作的檔案：

```text
00_setup_or_update.bat  建立／更新完整執行環境
01_run_capture.bat      產生 5×5 預覽圖並以 GPU TAGGER 篩選
02_run_download.bat     下載影片並同步處理字幕
03_open_muse.bat        開啟 Muse 圖形介面
.gitignore
README.md
requirements.txt
```

專案程式、測試、文件、MOSS 與其他輔助工具都放在 `lib/`。測試、計畫與暫存工作資料放在已忽略的 `tasks/`。

## 輸出目錄

```text
output/
├── 00_temp/             下載、字幕與 FFmpeg 暫存
├── 01_preview_images/   01_run_capture.bat 產生的 5×5 宮格
├── 02_preview_videos/   前 3 分鐘低畫質 → Whisper 語音剪片 + 軟 SRT，不硬字幕/不 enhance
├── 02_shorts/           內嵌翻譯時間軸直抓最高畫質片段；僅 URL 先分析前 9 分鐘 240P
├── 03_videos/           480P（對白>30s 剪片）+ 軟字幕，不 enhance
├── 04_downloaded/       已完成九宮格歸檔
├── 05_chosen/           精選輸入（可丟九宮格或含 URL 的影片）
└── 06_good/             精選成品：1080P + MOSS + Grok 4.5 minimal + 判斷 enhance
```

### 四條流程

| 流程 | 放入 | 畫質 | 字幕 | enhance | 輸出 |
|------|------|------|------|---------|------|
| 預覽 | `02_preview_videos` 九宮格或含 URL 影片 | 一次下載 BS 段低畫質（預設 3×3 分鐘）；語音>30s 剪片否則全留 | Demucs 人聲 → 批次 MOSS → 一次 Grok 精選翻譯 | 否 | 同目錄 |
| Shorts | `02_shorts` 九宮格或含 URL 影片 | 有內嵌翻譯直接依時間軸抓來源最高畫質；否則先抓前 9 分鐘 240P，再抓對應最高畫質片段 | 重用內嵌翻譯，或 MOSS → Grok 精選翻譯 | 判斷 | 同目錄 |
| 標準全片 | `03_videos` 九宮格或含 URL 影片 | 480P（Whisper 剪片） | Demucs 人聲 → Whisper + **Grok 4.3 none**（便宜） | 否 | `03_videos` |
| 精選 | `05_chosen` 九宮格或影片 | 1080P | Demucs 人聲 → MOSS + **Grok 4.5 minimal** | 判斷 | `06_good` |
| 歸檔 | （自動） | — | — | — | 九宮格→`04_downloaded`；chosen 來源影片刪除 |

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

1. 在專案根目錄建立共用的 `.venv`。
2. 安裝 `requirements.txt`。
3. 將 MOSS 安裝到同一個 `.venv`，並以獨立子程序釋放 CUDA 資源。
4. 安裝 CUDA 12.8 PyTorch、MOSS 與音訊處理依賴。
5. 固定 MOSS commit `9990574e6ac62390a21bcce25a914d66ac92c25e`。
6. 固定 ASMR Enhancer commit `ade1a82b4f8b97abf088280d22156448cc0a888f`。
7. 驗證 CUDA 並下載必要模型。

BAT 使用 ASCII 指令內容、CRLF 換行及 `chcp 65001`，可避免繁中 Windows 的批次檔編碼問題。環境檢查：

```bat
00_setup_or_update.bat --check
01_run_capture.bat --check
02_run_download.bat --check
03_open_muse.bat --check
```

## 使用流程

### 圖形介面（推薦）

完成第一次安裝後，雙擊：

```bat
03_open_muse.bat
```

Muse 會在瀏覽器開啟 `http://127.0.0.1:8765/`，所有操作與偏好都只保留在本機。介面把完整流程整理成四步：

1. **探索：** 同時選擇多個網站，以關鍵字或網址搜尋。
2. **預覽：** 把候選內容移到獨立分頁比較，依片長、來源與狀態篩選排序。
3. **佇列：** 決定建立 30 秒預覽或完整影片，接上既有下載與字幕管線。
4. **收藏：** 播放本機影片、顯示同名繁中軟字幕，並記住續播進度。

右上角可切換日／夜間與繁中／英文。縮圖模糊、隱私畫面（快捷鍵 `P`）和播放進度都只存在目前電腦。關閉 `03_open_muse.bat` 的命令視窗即可停止本機介面。

### 1. 產生 5×5 宮格

雙擊 `01_run_capture.bat`，貼上影片、關鍵字或列表網址。每支影片會同步抓取 25 張畫面；畫面一完成就由常駐 GPU 的 MobileNetV4 ONNX TAGGER 判斷。預設所有畫面都必須為 `RATING=general` 且含有 `TAG=smile`（信心值各至少 50%），否則整個 5×5 宮格不會儲存。批次檔可在執行時改寫這些條件。

```text
output/01_preview_images/
```

### 2. 選擇下載方式

將素材放入對應目錄：

- `output/02_preview_videos/`：九宮格**或含 URL 的影片** → 每次下載 **3 分鐘低畫質**並以 **MOSS** 辨識；累計對話未達 30 秒就續抓下一段，影片結束仍不足則保留完整影片。達門檻後依字幕剪片、自動判斷 **enhance**，輸出軟 SRT。
- `output/02_shorts/`：若影片已有內嵌翻譯字幕，會依 `preview_trimmed_segments`／`trimmed_segments` 將剪輯後字幕反向映射回原片時間，再下載來源最高畫質片段；只有 URL 時則先分析前 9 分鐘 240P。
- `output/03_videos/`：九宮格**或含 URL 的影片** → **480P**，**MOSS** 字幕並據此剪片（對白淨長 >30s），高畫質切塊下載完成就排入自動 **enhance**，輸出同名軟 SRT。
- `output/05_chosen/`：九宮格**或含 URL 的影片**（影片只作 URL 載體）→ **1080P + MOSS + Grok 4.5（minimal）**，**判斷 enhance**；先用低畫質分析，再只下載需要的高畫質區段，完成進 `06_good`；九宮格歸檔 `04_downloaded`，來源影片刪除。

### 3. 下載與字幕

雙擊 `02_run_download.bat`。程式會依序掃描 `02_preview_videos`、
`02_shorts`、`03_videos`、`05_chosen`；每層只要找到符合該層條件的九宮格圖片或影片就直接執行，
找不到則印出 `[SKIP]` 後繼續下一層，沒有逐層確認。四層依序為 Preview、Shorts、Video、Chosen，
不會因為只跑其中一層而先啟動其他昂貴流程。下載時只顯示本程式的階段進度，不顯示
yt-dlp DEBUG 與下載百分比訊息。

暫存位於 `output/00_temp/pipeline/`。Video 與 Chosen 預設分成兩個階段：

1. 以 240P 每 180 秒下載一段；累計滿 BS（預設 3 段）才排入一次 `Demucs → ASR`，並與下一批下載重疊。影片尾端不足 BS 仍會送出，全部完成後才進下一階段。
2. 同時執行 OpenRouter 翻譯、高畫質切塊下載，以及每個已下載切塊的自動 `enhance`；三條工作都完成後才 retime 字幕、拼接與發布。

Preview 每段預設 180 秒，一次先下載 `--asr-batch-size` 段（預設 3 段，共最多 9 分鐘），再用同一批執行 `Demucs → MOSS BS ASR`；合併完整批次字幕後只送一次 OpenRouter 精選翻譯。下載與剪片完成後同樣會自動判斷 enhance。

只盤點、不下載：

```bat
02_run_download.bat --list
```

直接指定預算層級：

```bat
02_run_download.bat --stages preview
02_run_download.bat --stages shorts
02_run_download.bat --stages preview video
02_run_download.bat --stages chosen
```

所有主要功能都有正反開關與 args：

```bat
02_run_download.bat --stages video --no-translation --no-dialogue-trim
02_run_download.bat --stages chosen --subtitles --enhance --metadata --archive
02_run_download.bat --stages preview --preview-seconds 90 --no-keep-work
02_run_download.bat --stages video --video-height 480 --asr-backend moss
02_run_download.bat --stages video --asr-stream --asr-chunk-seconds 180 --asr-batch-size 3
02_run_download.bat --stages chosen --no-asr-stream
02_run_download.bat --stages chosen --chosen-height 1080 --translation-model x-ai/grok-4.5 --reasoning-effort minimal
```

可控制項目包含：`asr`、`demucs-asr`、`asr-stream`、`subtitles`、`translation`、`dialogue-trim`、`enhance`、
`metadata`、`archive`、`keep-work`、`reuse-cache`、`force`。每個布林項目都可使用
`--功能` 或 `--no-功能`；完整說明請執行 `02_run_download.bat --help`。

這些開關彼此獨立：`--no-subtitles` 只停止輸出外掛 SRT，不會關閉 ASR、
翻譯或剪片；`--no-translation` 也不會關閉 ASR。若關閉 ASR 但仍開啟翻譯
或剪片，程式只會使用既有 ASR 快取；沒有快取時會明確報錯，不會偷偷替你
開啟 ASR 或順帶關閉其他功能。每層開始前都會印出實際 ON/OFF 設定。
`demucs-asr` 預設開啟，會在 ASR 前分離 vocals 人聲；它和最終成品的
`enhance` 完全獨立，需要時可用 `--no-demucs-asr` 關閉。
若要讓新設定重新套用到既有成品，請明確加上 `--force`；預設會保護既有成品。
Chosen 有 URL 時不會拿既有高畫質成品接續，會重新下載規劃出的高畫質片段；
若連 ASR 字幕快取也要重做，請加上 `--no-reuse-cache`。
每支發布完成的影片 Metadata 都會寫入 `published_stage`（`preview`、`shorts`、`video` 或 `chosen`）。下次掃描同一層資料夾時，標示同層已發布的影片會列為既有成品、不再處理；需要明確重跑時使用 `--force`。
剪片門檻與區段合併間隔可分別用 `--trim-threshold`、`--segment-gap` 調整；`--preview-seconds` 是 Preview 每次下載與 ASR 的片段長度，不再是總長度。
`asr-stream` 預設開啟；`--asr-chunk-seconds` 預設為 180。Video／Chosen 會累計滿 `--asr-batch-size`（預設 3）段才執行一次 MOSS BS ASR，同時開始下載下一批；影片尾端不足 BS 的批次仍會送出。關閉 `--asr-stream` 時，會退回完整 240P 代理下載完成後再 ASR，其他功能不會被連帶關閉。
目前剪片規則是：停頓小於門檻時完整保留；停頓大於或等於門檻時切段，預設前後延伸為 0 秒。

維護模式：

```bat
02_run_download.bat --retry-subtitles
02_run_download.bat --repair-over-1080
```

## MOSS 與字幕輸出

長影片預設以 **3 分鐘（180 秒）**下載 240P 片段；每片段先經 Demucs，再獨立辨識，合併成完整時間軸後才一次送到 OpenRouter。若單段發生 CUDA OOM，ASR 仍會自動二分到最低 90 秒。

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
- `ASR_BACKEND`：`moss`（預設，本機 GPU）或 `voxtral`（OpenRouter 雲端 STT）
- `REUSE_ASR_RESULT`：預設 `1`，有 `work_dir/asr_result.json` 則跳過 ASR 續跑
- `VOXTRAL_MODEL`：預設 `mistralai/voxtral-mini-transcribe`
- `MOSS_MAX_NEW_TOKENS`：僅 moss；正式片預設 `4096`，預覽預設 `1024`
- `MOSS_ASR_BATCH_SIZE`：一次並行幾段 3 分鐘音訊（預設 `3`；voxtral 為 HTTP 並行，moss 為 GPU batch）
- `MOSS_HOTWORDS`：逗號分隔的專有名詞
- `SUBTITLE_LOW_JOB_TIMEOUT_SECONDS`：預覽影片字幕 timeout，預設 900 秒
- `SUBTITLE_JOB_TIMEOUT_SECONDS`：正式影片字幕 timeout，預設 7200 秒

## 音訊自動增強

字幕前會直接 Seek 並解碼影片 25%、50%、75% 三個位置，各取 4 秒，分析階段不再解碼整支音軌。`pass` 保留原音；`enhance` 與 `uncertain` 才會使用 ASMR Enhancer 處理完整影片。真正的完整增強仍可能依片長與 GPU 花費數分鐘，但不會阻止已下載的正式影片出現在 `output/03_videos/`。分類器釋放 GPU 後才載入 MOSS，避免兩個模型同時占用 VRAM。

常用選項：

- `AUDIO_AUTO_ENHANCE=0`：關閉自動分析與增強
- `AUDIO_GPU_RESERVE_MB`：預設保留 2048 MB VRAM
- `ASMR_ENHANCER_DEVICE`：`auto`、`cpu` 或 `cuda`
- `AUDIO_ENHANCE_REPORT`：覆蓋分析報告路徑

## 多站支援

列表／搜尋／分頁／下載透過 `lib/sites/` registry。本輪內建：

- **Tier 1：** Eporner（關鍵字預設）、Pornhub、XVideos、xHamster、XNXX、SpankBang  
- **Tier 2：** Beeg、DrTuber、RedTube、YouPorn、Tube8、AlphaPorno、EMPFlix、EroProfile  
- **Tier 3：** MissAV、Jable.tv、91porn（`SITE_91PORN_COOKIES` = Netscape cookies.txt）、hanime.tv、HypnoTube  

下載僅走 yt-dlp 與各站 adapter hooks，**不再**使用 Pornhub HTML 直連 fallback。社群 plugin 僅 clone 至 `tasks/plugins-research/` 供研究，不安裝進 venv。

## Python 與測試

直接執行程式：

```powershell
.venv\Scripts\python.exe lib\capture_frames.py "影片網址"
.venv\Scripts\python.exe lib\run_download.py
```

執行單元測試：

```powershell
.venv\Scripts\python.exe -m pytest -q lib\tests
```

多站真實連線 smoke（產物在 `tasks/tests/site-smoke/`，失敗會 skip）：

```powershell
.venv\Scripts\python.exe -m pytest -q tasks\tests\test_sites_smoke.py -m site_smoke
```

查看或匯出影片 Meta：

```powershell
.venv\Scripts\python.exe lib\video_meta.py show "影片.mp4"
.venv\Scripts\python.exe lib\video_meta.py export "影片.mp4" --out-dir "輸出資料夾"
```

Confucius4-TTS 等非主流程工具保留在 `lib/`，並使用專案根目錄的
`.venv-confucius4`（Python 3.10）隔離其 Torch／CUDA 依賴，不會出現在
根目錄的日常入口中。ASMR Enhancer 只保留流程實際載入的原始碼，不建立
第三個正式 venv。

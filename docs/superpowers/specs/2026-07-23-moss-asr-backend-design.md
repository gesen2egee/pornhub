# MOSS ASR Backend 整合設計

## 目標

在既有字幕流程中新增可選的 MOSS-Transcribe-Diarize ASR backend，同時完整保留目前 faster-whisper 行為。使用者可透過 `ASR_BACKEND=whisper|moss` 切換，不需維護兩套翻譯、SRT 輸出或 FFmpeg 封裝程式。

## 使用環境與版本

- 目標作業系統：Windows。
- 目標運算環境：NVIDIA CUDA。
- MOSS 模型：ModelScope `openmoss/MOSS-Transcribe-Diarize`。
- MOSS 官方推理程式碼：GitHub commit `9990574e6ac62390a21bcce25a914d66ac92c25e`。
- PyTorch：依官方 quickstart 使用 CUDA 12.8 wheel index，不自行改用其他 CUDA wheel。
- Python：MOSS 使用獨立 Python 3.12 venv。
- 模型載入需要 `trust_remote_code=True`，因為模型倉庫包含自訂 Transformers 程式碼。

## 架構

採用雙 venv、單一字幕工作流程：

- `whisper/.venv`：既有 faster-whisper backend。
- `moss/.venv`：MOSS、PyTorch CUDA、Transformers、ModelScope 與 MOSS 官方 helper package。
- `run_subtitle.bat`：依 `ASR_BACKEND` 選擇 Python interpreter；未設定時使用 `whisper`。
- `run_subtitle.py`：只負責共用批次流程，透過 backend factory 載入指定 ASR。
- MOSS adapter：封裝模型載入、推理及官方 transcript segment 解析，輸出既有 cue 結構。

不建立常駐 API server，也不引入 SGLang 或 vLLM。這是 Windows 單機批次流程，不需要 server 的額外部署與維護成本。

## 安裝流程

新增 Windows 安裝腳本，負責：

1. 檢查 NVIDIA driver／`nvidia-smi`、Git 與可用的 Python 3.12。
2. 建立 `moss/.venv`。
3. 從官方 CUDA 12.8 wheel index 安裝 `torch` 與 `torchaudio`。
4. 以固定 commit 安裝官方 MOSS GitHub package。
5. 安裝 ModelScope 與既有翻譯流程所需的 `requests`。
6. 驗證 `torch.cuda.is_available()` 與必要 import。
7. 透過 ModelScope `snapshot_download` 將權重下載至 `moss/model-cache`。

安裝腳本重跑時應安全復用既有 venv 與 cache。若 GPU、Python 或安裝步驟不符合需求，腳本應立即停止並顯示可操作的繁體中文錯誤。

## 推理資料流

```text
影片
  ↓
run_subtitle.bat 依 ASR_BACKEND 選擇 venv
  ↓
run_subtitle.py 載入 backend
  ├─ whisper → faster-whisper segments
  └─ moss → ModelScope 本機模型 → 官方 generate_transcription → parse_transcript
  ↓
統一 cue：id、time、text
  ↓
OpenRouter 翻譯
  ↓
同名 SRT
  ↓
FFmpeg 內嵌軟字幕並覆蓋原影片
```

MOSS cue 的 `text` 使用 `[S01] 原文` 格式保留匿名說話者標籤。時間戳轉換成既有 SRT `HH:MM:SS,mmm --> HH:MM:SS,mmm` 格式。說話者標籤只表示同一輸入檔案內的相對說話者，不視為真實身分。

## 設定介面

- `ASR_BACKEND`：`whisper` 或 `moss`，預設 `whisper`。
- `MOSS_MODEL`：預設 `openmoss/MOSS-Transcribe-Diarize`；adapter 以此 ID 呼叫 ModelScope `snapshot_download`，並將回傳的本機 snapshot path 交給 Transformers。
- `MOSS_DEVICE`：預設 `cuda:0`。
- `MOSS_DTYPE`：預設 `bfloat16`。
- `MOSS_MAX_NEW_TOKENS`：預設 `65536`，採用官方長音訊範例上限；可用環境變數覆寫。
- `MOSS_HOTWORDS`：可選，以逗號分隔並附加到官方 transcription prompt。

命令列介面維持 `run_subtitle.py --force --limit N --dry-run`。backend 切換不改變輸入資料夾、翻譯或輸出行為。

## 錯誤處理

- `ASR_BACKEND` 不合法：啟動前停止並列出允許值。
- 選擇 MOSS 但 `moss/.venv` 不存在：Bat 顯示安裝腳本名稱後停止。
- CUDA 不可用：MOSS 明確失敗，不自動退回 CPU 或 Whisper。
- ModelScope snapshot 不存在或不完整：提示重新執行安裝／下載步驟。
- 模型輸出無法解析或沒有有效 segment：該影片標記失敗，批次繼續處理下一部。
- MOSS segment 時間範圍無效：拒絕該 segment，避免產生損壞 SRT。
- OpenRouter 或 FFmpeg 錯誤：沿用目前逐片失敗與批次統計行為。

## 測試策略

單元測試不載入真實模型，使用小型 fake model／processor 或依賴注入驗證：

- backend 名稱解析與預設 Whisper。
- MOSS segment 轉換為 SRT cue。
- `[Sxx]` 說話者標籤保留。
- 無效時間戳與空 transcript 的錯誤。
- MOSS CUDA guard 不會退回 CPU。
- Bat interpreter 選擇邏輯使用 `cmd /c` 搭配暫時的 fake interpreter path 做 smoke test。
- 原有 Whisper cue 行為不變。

實作遵循 TDD：先執行新測試並確認因功能不存在而失敗，再加入最小實作使其通過。

Windows CUDA 真實驗證分兩層：

1. 安裝驗證：確認 CUDA、import、model snapshot 與模型載入。
2. 推理驗證：使用短音訊或 `--limit 1` 執行 MOSS，確認能產生含說話者標籤的 SRT。

目前開發機只有 Intel UHD 730，無 NVIDIA CUDA，因此可完成單元測試與靜態安裝檢查，但不能在此機宣稱真實 CUDA 模型推理成功。真實推理結果必須在目標 Windows CUDA 機器執行後判定。

## 不在本次範圍

- 不移除或改變 faster-whisper 預設行為。
- 不部署 MOSS API server。
- 不加入 CPU fallback。
- 不做人名辨識或跨檔案 speaker identity。
- 不改變 OpenRouter 指定模型版本。
- 不重構下載、截圖或其他非字幕功能。

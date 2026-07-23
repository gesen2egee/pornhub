# Design: 影片內嵌網頁 Meta 與字幕 Meta

**日期：** 2026-07-24  
**狀態：** Approved for user review  
**範圍：** 下載後寫入網頁 meta；字幕流程寫入原文／翻譯 SRT meta；讀回工具。不改九宮格圖片下載鏈。

---

## 1. 目標與非目標

### 目標

1. 每部下載成功的 MP4 都帶有**可再讀取**的網頁 meta（標題、上傳者、標籤、分類、views…）。
2. 字幕完成後，同一支 MP4 的 metadata 內另存：
   - **原文 SRT**（MOSS，含 `[S01]` / `[S02]` …）
   - **翻譯 SRT**（繁中，**同樣保留** `[S01]` / `[S02]` …）
3. Meta 用途僅為**封存與之後程式讀回**，不當播放器字幕軌。
4. **不影響**現有圖片 EXIF → `run_download` 下載流程。

### 非目標

- 不把字幕當第二條軟字幕軌給播放器用。
- 不改九宮格 JPG EXIF 寫入／讀取（`ImageDescription` 仍只服務下載 URL）。
- 第一版不為 Eporner 自寫 HTML 補強 uploader／tags（yt-dlp 缺的欄位存 `null`）。
- 不強制額外 sidecar `.json` 作為主儲存（meta 跟 MP4 走）。

---

## 2. 背景與現況

| 能力 | 現況 |
|------|------|
| 九宮格 EXIF | 只嵌影片 URL；下載依賴此欄位 |
| Pornhub 網頁 meta | yt-dlp 可取 tags、categories、cast、uploader、views、likes… |
| Eporner 網頁 meta | yt-dlp 較薄（title、views、rating、description 關鍵字串等；無結構化 tags/uploader） |
| 字幕 | MOSS 原文含 `[Sxx]`；`translate_cues`／`strip_speaker_labels` **會去掉**發言者後再寫 `.srt` |
| MP4 comment 容量 | mutagen 寫入 `©cmt` 實測 1KB～2MB exact 讀回；實務雙份 SRT（約 10–50KB）足夠 |

---

## 3. 架構

```
[capture_frames / JPG EXIF]  ──不變──►  run_download
                                           │
                                           ├─ 下載 MP4（現有邏輯）
                                           └─ 成功後：寫入 WEB_META 到 MP4 tags

[run_subtitle]
    ASR → original_cues（含 [Sxx]）
    翻譯 → translated_cues（**保留 [Sxx]**）
    寫同名 .srt（翻譯，含 [Sxx]）
    軟字幕 / 硬字幕（現有）
    最後：合併寫入 ORIGINAL_SRT + TRANSLATED_SRT 到 MP4 comment
           （並保留既有 WEB_META 區段）
```

### 元件

| 元件 | 職責 |
|------|------|
| `video_meta.py`（新） | 序列化／解析 comment 區段；mutagen 讀寫；從 yt-dlp info 建 WEB_META |
| `run_download.py` | 下載成功後呼叫寫入 WEB_META；**不改** EXIF 讀 URL |
| `translate_srt_openrouter.py` | 翻譯時保留發言者標籤（見 §5） |
| `run_subtitle.py` | 翻譯後不再 strip；封裝後寫入雙份 SRT meta |
| CLI 讀取（可掛在 `video_meta.py` 或小腳本） | 顯示／匯出 meta |

寫入實作優先使用 **mutagen** 直接改 MP4 atom，避免 Windows 下 `ffmpeg -metadata` 命令列長度限制。

---

## 4. MP4 儲存格式

### 4.1 標準 tags（方便檔案總管／播放器顯示）

| Tag | 來源 |
|-----|------|
| `title`（`©nam`） | yt-dlp `title` |
| `artist`（`©ART`） | `uploader` 或 `cast` 第一位（有則寫） |
| `date`（`©day`） | `upload_date`（YYYY-MM-DD 或 YYYYMMDD） |

### 4.2 `comment`（`©cmt`）正文：分區純文字

單一字串，UTF-8，區段標記固定、可擴充：

```text
===WEB_META_V1===
{...單行或緊湊 JSON...}
===ORIGINAL_SRT===
（完整 SRT 正文，含 [S01] 等）
===TRANSLATED_SRT===
（完整 SRT 正文，含 [S01] 等）
```

規則：

1. 區段以行首 `===NAME===` 識別；未知區段讀取時忽略，寫入時盡量保留。
2. `WEB_META_V1` 為一個 JSON object（建議緊湊、`ensure_ascii=False`）。
3. 下載階段：只寫／更新 `WEB_META_V1`；若 comment 已有字幕區段則保留。
4. 字幕階段：更新 `ORIGINAL_SRT` 與 `TRANSLATED_SRT`；保留 `WEB_META_V1`。
5. 若某階段尚未有資料，可省略該區段（讀取時當空）。

### 4.3 `WEB_META_V1` 欄位（schema）

```json
{
  "schema": "web_meta_v1",
  "extractor": "PornHub",
  "id": "ph…",
  "title": "…",
  "description": null,
  "uploader": "…",
  "uploader_id": "…",
  "tags": ["…"],
  "categories": ["…"],
  "cast": ["…"],
  "view_count": 0,
  "like_count": 0,
  "comment_count": 0,
  "average_rating": null,
  "duration": 0,
  "upload_date": "YYYYMMDD",
  "thumbnail": "https://…",
  "webpage_url": "https://…",
  "age_limit": 18
}
```

- **缺值約定（固定）：** 純量缺 → `null`；陣列缺 → `[]`。不省略 key，方便測試與讀取。
- 不存串流 URL、cookies、formats 等下載憑證。

---

## 5. 發言者標籤 `[S01]` 保留策略

**需求：** 原文與翻譯「前後」都保留 `[Sxx]`。

### 5.1 現況問題

`translate_cues` 翻譯前會 `strip_speaker_labels`，回寫時再次 strip，導致繁中 SRT 與畫面上都沒有發言者。

### 5.2 目標行為

1. **原文 cues／ORIGINAL_SRT**：維持 ASR 輸出，例如 `[S01] Hello`。
2. **翻譯 cues／TRANSLATED_SRT／同名 .srt／硬字幕燒錄文字**：例如 `[S01] 你好`（標籤保留，只翻譯正文）。
3. Meta 兩區與磁碟 `.srt` 一致，避免「檔案一套、meta 一套」。

### 5.3 實作方式（建議）

在 `translate_srt_openrouter.py`：

1. 翻譯前：從每條 `text` **分離**前綴 `^\s*\[S\d+\]\s*` 與正文。
2. 只把**正文**送進 OpenRouter（減少模型改壞標籤）。
3. 翻譯後：用**原前綴**接回譯文：`f"{prefix}{translated_body}"`。
4. 若模型仍回傳標籤，再 strip 譯文上的重複標籤後接回原前綴，避免 `[S01] [S01] …`。
5. **移除** `run_subtitle.py` 對翻譯結果的 `strip_speaker_labels` 呼叫。
6. `strip_speaker_labels` 可保留給測試或其它用途，但預設翻譯路徑不再使用。

System prompt 可補一句：正文翻譯結果不要自行加 `[Sxx]` 前綴（實際以前綴重貼為準）。

### 5.4 相容

- Whisper 無發言者時：前綴為空，行為與現在幾乎相同。
- 既有已去掉標籤的 `.srt`：不強制回填；`--force` 重跑 ASR+翻譯才會帶標籤。

---

## 6. 流程細節

### 6.1 `run_download`

1. 既有：從 JPG EXIF 讀 URL、yt-dlp／fallback 下載。
2. **新增（僅 `download_success` 後）：**
   - 若下載過程已有 `extract_info` 結果可重用則重用；否則對 `video_url` 再 `extract_info(download=False)` 一次（失敗則 log 警告，不讓下載算失敗）。
   - `build_web_meta(info)` → `merge_write_mp4_meta(mp4_path, web_meta=…)`，**同時**寫入 §4.1 的 `title` / `artist` / `date`。
3. low_videos 與 videos 兩路徑皆寫入。
4. 圖片搬移／保留邏輯不變。

### 6.2 `run_subtitle`

1. ASR → `original_cues`（含標籤）。
2. `translated_cues = translate_cues(...)`（**保留標籤**，見 §5）。
3. 寫同名 `.srt` = 翻譯版（含標籤）。
4. 軟字幕／硬字幕：現有 ffmpeg 流程；需確保 **不丟** 已有 WEB_META：
   - 軟字幕已有 `-map_metadata 0`；硬字幕同樣保留 metadata map。
   - 封裝成功後仍呼叫 `merge_write_mp4_meta`，以 mutagen **再寫一次** 完整 comment（WEB + 雙 SRT），作為保險（避免 map 遺失或工具截斷）。
5. `ORIGINAL_SRT` / `TRANSLATED_SRT` 字串用 `format_srt(original_cues)` / `format_srt(translated_cues)`。
6. 若略過 ASR（同名 SRT 已存在）：  
   - `TRANSLATED_SRT` 可從現有 `.srt` 讀取；  
   - `ORIGINAL_SRT` 若 comment 裡已有則保留，否則可省略或僅更新翻譯區（`--force` 可重生原文）。  
   - **仍須**在軟／硬字幕封裝後執行一次 `merge_write_mp4_meta`，避免 re-mux 後 comment 變空。

### 6.3 讀取 API

```text
read_mp4_meta(path) -> {
  "title", "artist", "date",
  "web_meta": dict | None,
  "original_srt": str | None,
  "translated_srt": str | None,
  "raw_comment": str | None,
}
```

可選 CLI：

```bash
python video_meta.py show path.mp4
python video_meta.py export path.mp4 --out-dir dir/
```

`export` 寫出 `*.web_meta.json`、`*.orig.srt`、`*.translated.srt`（僅匯出，非主儲存）。

---

## 7. 錯誤處理

| 情況 | 行為 |
|------|------|
| yt-dlp 取 meta 失敗 | 下載仍算成功；印警告；MP4 可不寫 WEB_META |
| mutagen 寫入失敗 | 印錯誤；不 rollback 影片檔；不影響下載／字幕主流程成功碼（或字幕流程記 warning，可討論） |
| comment 過大 | 實務不會；若未來需要可警告 > 2MB |
| 舊片無 meta | 讀取回空；不報錯 |
| 重封裝後 meta 遺失 | 字幕步驟結尾強制 mutagen 重寫字幕區 + 保留 web |

建議：**meta 失敗不阻斷**主流程（下載／字幕），但 log 清楚，方便之後批次補寫。

---

## 8. 測試計畫

1. **單元：`video_meta` 分區 parse/merge**  
   - 只 WEB、只字幕、兩者皆有、未知區段保留。
2. **單元：發言者保留**  
   - `[S01] Hello` → 譯文以 `[S01] ` 開頭；無標籤句子不受影響；避免雙重前綴。
3. **整合（可用暫存小 MP4）：**  
   - 寫 WEB_META → ffprobe/mutagen 讀回欄位一致。  
   - 再寫雙 SRT → WEB 仍在、兩段 SRT exact。  
4. **回歸：**  
   - 無 EXIF 的 JPG 仍 skip；有 EXIF 下載路徑不因 meta 失敗而 FAIL。  
   - `run_subtitle` 軟字幕仍成功。  
5. **既有測試：** 更新任何「翻譯後不得含 `[S01]`」的斷言，改為「必須保留」。

---

## 9. 依賴

- 新增：`mutagen`（寫入 `requirements.txt`）。
- 既有：`yt-dlp`、`ffmpeg`（字幕封裝）。

---

## 10. 文件與 README

- 更新 README：說明 MP4 內嵌 meta、發言者在翻譯後仍保留、如何 `show`／`export`。
- 修正現有「翻譯輸出會移除 `[S01]`」敘述。

---

## 11. 實作順序（高階）

1. `video_meta.py` + 單元測試（格式、讀寫）。  
2. 翻譯路徑保留 `[Sxx]` + 測試。  
3. 接入 `run_download`（成功後 WEB_META）。  
4. 接入 `run_subtitle`（雙 SRT + 合併寫入）。  
5. CLI 讀取／export。  
6. README。

---

## 12. 決策記錄

| 決策 | 選擇 | 原因 |
|------|------|------|
| 存哪 | 只 MP4，不動 JPG 下載鏈 | 使用者要求不影響下載流程 |
| 字幕怎麼存 | comment 分區，不當字幕軌 | 只要 meta 封存；容量實測足夠 |
| 寫入工具 | mutagen | 避開 Windows CLI 長度限制；大段多行可靠 |
| EP meta | yt-dlp only | 第一版夠用；缺欄 null |
| 發言者 | 原文＋翻譯都保留 | 使用者明確要求「前後都要保留 [S01]」 |
| meta 失敗 | 不阻斷主流程 | 下載／字幕優先 |

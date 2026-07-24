# Design: 影片內嵌網頁 Meta 與字幕 Meta

**日期：** 2026-07-24  
**狀態：** Approved for user review  
**範圍：** 下載後寫入網頁 meta；舊版九宮格 FALLBACK 升級；字幕雙份完整 SRT meta；讀回工具。

---

## 1. 目標與非目標

### 目標

1. 每部下載成功的 MP4 都帶有**可再讀取**的網頁 meta（標題、上傳者、標籤、分類、views…）。
2. 字幕完成後，同一支 MP4 的 metadata 內另存**兩份完整原格式 SRT**（序號 + 時間軸 + 正文，不得壓成純文字）：
   - **ORIGINAL_SRT**：ASR 原本輸出（含 `[S01]` / `[S02]` …與完整時間軸），**一字不改結構**。
   - **TRANSLATED_SRT**：**只換翻譯版本**——與原文相同序號／時間軸／`[Sxx]` 前綴，**僅 cue 正文換成繁中譯文**。
3. 磁碟同名 `.srt`（給軟／硬字幕用）= **只換翻譯版本**（與 TRANSLATED_SRT 一致）。
4. 影片層級時間一併寫入 WEB_META：`duration`、`duration_string`、`upload_date`、`timestamp`、`meta_written_at`。
5. Meta 用途僅為**封存與之後程式讀回**，不當播放器字幕軌。
6. **下載讀 URL 相容：** 仍從九宮格 `ImageDescription` 讀影片網址；舊圖只含 URL 時可照常下載。
7. **舊格式 FALLBACK：** 下載時若九宮格為「只有網址的舊格式」，自動抓取網頁 meta，升級為**有 WEB_META 的影片 + 有 WEB_META 的九宮格**。

### 非目標

- 不把字幕當第二條軟字幕軌給播放器用。
- **不**把完整雙份 SRT 塞進九宮格（SRT 只進 MP4）；九宮格只升 **WEB_META**（+ 保留 URL）。
- 第一版不為 Eporner 自寫 HTML 補強 uploader／tags（yt-dlp 缺的欄位存 `null`）。
- 不強制額外 sidecar `.json` 作為主儲存（meta 跟 MP4／JPG 走）。

---

## 2. 背景與現況

| 能力 | 現況 |
|------|------|
| 九宮格 EXIF | **舊格式**：只嵌影片 URL（`ImageDescription`）；下載依賴此欄位 |
| Pornhub 網頁 meta | yt-dlp 可取 tags、categories、cast、uploader、views、likes… |
| Eporner 網頁 meta | yt-dlp 較薄（title、views、rating、description 關鍵字串等） |
| 字幕 | MOSS 原文含 `[Sxx]`；翻譯路徑目前會 strip 發言者（本設計改為保留） |
| MP4 comment 容量 | mutagen 寫入 `©cmt` 實測可到 2MB exact 讀回 |

---

## 3. 架構

```
[capture_frames]  新圖：URL + WEB_META 寫入 JPG
        │
        ▼
[run_download]  讀 ImageDescription URL（舊／新格式皆可）
        │
        ├─ 下載 MP4（現有邏輯；已存在則跳過下載）
        │
        └─ FALLBACK 升級（下載成功或片已存在，best-effort）：
              extract_info → WEB_META → MP4
              若九宮格舊格式 → WEB_META → JPG UserComment（URL 不變）

[run_subtitle]
    ASR → original_cues（含 [Sxx]）
    翻譯 → translated_cues（只換正文，保留 [Sxx] + 時間）
    寫同名 .srt = 只換翻譯版本
    軟／硬字幕（現有）
    最後：ORIGINAL_SRT + TRANSLATED_SRT 寫入 MP4 comment（保留 WEB_META）
```

### 元件

| 元件 | 職責 |
|------|------|
| `video_meta.py`（新） | 序列化／解析 comment 區段；mutagen 讀寫 MP4；Pillow 讀寫 JPG EXIF meta；`build_web_meta`；舊格式偵測與升級 |
| `run_download.py` | 仍只從 `ImageDescription` 讀 URL；下載成功／片已存在後做 meta 升級 fallback |
| `capture_frames.py` | **新產九宮格**即寫 URL + WEB_META（避免再產出舊格式） |
| `translate_srt_openrouter.py` | 翻譯時保留發言者標籤（見 §5） |
| `run_subtitle.py` | 翻譯後不再 strip；封裝後寫入雙份完整 SRT meta |
| CLI 讀取 | 顯示／匯出 MP4（與可選 JPG）meta |

寫入 MP4 優先使用 **mutagen**；寫入 JPG 使用 **Pillow EXIF**（與現況一致）。

---

## 4. MP4 儲存格式

### 4.1 標準 tags（方便檔案總管／播放器顯示）

| Tag | 來源 |
|-----|------|
| `title`（`©nam`） | yt-dlp `title` |
| `artist`（`©ART`） | `uploader` 或 `cast` 第一位（有則寫） |
| `date`（`©day`） | `upload_date`（優先 `YYYY-MM-DD`，否則 `YYYYMMDD`） |

### 4.2 `comment`（`©cmt`）正文：分區純文字

單一字串，UTF-8，區段標記固定、可擴充：

```text
===WEB_META_V1===
{...單行或緊湊 JSON...}
===ORIGINAL_SRT===
（ASR 原本完整 SRT 輸出格式，未改結構）
===TRANSLATED_SRT===
（只換翻譯版本：同格式，僅正文為繁中）
```

規則：

1. 區段以行首 `===NAME===` 識別；未知區段讀取時忽略，寫入時盡量保留。
2. `WEB_META_V1` 為一個 JSON object（建議緊湊、`ensure_ascii=False`）。
3. 下載階段：只寫／更新 `WEB_META_V1`；若 comment 已有字幕區段則保留。
4. 字幕階段：更新 `ORIGINAL_SRT` 與 `TRANSLATED_SRT`；保留 `WEB_META_V1`。
5. 若某階段尚未有資料，可省略該區段（讀取時當空）。
6. **兩份字幕都必須是完整原格式 SRT**（`format_srt`／檔案原文），禁止只存純譯句列表。
7. **時間必存（見 §5）：** 影片層級時間在 WEB_META；字幕 cue 時間軸在完整 SRT 內，翻譯不得改動。

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
  "duration": 1531,
  "duration_string": "25:31",
  "upload_date": "20180512",
  "timestamp": 1526094205,
  "thumbnail": "https://…",
  "webpage_url": "https://…",
  "age_limit": 18,
  "meta_written_at": "2026-07-24T12:34:56+00:00"
}
```

**時間相關欄位（必含 key，缺值用 `null`）：**

| 欄位 | 意義 | 來源 |
|------|------|------|
| `duration` | 片長（秒，整數或數字） | yt-dlp `duration` |
| `duration_string` | 可讀片長 | yt-dlp 或由 `duration` 格式化 |
| `upload_date` | 上傳日 `YYYYMMDD` | yt-dlp `upload_date` |
| `timestamp` | 上傳 Unix 秒 | yt-dlp `timestamp` |
| `meta_written_at` | 本工具寫入 WEB_META 的 UTC ISO8601 | 本機 `datetime.now(timezone.utc)` |

- **缺值約定（固定）：** 純量缺 → `null`；陣列缺 → `[]`。不省略 key，方便測試與讀取。
- 不存串流 URL、cookies、formats 等下載憑證。

---

## 5. 發言者標籤與時間軸保留策略

**需求：**

1. 原文與翻譯「前後」都保留 `[Sxx]`。
2. **時間也要：** 影片上傳／片長進 WEB_META；每條字幕的 SRT 時間軸（`HH:MM:SS,mmm --> HH:MM:SS,mmm`）原文與翻譯**完全一致**，不得在翻譯或寫 meta 時丟棄或改寫。

### 5.1 現況問題

`translate_cues` 翻譯前會 `strip_speaker_labels`，回寫時再次 strip，導致繁中 SRT 與畫面上都沒有發言者。時間軸目前在 cue 的 `time` 欄位，翻譯路徑通常不動，但須在規格中**明確保證**並寫入完整 SRT（含時間行）。

### 5.2 目標行為

1. **原文 cues／ORIGINAL_SRT**：維持 ASR 輸出，例如：
   ```text
   1
   00:00:00,480 --> 00:00:01,660
   [S01] Hello
   ```
2. **翻譯 cues／TRANSLATED_SRT／同名 .srt／硬字幕燒錄文字**：
   ```text
   1
   00:00:00,480 --> 00:00:01,660
   [S01] 你好
   ```
   - 標籤保留，只翻譯正文。
   - **`id` 與 `time` 與原文同一 cue 完全相同。**
3. Meta 兩區與磁碟 `.srt` 一致，避免「檔案一套、meta 一套」。
4. WEB_META 必須帶上 §4.3 的時間欄位（有則填值，無則 `null`）。

### 5.3 實作方式（建議）

在 `translate_srt_openrouter.py`：

1. 翻譯前：從每條 `text` **分離**前綴 `^\s*\[S\d+\]\s*` 與正文；**不要改** `id` / `time`。
2. 只把**正文**送進 OpenRouter（減少模型改壞標籤）；payload **不含**時間字串（避免模型改時間）。
3. 翻譯後：用**原前綴**接回譯文：`f"{prefix}{translated_body}"`；`time` 原樣拷貝。
4. 若模型仍回傳標籤，再 strip 譯文上的重複標籤後接回原前綴，避免 `[S01] [S01] …`。
5. **移除** `run_subtitle.py` 對翻譯結果的 `strip_speaker_labels` 呼叫。
6. `strip_speaker_labels` 可保留給測試或其它用途，但預設翻譯路徑不再使用。
7. `format_srt` 寫出完整區塊（序號 + 時間行 + 文本）；寫入 meta 的 SRT 必須由此產生，禁止只存純文字行。

System prompt 可補一句：正文翻譯結果不要自行加 `[Sxx]` 前綴（實際以前綴重貼為準）。

### 5.4 相容

- Whisper 無發言者時：前綴為空，行為與現在幾乎相同；時間軸仍完整保留。
- 既有已去掉標籤的 `.srt`：不強制回填；`--force` 重跑 ASR+翻譯才會帶標籤。

---

## 6. 流程細節

### 6.0 九宮格 meta 格式與舊版偵測

| 欄位 | EXIF tag | 內容 |
|------|----------|------|
| 影片 URL（下載用） | `ImageDescription` `0x010e` | 純 URL 字串（**永遠保留，下載只讀此欄**） |
| 網頁 meta | `UserComment` `0x9286` | 與 MP4 相同分區語法，至少含 `===WEB_META_V1===` + JSON（**不含**長 SRT） |

**舊格式（URL-only）判定** — 任一成立即視為舊格式、需要升級：

1. 有合法 URL 於 `ImageDescription`，且
2. `UserComment` 缺失／空白，或 parse 後沒有可解析的 `WEB_META_V1` JSON。

**新格式：** URL + 可讀 `WEB_META_V1`。

讀寫 API（`video_meta.py`）：

```text
is_legacy_grid_jpg(path) -> bool
read_grid_jpg_meta(path) -> {url, web_meta, raw_user_comment}
write_grid_jpg_web_meta(path, web_meta, *, url=None) -> None
  # url=None 時保留既有 ImageDescription；寫入時不得清空 URL
```

`UserComment` 寫入注意：Pillow 對 UserComment 常需 `charset` 前綴（如 `ascii\x00\x00\x00` 或 `UNICODE\x00`）；實作須 round-trip 測過再定 charset，以 UTF-8 可讀為準。

### 6.1 `run_download`（含 FALLBACK 升級）

1. 既有：從 JPG `ImageDescription` 讀 URL、yt-dlp／fallback 下載。**不改讀 URL 邏輯。**
2. **升級觸發點（best-effort，失敗只警告）：**
   - **A. 下載成功**後，或
   - **B. 影片已存在而 skip 下載**時（重跑 download 可幫舊庫升級，不必重下片）。
3. 升級步驟（共用 helper，例如 `upgrade_media_web_meta(jpg_path, mp4_path, video_url)`）：
   1. `extract_info(url, download=False)`（失敗 → 警告並 return；**不**把下載標成 FAIL）。
   2. `web = build_web_meta(info)`。
   3. 若 `mp4_path` 存在 → `merge_write_mp4_meta(mp4, web_meta=web)`（補齊／覆寫 WEB；保留已有 SRT 區段）。
   4. 若 `is_legacy_grid_jpg(jpg)` → `write_grid_jpg_web_meta(jpg, web)`，log：`[UPGRADE] 九宮格舊格式→已寫入 WEB_META`。
   5. 若九宮格已是新格式：可選擇**仍用最新 info 覆寫** WEB_META（建議：覆寫，資料較新）或跳過；**預設覆寫**以與 MP4 一致。
4. low_videos 與 videos 兩路徑皆適用。
5. 圖片搬移／保留邏輯不變（videos 成功後仍搬到 `downloads/`；搬移前完成 JPG 升級，使 `downloads/` 內也是新格式）。

### 6.1b `capture_frames`（新圖直接新格式）

產九宮格存檔時：

1. `ImageDescription` = `video_url`（同現況）。
2. 用當次 `extract_video_info`／yt-dlp info 建 `build_web_meta`（若當次 info 欄位不足，可再一次輕量 extract 或只寫已有 title/duration + url）。
3. `UserComment` = `===WEB_META_V1===\n{json}`。

如此新截圖不再產出「只有網址」的舊格式；舊圖仍靠 §6.1 fallback 升級。

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
| yt-dlp 取 meta 失敗 | 下載仍算成功；印警告；不升級 MP4／JPG |
| mutagen／Pillow 寫入失敗 | 印警告；不 rollback；不影響下載／字幕成功 |
| comment 過大 | 實務不會；若未來需要可警告 > 2MB |
| 舊片／舊九宮格無 meta | 讀取回空；**重跑 run_download** 觸發 fallback 升級 |
| 重封裝後 meta 遺失 | 字幕步驟結尾強制 mutagen 重寫字幕區 + 保留 web |
| 無 URL 的九宮格 | 維持現況 SKIP 下載；無法升級 |

建議：**meta／升級失敗不阻斷**主流程（下載／字幕），但 log 清楚（含 `[UPGRADE]`），方便之後批次補寫。

---

## 8. 測試計畫

1. **單元：`video_meta` 分區 parse/merge**  
   - 只 WEB、只字幕、兩者皆有、未知區段保留；`None` 不刪既有區段。
2. **單元：發言者與時間軸保留**  
   - `[S01] Hello` → 譯文以 `[S01] ` 開頭；無標籤句子不受影響；避免雙重前綴。  
   - 翻譯前後 `cue["time"]` 字串 identical；`format_srt` 含 `-->` 時間行。  
   - `build_web_meta` 含 `duration` / `duration_string` / `upload_date` / `timestamp` / `meta_written_at` keys。
3. **單元：九宮格舊／新格式**  
   - 只有 `ImageDescription` URL → `is_legacy_grid_jpg` True。  
   - 寫入 UserComment WEB_META 後 → False；URL 仍可讀。  
   - round-trip `web_meta` JSON 一致。
4. **整合（暫存小 MP4 + 小 JPG）：**  
   - 寫 WEB_META → mutagen／Pillow 讀回。  
   - 再寫雙 SRT → WEB 仍在、兩段 SRT exact。  
   - 模擬 download helper：舊 JPG + 新 MP4 → 兩者皆有 WEB_META。  
5. **回歸：**  
   - 無 EXIF 的 JPG 仍 skip；有 EXIF 下載路徑不因 meta 失敗而 FAIL。  
   - `run_subtitle` 軟字幕仍成功。  
6. **既有測試：** 更新任何「翻譯後不得含 `[S01]`」的斷言，改為「必須保留」。

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

1. `video_meta.py` + 單元測試（格式、MP4／JPG 讀寫、legacy 偵測）。  
2. 翻譯路徑保留 `[Sxx]` + 測試。  
3. 接入 `run_download`（WEB_META + **舊九宮格 FALLBACK 升級**；片已存在也升級）。  
4. `capture_frames` 新圖直接寫 URL+WEB_META。  
5. 接入 `run_subtitle`（雙完整 SRT + 合併寫入）。  
6. CLI 讀取／export。  
7. README。

---

## 12. 決策記錄

| 決策 | 選擇 | 原因 |
|------|------|------|
| 下載讀 URL | 仍只用 `ImageDescription` | 相容舊九宮格；下載鏈不破 |
| 舊格式 FALLBACK | 下載成功或片已存在時抓 meta，升級 MP4+JPG | 使用者要求自動升級舊庫 |
| 九宮格存什麼 | URL + WEB_META only（無 SRT） | 圖小、夠用；SRT 只進影片 |
| 字幕怎麼存 | MP4 comment 分區，完整原格式 SRT | 封存用；容量實測足夠 |
| 寫入工具 | mutagen（MP4）+ Pillow（JPG） | 可靠；避開 ffmpeg CLI 長度限制 |
| EP meta | yt-dlp only | 第一版夠用；缺欄 null |
| 發言者 | 原文＋翻譯都保留 | 使用者明確要求「前後都要保留 [S01]」 |
| 時間 | 影片層級 + cue 時間軸都存 | 使用者要求「時間也要」 |
| 雙份 SRT | 原本輸出 + 只換翻譯版本 | 結構相同，只換正文；`.srt` = 翻譯版 |
| meta 失敗 | 不阻斷主流程 | 下載／字幕優先 |

# Design: 多站成人資源支援（registry + 薄適配）

**日期：** 2026-07-24  
**狀態：** Ready for user review  
**範圍：** 站點 registry、列表／分頁／搜尋、預覽與 META、low 影片下載 smoke；社群 plugin 研究後精簡內建。

---

## 1. 目標與非目標

### 目標

1. 以 **站點 registry + 薄適配層** 支援多個成人站的：
   - 列表／搜尋 **抓影片網址**
   - **翻頁**
   - **預覽圖**（九宮格或 smoke 最小截幀）
   - **WEB_META**（至少 title + webpage_url）
   - **下載 low 影片**（既有最低畫質 + 短區間邏輯）
2. 正式程式 **不安裝** 社群 yt-dlp plugin；plugin 原始碼 **clone 到 `tasks/plugins-research/` 僅供研究**，再把必要邏輯 **精簡內建** 進 `lib/sites/`。
3. 真實連線 smoke 測試放在 **`tasks/tests/`**，產物寫入 `tasks/tests/site-smoke/`，**不污染** `output/`。
4. 保持既有管線相容：九宮格 EXIF 仍嵌 URL（+ WEB_META）；`run_download` 仍從 ImageDescription 讀 URL。

### 非目標（本輪）

- Tier 2 原生站（Beeg、DrTuber、RedTube、YouPorn、Tube8、AlphaPorno、EMPFlix、EroProfile）— **延後**。
- 直播錄製（Chaturbate、BongaCams、Camsoda）。
- HypnoTube、純圖片站（nHentai 等）。
- 在 `00_setup_or_update.bat` 安裝 yt-dlp 社群 plugin。
- 改關鍵字預設站（仍為 Eporner）；`--site` 選擇器可列為後續小項，非本輪必做。
- 為每站寫完整 HTML fallback 下載器（僅保留既有 Pornhub fallback）。

---

## 2. 背景與現況

| 能力 | 現況 |
|------|------|
| 關鍵字搜尋 | 非 URL → 轉 Eporner tag 搜尋 |
| 列表解析 | 硬編碼 Pornhub `viewkey` + Eporner path；失敗才 `yt-dlp extract_flat` |
| 分頁 | query `page=` 通用；Eporner 路徑頁碼特判 |
| 下載 | 通用 yt-dlp；Pornhub 有 HTML MP4 fallback |
| META | `video_meta.build_web_meta` + 九宮格／MP4 寫入（既有設計） |
| 測試 | `lib/tests` 單元；`tasks/tests` 整合／smoke |

問題：新增站必須改 `capture_frames.py` 多處 if/hostname，無法擴充；社群站（MissAV 等）原生 yt-dlp 常失敗。

---

## 3. 架構

```
[user input: keyword | list URL | video URL | file]
        │
        ▼
[lib/sites/registry]  resolve(adapter) by URL host / default site
        │
        ├─ search_url(keyword)
        ├─ build_page_url(url, page)
        ├─ extract_list_urls(page_url)  → video URLs
        ├─ is_single_video(url)
        ├─ ydl_opts(context)
        ├─ extract_info(url)            → optional dict | None
        └─ resolve_stream(url)          → optional stream | None
        │
        ▼
[capture_frames]  單片解析同一套 §3 順序 → 九宮格 + EXIF URL + WEB_META
        │
        ▼
[run_download]    讀 URL → 同一套 §3 順序 → low/full
                  僅 Pornhub 保留 HTML MP4 fallback
```

### 元件

| 元件 | 職責 |
|------|------|
| `lib/sites/base.py` | `SiteAdapter`：match、search、分頁、列表、單片、ydl_opts、可選 extract_info／resolve_stream（預設回 `None`，不 raise） |
| `lib/sites/registry.py` | 註冊與 `get_adapter_for_url` / `default_adapter` |
| `lib/sites/http_util.py` | 共用 UA、age cookie、fetch HTML（薄封裝） |
| `lib/sites/<name>.py` | 各站薄適配（見 §4） |
| `lib/sites/__init__.py` | 匯出 registry 公開 API |
| `capture_frames.py` | 列表走 registry；`extract_video_info` 走 §3 單片順序 |
| `run_download.py` | 下載／META 走 §3 單片順序；Pornhub HTML fallback 仍限 pornhub |
| `tasks/plugins-research/` | clone 研究用 repo（gitignore 或文件說明可重 clone） |
| `tasks/tests/test_sites_smoke.py` | 每站真實連線五項 smoke |

### SiteAdapter 介面（最小）

```python
class SiteAdapter:
    name: str                    # e.g. "pornhub", "missav"
    domains: tuple[str, ...]     # host suffixes

    def match_url(self, url: str) -> bool: ...
    def search_url(self, keyword: str) -> str: ...
    def build_page_url(self, url: str, page_num: int) -> str: ...
    def get_start_page(self, url: str) -> int: ...  # default 1 / query page=
    def is_single_video_url(self, url: str) -> bool: ...
    def extract_list_urls(self, page_url: str) -> list[str]: ...
    def ydl_opts(self, purpose: str) -> dict: ...
    # purpose: "list" | "info" | "download_low" | "download_full"

    # 可選 hooks：base 固定 return None（不 raise），呼叫端 null-check
    def extract_info(self, video_url: str, purpose: str) -> dict | None: ...
    def resolve_stream(self, video_url: str, prefer_lowest: bool) -> dict | None:
        # 可回傳 {"url": stream_or_m3u8, "http_headers": {...}, "info": partial_info}
        ...
```

**預設行為（base）：**

- `build_page_url`：query `page=`
- `extract_list_urls`：先站點 HTML 規則（若覆寫），否則 `yt-dlp extract_flat`
- `ydl_opts`：`{}`（沿用全域設定）
- `extract_info` / `resolve_stream`：固定 `return None`（不 raise）
- 關鍵字非 URL：用 **default adapter = Eporner** 的 `search_url`

**單片 info／串流／下載呼叫順序（固定；capture 與 download 共用）：**

1. 若 `extract_info(...)` 回傳 dict → 取 title／duration／WEB_META 原料；若 dict 已含可播 `url`（或 stream 欄位）可直接用於截幀。  
2. 若仍缺可播串流：若 `resolve_stream(...)` 回傳 `{"url", "http_headers", ...}` → 用於截幀與下載（含 low 區間）。  
3. 否則：`yt-dlp` + `adapter.ydl_opts(purpose)`（**Tier 1 主路徑**；Tier 3 優先靠 headers／referer／format 讓 yt-dlp 過）。  
4. 僅 Pornhub：既有 HTML MP4 fallback（download 路徑；capture 若 yt-dlp 已能給 stream 則不必）。  
5. 仍失敗 → 該片失敗；smoke 則 **skip** 並寫 status（不擴成每站完整下載器）。

Smoke #3（預覽）／#4（META）／#5（low）必須共用上述策略，避免「能下載不能截幀」的不一致實作。

**Tier 3 內建邊界：** 研究 plugin 後，優先把「能讓 yt-dlp 成功的 opts + 列表 HTML」內建；僅當 yt-dlp 仍無法解析時，才在該 adapter 實作 `resolve_stream`／`extract_info`（m3u8 + Referer 等），**禁止** runtime import research 目錄。

**解析優先序：**

1. 明確 video URL → 該站 adapter，回傳單元素列表  
2. list/search URL → adapter 分頁 + extract_list_urls  
3. 純關鍵字 → Eporner `search_url`  
4. 文字檔路徑 → 既有讀檔（不經 adapter）

---

## 4. 站點清單（本輪）

### Tier 1 — 原生 yt-dlp（必做）

| 站 | 列表策略 | 分頁 | 備註 |
|----|----------|------|------|
| **Eporner** | 既有 path regex | 路徑頁碼 | 預設搜尋站；遷入 adapter |
| **Pornhub** | viewkey HTML + extract_flat | `page=` | 保留 download HTML fallback |
| **XVideos** | HTML 或 extract_flat | 站點規則／query | |
| **xHamster** | 同上 | 同上 | |
| **XNXX** | 同上 | 同上 | |
| **SpankBang** | 同上 | 同上 | |

### Tier 3 — 社群研究後精簡內建

| 站 | 研究來源（clone 至 tasks） | 內建方向 |
|----|---------------------------|----------|
| **MissAV** | `yt-dlp-plugin-yellow`（或同等 yellow 系） | 優先 ydl_opts（headers/referer）；不足再 `resolve_stream`（m3u8） |
| **Jable.tv** | 同上 | 同上 |
| **91porn** | 同上 | cookies：`SITE_91PORN_COOKIES`；未設定 smoke skip（`needs_cookies`） |
| **hanime.tv** | `hanime-plugin` 或同等 | 優先 opts；不足再 extract_info／resolve_stream |
| **HentaiHaven / hstream.moe** | 若與 hanime 同源邏輯 | optional；同源則共用 base，否則 skip 並記錄 |

**研究流程（實作前）：**

1. Clone 到 `tasks/plugins-research/<repo>/`（不 pip install 進 `lib/.venv`）。  
2. 閱讀 extractor：列表 URL 形態、單片 extract、headers、geo/cookies。  
3. 在對應 `lib/sites/<name>.py` **重寫精簡版**（只保留本管線需要的 list + info + download）。  
4. Runtime **禁止** `import` research 目錄。

### 延後

- Tier 2 原生、直播站、HypnoTube。

---

## 5. 與既有管線的銜接

### capture_frames

- `get_start_page_from_url` / `build_page_url` / `extract_single_page_urls` / 關鍵字轉搜尋 → 改走 registry。  
- `extract_video_info`：**必須**走 §3 單片順序（`extract_info` → `resolve_stream` → yt-dlp+`ydl_opts("info")`），產出 title、duration、`stream_url`、`http_headers`、`web_meta`；不得在 capture 路徑只接 yt-dlp 而忽略 hooks。  
- 九宮格寫入：維持 URL + WEB_META（既有 `video_meta`）。  
- 輸出資料夾命名：可用 adapter.name 或既有 search_ 標籤邏輯，不破壞時間戳格式。

### run_download

- 自 JPG 讀 URL 後 `get_adapter_for_url`。  
- 下載依 §3 **呼叫順序**：`resolve_stream` → yt-dlp+`ydl_opts` → 僅 Pornhub HTML fallback。  
- `is_pornhub_url` + `direct_fetch_pornhub_mp4_stream` 保留，不泛化到他站。  
- META 升級：優先 adapter `extract_info`；否則 `YoutubeDL` + `ydl_opts("info")` 再 `build_web_meta`。

### video_meta

- **不改 schema**；欄位缺失允許 `null`。  
- Smoke 只要求 `title` 與 `webpage_url`（或等同 id+url）非空。

---

## 6. 測試（tasks）

### 檔案

- `tasks/tests/test_sites_smoke.py` — 參數化 per-site  
- 產物：`tasks/tests/site-smoke/<site>/`（預覽圖、low mp4、可選 log）  
- 狀態：`tasks/logs/site-smoke-status.json`（每站 pass/skip/fail 原因）  
- 可選單元：`lib/tests/test_sites_registry.py` — match、build_page_url、mock HTML 列表（無網路）

### 每站五項成功標準

| # | 項目 | 通過條件 |
|---|------|----------|
| 1 | 抓網址 | 熱門列表或固定 search ≥ 1 個 http(s) video URL |
| 2 | 翻頁 | page1 與 page2 的 page URL 不同；page2 再抓到 ≥ 1 URL |
| 3 | 預覽圖 | 對 1 支 URL：`extract_video_info` 成功 + 至少 1 幀或最小九宮格寫入 smoke 目錄 |
| 4 | META | `build_web_meta` 含非空 `title` 與可還原網址（`webpage_url` 或寫入用 URL） |
| 5 | low 影片 | prefer_lowest + 既有短區間；檔案 size > 0 且 ffprobe 可讀 duration |

### 執行策略

- `@pytest.mark.network` + `@pytest.mark.site_smoke`  
- 單站網路／站點錯誤 → **`pytest.skip`**（不整包紅），並寫入 status JSON  
- 預設不納入 `lib/tests` 的 `-q` CI 式快速跑；文件註明：

```powershell
lib\.venv\Scripts\python.exe -m pytest -q tasks\tests\test_sites_smoke.py -m site_smoke
```

- 91porn 無 cookies：skip download／必要時 skip 整站並註明 `needs_cookies`  
- Timeout：單站建議上限（例如 list 30s、download 180s），超時 skip/fail 記錄

### 研究 clone 不測

- `tasks/plugins-research/` 不跑 pytest；僅文件說明如何 clone 與對照。

---

## 7. 錯誤處理

| 情況 | 行為 |
|------|------|
| 未知 host | 通用 adapter：extract_flat + query 分頁；列表失敗則當單 URL |
| 列表空 | 回傳 `[]`；capture 印警告 |
| yt-dlp extract 失敗 | 與現況相同：該片跳過／記 log |
| META 失敗 | 不阻斷下載（既有原則） |
| smoke 失敗 | skip + status JSON，不寫入 `output/` |

---

## 8. 檔案變更清單（預期）

| 路徑 | 動作 |
|------|------|
| `lib/sites/*.py` | 新增 |
| `lib/capture_frames.py` | 改為 registry |
| `lib/run_download.py` | 合併 ydl_opts；小幅 |
| `lib/tests/test_sites_*.py` | 可選單元 |
| `tasks/tests/test_sites_smoke.py` | 新增 smoke |
| `tasks/plugins-research/README.md` | clone 說明 |
| `.gitignore` | 可忽略 research clone 大目錄或 status 產物（實作時定） |
| `README.md` | 簡短：支援站、smoke 指令 |

---

## 9. 實作順序建議

1. `SiteAdapter` + registry + Eporner／Pornhub 遷入（行為對等既有測試）。  
2. Tier 1 其餘四站適配 + mock 單元（分頁／match）。  
3. `capture_frames` / `run_download` 接線。  
4. Clone yellow／hanime 到 `tasks/plugins-research/`，精簡 MissAV、Jable、91porn、hanime。  
5. `tasks/tests/test_sites_smoke.py` 五項標準，跑通並寫 status JSON。  
6. README 補充。

---

## 10. 風險與緩解

| 風險 | 緩解 |
|------|------|
| 站點 HTML 常變 | 列表雙路徑：HTML → extract_flat；smoke skip 不阻開發 |
| 社群站反爬／地區 | headers、referer；91porn cookies env；skip |
| 內建 extractor 與上游 plugin 漂移 | research README 記 commit hash；必要時再對照 |
| smoke 慢／不穩 | mark 隔離、timeout、產物在 tasks |

---

## 11. 決策記錄

| 決策 | 選擇 |
|------|------|
| 架構 | A：registry + 薄適配 |
| 範圍 | Tier 1 + Tier 3；Tier 2／直播延後 |
| Plugin | clone 研究、精簡內建；不安裝進 venv |
| 測試 | 真實連線 smoke；失敗 skip |
| 預設搜尋 | 維持 Eporner |
| 下載 fallback | 僅 Pornhub |

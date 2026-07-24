# Design: 多站成人資源支援（registry + 薄適配）

**日期：** 2026-07-24  
**狀態：** Approved  
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
- **任何站的 HTML 直連 fallback 下載器**（含既有 Pornhub `direct_fetch_pornhub_mp4_stream`）— **移除、不再維護**。

---

## 2. 背景與現況

| 能力 | 現況 |
|------|------|
| 關鍵字搜尋 | 非 URL → 轉 Eporner tag 搜尋 |
| 列表解析 | 硬編碼 Pornhub `viewkey` + Eporner path；失敗才 `yt-dlp extract_flat` |
| 分頁 | query `page=` 通用；Eporner 路徑頁碼特判 |
| 下載 | 通用 yt-dlp；曾有 Pornhub HTML MP4 fallback（**本設計要求刪除**） |
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
[capture_frames]  resolve_playable → 九宮格 + EXIF URL + WEB_META
        │
        ▼
[run_download]    讀 URL → resolve_playable → yt-dlp / adapter stream → low/full
                  （無站點專屬 HTML fallback）
```

### 元件

| 元件 | 職責 |
|------|------|
| `lib/sites/base.py` | `SiteAdapter`：match、search、分頁、列表、單片、ydl_opts、可選 extract_info／resolve_stream（預設回 `None`，不 raise） |
| `lib/sites/registry.py` | 註冊與 `get_adapter_for_url` / `default_adapter` |
| `lib/sites/http_util.py` | 共用 UA、age cookie、fetch HTML（薄封裝） |
| `lib/sites/resolve.py` | `resolve_playable` 共用解析 |
| `lib/sites/generic.py` | 未知 host |
| `lib/sites/<name>.py` | 各站薄適配（見 §4） |
| `lib/sites/__init__.py` | 匯出 registry 公開 API |
| `capture_frames.py` | 列表走 registry；`extract_video_info` 走 `resolve_playable` |
| `run_download.py` | 下載／META 走 `resolve_playable`；**刪除** Pornhub HTML fallback |
| `tasks/plugins-research/` | clone 研究用 repo |
| `tasks/tests/test_sites_smoke.py` | 每站真實連線五項 smoke |

### SiteAdapter 介面（最小）

```python
class SiteAdapter:
    name: str
    domains: tuple[str, ...]

    def match_url(self, url: str) -> bool: ...
    def search_url(self, keyword: str) -> str: ...
    def build_page_url(self, url: str, page_num: int) -> str: ...
    def get_start_page(self, url: str) -> int: ...
    def is_single_video_url(self, url: str) -> bool: ...
    def extract_list_urls(self, page_url: str) -> list[str]: ...
    def ydl_opts(self, purpose: str) -> dict: ...
    def extract_info(self, video_url: str, purpose: str) -> dict | None: ...
    def resolve_stream(self, video_url: str, prefer_lowest: bool) -> dict | None: ...
```

**預設行為（base）：**

- `build_page_url`：query `page=`
- `extract_list_urls`：站點 HTML（若覆寫）否則 `yt-dlp extract_flat`
- `ydl_opts`：`{}`
- `extract_info` / `resolve_stream`：固定 `return None`
- 關鍵字非 URL：default adapter = Eporner

**單片解析（capture 與 download 同一 helper）：**

`lib/sites/resolve.py` → `resolve_playable(adapter, video_url, purpose, prefer_lowest)` 回傳：
`{"info", "stream_url", "http_headers", "source": "extract_info"|"resolve_stream"|"yt_dlp"}`

順序：

1. `extract_info` → info；若含可播 URL → 截幀與下載同權使用  
2. 否則 `resolve_stream` → 同權使用  
3. 否則 `yt-dlp` + `ydl_opts(purpose)`  
4. 仍失敗 → 該片失敗；smoke **skip**  

**不再有** 第 5 步站點 HTML fallback。

Smoke #3／#4／#5 必須呼叫同一 `resolve_playable`。

**Tier 3：** 優先 ydl_opts + 列表 HTML；不足再 `resolve_stream`／`extract_info`。禁止 runtime import research。

**解析優先序：** video URL → list URL 分頁 → 關鍵字 Eporner → 文字檔。

---

## 4. 站點清單（本輪）

### Tier 1 — 原生 yt-dlp（必做）

| 站 | 列表策略 | 分頁 | 備註 |
|----|----------|------|------|
| **Eporner** | path regex | 路徑頁碼 | 預設搜尋站 |
| **Pornhub** | viewkey HTML + extract_flat | `page=` | **僅** yt-dlp／adapter hooks，無 HTML fallback |
| **XVideos** | HTML 或 extract_flat | 站點規則／query | |
| **xHamster** | 同上 | 同上 | |
| **XNXX** | 同上 | 同上 | |
| **SpankBang** | 同上 | 同上 | |

### Tier 3 — 社群研究後精簡內建

| 站 | 研究來源 | 內建方向 |
|----|----------|----------|
| **MissAV** | yt-dlp-plugin-yellow | ydl_opts → resolve_stream |
| **Jable.tv** | 同上 | 同上 |
| **91porn** | 同上 | `SITE_91PORN_COOKIES` = Netscape cookies.txt 路徑 |
| **hanime.tv** | hanime-plugin | ydl_opts → hooks |
| **HentaiHaven / hstream.moe** | 若同源 | optional |

### 延後

- Tier 2 原生、直播站、HypnoTube。

---

## 5. 與既有管線的銜接

### capture_frames

- 列表／分頁／搜尋 → registry  
- `extract_video_info` → `resolve_playable(..., purpose="info")`  
- 九宮格：URL + WEB_META  

### run_download

- 讀 URL → adapter → `resolve_playable`  
- **刪除** `select_pornhub_mp4_url`、`direct_fetch_pornhub_mp4_stream`、`is_pornhub_url` 及 except 內 fallback 分支  
- 相關單元測試改為刪除或改測「yt-dlp 失敗即失敗、不進 fallback」  

### video_meta

- schema 不變；smoke 要求 title + webpage_url  

---

## 6. 測試（tasks）

| # | 項目 | 通過條件 |
|---|------|----------|
| 1 | 抓網址 | ≥1 video URL |
| 2 | 翻頁 | page1≠page2 且 page2 有 URL |
| 3 | 預覽圖 | info + ≥1 幀於 smoke 目錄 |
| 4 | META | title + webpage_url |
| 5 | low 影片 | size>0 + ffprobe |

- `tasks/tests/test_sites_smoke.py`，`@pytest.mark.network` + `site_smoke`  
- 失敗 → skip + `tasks/logs/site-smoke-status.json`  
- 產物：`tasks/tests/site-smoke/<site>/`  

```powershell
lib\.venv\Scripts\python.exe -m pytest -q tasks\tests\test_sites_smoke.py -m site_smoke
```

---

## 7. 錯誤處理

| 情況 | 行為 |
|------|------|
| 未知 host | GenericAdapter |
| 列表空 | `[]` + 警告 |
| yt-dlp 失敗 | 該片失敗（無 HTML fallback） |
| META 失敗 | 不阻斷下載 |
| smoke 失敗 | skip + status JSON |

---

## 8. 檔案變更清單

| 路徑 | 動作 |
|------|------|
| `lib/sites/*.py` | 新增 |
| `lib/capture_frames.py` | registry |
| `lib/run_download.py` | resolve_playable；**移除** Pornhub fallback |
| `lib/tests/test_run_download_meta.py` | 移除 fallback 測試 |
| `lib/tests/test_sites_*.py` | 單元 |
| `tasks/tests/test_sites_smoke.py` | smoke |
| `tasks/plugins-research/README.md` | clone 說明 |
| `README.md` | 支援站與 smoke 指令 |

---

## 9. 實作順序

1. sites base + registry + resolve + Eporner／Pornhub 遷入  
2. Tier 1 其餘站  
3. capture／download 接線；**刪除 Pornhub fallback**  
4. research clone → Tier 3  
5. smoke 測試  
6. README  

---

## 10. 決策記錄

| 決策 | 選擇 |
|------|------|
| 架構 | registry + 薄適配 |
| 範圍 | Tier 1 + Tier 3 |
| Plugin | 研究 clone，精簡內建 |
| 測試 | 真實 smoke，失敗 skip |
| 預設搜尋 | Eporner |
| 下載 fallback | **無**（含 Pornhub 一併移除） |

# Multi-Site Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a site registry so Tier 1 (Pornhub, XVideos, xHamster, XNXX, SpankBang, Eporner) and Tier 3 (MissAV, Jable, 91porn, hanime) support list URL extraction, paging, preview frames, WEB_META, and low-quality download — with real-network smoke tests under `tasks/tests`, and **no** Pornhub HTML download fallback.

**Architecture:** `lib/sites/` adapters + shared `resolve_playable`; `capture_frames` / `run_download` call registry only. Community plugins are cloned under `tasks/plugins-research/` for study, then logic is rewritten into adapters (no runtime plugin import).

**Tech Stack:** Python 3.12, yt-dlp, ffmpeg/ffprobe, pytest, Pillow, existing `video_meta`.

**Spec:** `lib/docs/superpowers/specs/2026-07-24-multi-site-support-design.md`

---

## File map

| File | Responsibility |
|------|----------------|
| `lib/sites/base.py` | `SiteAdapter` base |
| `lib/sites/http_util.py` | fetch HTML, default headers |
| `lib/sites/resolve.py` | `resolve_playable` |
| `lib/sites/registry.py` | register / lookup / default Eporner |
| `lib/sites/generic.py` | unknown hosts |
| `lib/sites/eporner.py` … | per-site adapters |
| `lib/sites/__init__.py` | public API + register all |
| `lib/capture_frames.py` | use registry + resolve_playable |
| `lib/run_download.py` | merge ydl_opts; **delete** Pornhub fallback |
| `lib/tests/test_sites_registry.py` | unit tests (no network) |
| `lib/tests/test_run_download_meta.py` | remove pornhub fallback tests |
| `tasks/tests/test_sites_smoke.py` | network smoke |
| `tasks/plugins-research/README.md` | how to clone research repos |

---

### Task 1: SiteAdapter base, http_util, generic, registry skeleton

**Files:**
- Create: `lib/sites/base.py`, `http_util.py`, `generic.py`, `registry.py`, `__init__.py`
- Test: `lib/tests/test_sites_registry.py`

- [ ] **Step 1:** Write unit tests for hostname match, default page= builder, generic extract_flat path (mocked).

- [ ] **Step 2:** Implement base + http_util + generic + empty registry with `get_adapter_for_url`, `default_adapter`, `register`.

- [ ] **Step 3:** `pytest lib/tests/test_sites_registry.py -q` PASS; commit.

---

### Task 2: resolve_playable

**Files:**
- Create: `lib/sites/resolve.py`
- Test: extend `test_sites_registry.py`

- [ ] **Step 1:** Tests: extract_info with url wins; resolve_stream wins; yt-dlp path when hooks None (mock YoutubeDL).

- [ ] **Step 2:** Implement `resolve_playable(adapter, video_url, purpose="info", prefer_lowest=False)`.

- [ ] **Step 3:** pytest PASS; commit.

---

### Task 3: Eporner + Pornhub adapters (migrate existing logic)

**Files:**
- Create: `lib/sites/eporner.py`, `lib/sites/pornhub.py`
- Modify: `lib/sites/__init__.py` to register them
- Test: unit tests for page path (Eporner), viewkey list parse (Pornhub mock HTML)

- [ ] **Step 1:** Move Eporner path paging + list regex from `capture_frames` into adapter.

- [ ] **Step 2:** Move Pornhub viewkey list + page= into adapter; **no** download HTML fallback in adapter.

- [ ] **Step 3:** pytest PASS; commit.

---

### Task 4: Tier 1 remaining (XVideos, xHamster, XNXX, SpankBang)

**Files:**
- Create: `lib/sites/xvideos.py`, `xhamster.py`, `xnxx.py`, `spankbang.py`
- Prefer: base `page=` + extract_flat first; HTML list only if known simple patterns.

- [ ] **Step 1:** Unit tests match_url + build_page_url per site.

- [ ] **Step 2:** Implement adapters; register.

- [ ] **Step 3:** pytest PASS; commit.

---

### Task 5: Wire capture_frames + run_download; remove Pornhub fallback

**Files:**
- Modify: `lib/capture_frames.py`, `lib/run_download.py`
- Modify: `lib/tests/test_run_download_meta.py` (delete fallback tests)

- [ ] **Step 1:** `extract_urls_from_target` / `build_page_url` / `get_start_page` / keyword search → sites API.

- [ ] **Step 2:** `extract_video_info` → `resolve_playable`.

- [ ] **Step 3:** `run_download`: merge `adapter.ydl_opts`; on yt-dlp failure **do not** call Pornhub fallback; delete `select_pornhub_mp4_url`, `direct_fetch_pornhub_mp4_stream`, `is_pornhub_url`.

- [ ] **Step 4:** Remove `test_pornhub_fallback_*`; run `lib/tests` PASS; commit.

---

### Task 6: Plugin research clones + Tier 3 adapters

**Files:**
- Create: `tasks/plugins-research/README.md`
- Create: `lib/sites/missav.py`, `jable.py`, `porn91.py`, `hanime.py`
- Clone (not installed): yellow + hanime plugin into `tasks/plugins-research/`

- [ ] **Step 1:** README with clone commands and commit-hash note.

- [ ] **Step 2:** Clone/study; implement list + ydl_opts (+ resolve_stream if needed).

- [ ] **Step 3:** 91porn: cookies from `SITE_91PORN_COOKIES` if set.

- [ ] **Step 4:** unit match tests; commit (research clones may be gitignored).

---

### Task 7: tasks smoke tests

**Files:**
- Create: `tasks/tests/test_sites_smoke.py`
- Optional: update `.gitignore` for `tasks/tests/site-smoke/`, status json

- [ ] **Step 1:** Parametrize Tier1+Tier3; five checks; skip on failure; write status JSON.

- [ ] **Step 2:** Run smoke (network); document results; commit test code (not large media if possible).

---

### Task 8: README

- [ ] Document supported sites + smoke command; commit.

---

## Done when

- No Pornhub HTML fallback code or tests remain.
- Registry drives list/page/search for Tier 1 (+ Tier 3 best-effort).
- `lib/tests` green offline.
- `tasks/tests/test_sites_smoke.py` exists; network run records status JSON.

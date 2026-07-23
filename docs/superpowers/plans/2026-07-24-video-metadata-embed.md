# Video Metadata Embed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Embed readable web page meta and full original/translated SRT (with `[Sxx]` and cue timings) into each downloaded MP4, without changing the JPG EXIF download flow.

**Architecture:** New `video_meta.py` owns comment section parse/merge and mutagen MP4 read/write. `run_download.py` writes `WEB_META_V1` after successful download. `translate_srt_openrouter.py` keeps speaker prefixes and cue `time` unchanged during translation. `run_subtitle.py` writes full original + translated SRT into the same comment after soft/hard subtitle packaging. Meta failures log warnings and never fail download/subtitle success.

**Tech Stack:** Python 3, mutagen, yt-dlp, existing Pillow/ffmpeg pipelines, pytest.

**Spec:** `docs/superpowers/specs/2026-07-24-video-metadata-embed-design.md`

---

## File map

| File | Responsibility |
|------|----------------|
| Create `video_meta.py` | Sectioned comment codec; `build_web_meta`; mutagen merge write/read; optional CLI `show`/`export` |
| Create `tests/test_video_meta.py` | Unit tests for parse/merge/build/read-write |
| Modify `translate_srt_openrouter.py` | Preserve `[Sxx]` + `time` through translation |
| Modify `tests/test_run_subtitle.py` (+ new translate tests if needed) | Expect speaker labels kept |
| Modify `run_download.py` | After download success, write WEB_META |
| Modify `run_subtitle.py` | Keep labels; after embed/burn, write dual SRT meta |
| Modify `requirements.txt` | Add `mutagen` |
| Modify `README.md` | Document meta embed + speaker retention |

---

### Task 1: `video_meta` section codec + web_meta builder

**Files:**
- Create: `video_meta.py`
- Create: `tests/test_video_meta.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Add mutagen dependency**

```text
# requirements.txt — append
mutagen
```

Run: `.\.venv\Scripts\python.exe -m pip install mutagen -q`

- [ ] **Step 2: Write failing tests for comment sections and build_web_meta**

```python
# tests/test_video_meta.py
from video_meta import (
    parse_comment_sections,
    merge_comment_sections,
    build_web_meta,
    SECTION_WEB,
    SECTION_ORIGINAL_SRT,
    SECTION_TRANSLATED_SRT,
)


def test_parse_and_merge_preserves_unknown_and_order():
    raw = (
        "===WEB_META_V1===\n"
        '{"schema":"web_meta_v1"}\n'
        "===ORIGINAL_SRT===\n"
        "1\n00:00:00,000 --> 00:00:01,000\n[S01] Hi\n"
        "===CUSTOM===\n"
        "keep-me\n"
    )
    sections = parse_comment_sections(raw)
    assert sections[SECTION_WEB].startswith("{")
    assert "[S01] Hi" in sections[SECTION_ORIGINAL_SRT]
    assert sections["CUSTOM"] == "keep-me"

    merged = merge_comment_sections(
        sections,
        {
            SECTION_TRANSLATED_SRT: "1\n00:00:00,000 --> 00:00:01,000\n[S01] 你好\n",
        },
    )
    assert "===WEB_META_V1===" in merged
    assert "===ORIGINAL_SRT===" in merged
    assert "===TRANSLATED_SRT===" in merged
    assert "===CUSTOM===" in merged
    assert "[S01] 你好" in merged


def test_build_web_meta_nulls_and_times():
    info = {
        "extractor": "PornHub",
        "id": "ph1",
        "title": "T",
        "uploader": "U",
        "uploader_id": "u1",
        "tags": ["a"],
        "categories": ["b"],
        "cast": ["C"],
        "view_count": 10,
        "like_count": 2,
        "comment_count": 1,
        "duration": 90,
        "duration_string": "1:30",
        "upload_date": "20200102",
        "timestamp": 1577923200,
        "thumbnail": "https://x",
        "webpage_url": "https://y",
        "age_limit": 18,
    }
    meta = build_web_meta(info)
    assert meta["schema"] == "web_meta_v1"
    assert meta["tags"] == ["a"]
    assert meta["description"] is None  # missing key -> null
    assert meta["average_rating"] is None
    assert meta["duration"] == 90
    assert meta["duration_string"] == "1:30"
    assert meta["upload_date"] == "20200102"
    assert meta["timestamp"] == 1577923200
    assert meta["meta_written_at"]  # ISO8601 non-empty
    assert meta["categories"] == ["b"]


def test_build_web_meta_empty_arrays():
    meta = build_web_meta({"title": "only"})
    assert meta["tags"] == []
    assert meta["categories"] == []
    assert meta["cast"] == []
    assert meta["title"] == "only"
    assert meta["extractor"] is None
```

- [ ] **Step 3: Run tests — expect FAIL (module missing)**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_video_meta.py -v`  
Expected: import error / FAIL

- [ ] **Step 4: Implement `video_meta.py` (codec + builder only)**

Implement at minimum:

```python
# video_meta.py — core API (skeleton; fill fully)
SECTION_WEB = "WEB_META_V1"
SECTION_ORIGINAL_SRT = "ORIGINAL_SRT"
SECTION_TRANSLATED_SRT = "TRANSLATED_SRT"

def parse_comment_sections(comment: str | None) -> dict[str, str]:
    """Parse ===NAME=== sections; values are body text without trailing extra blanks if practical."""

def merge_comment_sections(
    existing: dict[str, str],
    updates: dict[str, str],
) -> str:
    """Apply only keys present in updates (values are non-empty or empty strings to set).
    Contract: omit a key to leave that section unchanged — never treat missing/None as delete.
    Preserve unknown keys; emit fixed section order WEB, ORIGINAL_SRT, TRANSLATED_SRT
    then any other keys in stable order.
    """

def build_web_meta(info: dict) -> dict:
    """All schema keys present; scalars None; arrays []; set meta_written_at UTC ISO8601.
    duration_string: use info.get or format from duration if missing.
    """
```

Rules from spec:
- Scalar missing → `null`
- Array missing → `[]`
- Do not store stream URLs / cookies / formats

- [ ] **Step 5: Run tests — expect PASS**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_video_meta.py -v`  
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add requirements.txt video_meta.py tests/test_video_meta.py
git commit -m "Add video_meta comment codec and web meta builder"
```

---

### Task 2: mutagen MP4 read/write helpers

**Files:**
- Modify: `video_meta.py`
- Modify: `tests/test_video_meta.py`

- [ ] **Step 1: Write failing tests for merge_write / read_mp4_meta**

Use a tiny real MP4 if available under `low_videos/`, or create via ffmpeg:

```python
import shutil
import subprocess
from pathlib import Path
import video_meta

def _tiny_mp4(path: Path):
    # Prefer copy smallest project mp4; else ffmpeg color source
    ...

def test_write_web_then_srt_roundtrip(tmp_path):
    mp4 = tmp_path / "t.mp4"
    _tiny_mp4(mp4)
    web = video_meta.build_web_meta({
        "title": "Demo",
        "uploader": "Author",
        "upload_date": "20200102",
        "duration": 12,
        "webpage_url": "https://example.com/v",
    })
    video_meta.merge_write_mp4_meta(mp4, web_meta=web)
    got = video_meta.read_mp4_meta(mp4)
    assert got["web_meta"]["title"] == "Demo"
    assert got["title"] == "Demo"  # ©nam
    assert got["artist"] == "Author"

    orig = "1\n00:00:00,000 --> 00:00:01,000\n[S01] Hi\n"
    trans = "1\n00:00:00,000 --> 00:00:01,000\n[S01] 你好\n"
    video_meta.merge_write_mp4_meta(
        mp4,
        original_srt=orig,
        translated_srt=trans,
    )
    got2 = video_meta.read_mp4_meta(mp4)
    assert got2["web_meta"]["title"] == "Demo"
    assert got2["original_srt"].strip() == orig.strip()
    assert got2["translated_srt"].strip() == trans.strip()
    # full SRT format: must contain time arrow
    assert "-->" in got2["original_srt"]
    assert "-->" in got2["translated_srt"]


def test_merge_write_none_kwargs_preserve_sections(tmp_path):
    """None means no-op for that section (download WEB must not wipe SRT)."""
    mp4 = tmp_path / "t.mp4"
    _tiny_mp4(mp4)
    video_meta.merge_write_mp4_meta(
        mp4,
        web_meta=video_meta.build_web_meta({"title": "A"}),
        original_srt="1\n00:00:00,000 --> 00:00:01,000\n[S01] Hi\n",
        translated_srt="1\n00:00:00,000 --> 00:00:01,000\n[S01] 你好\n",
    )
    video_meta.merge_write_mp4_meta(
        mp4,
        web_meta=video_meta.build_web_meta({"title": "B"}),
        # original_srt / translated_srt omitted (None) — must remain
    )
    got = video_meta.read_mp4_meta(mp4)
    assert got["web_meta"]["title"] == "B"
    assert "[S01] Hi" in (got["original_srt"] or "")
    assert "[S01] 你好" in (got["translated_srt"] or "")
```

Use ffmpeg one-liner for `_tiny_mp4` (do not depend on `low_videos/`):

```python
def _tiny_mp4(path: Path):
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
    )
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_video_meta.py::test_write_web_then_srt_roundtrip -v`

- [ ] **Step 3: Implement mutagen helpers**

```python
def merge_write_mp4_meta(
    path: Path | str,
    *,
    web_meta: dict | None = None,
    original_srt: str | None = None,
    translated_srt: str | None = None,
) -> None:
    """Read existing ©cmt, merge sections, write back.
    None kwargs mean leave that section unchanged (do not delete).
    Only non-None kwargs are written into updates dict.
    If web_meta provided: also set ©nam/title, ©ART/artist, ©day/date from web_meta.
    Raise on hard failures so unit tests see them; run_download / run_subtitle wrap in try/except.
    """

def read_mp4_meta(path: Path | str) -> dict:
    """Return title, artist, date, web_meta, original_srt, translated_srt, raw_comment."""
```

Mapping:
- `©nam` ← `web_meta["title"]`
- `©ART` ← `uploader` or first of `cast`
- `©day` ← format `upload_date` to `YYYY-MM-DD` if 8 digits

- [ ] **Step 4: Run tests — expect PASS**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_video_meta.py -v`

- [ ] **Step 5: Commit**

```bash
git add video_meta.py tests/test_video_meta.py
git commit -m "Add mutagen MP4 metadata read/write helpers"
```

---

### Task 3: Preserve `[Sxx]` and cue times in translation

**Files:**
- Modify: `translate_srt_openrouter.py`
- Create or modify: `tests/test_translate_srt_openrouter.py` (create if missing)
- Modify: `tests/test_run_subtitle.py` (assertion expects labels)

- [ ] **Step 1: Write failing unit tests for speaker reattach**

```python
# tests/test_translate_srt_openrouter.py
from translate_srt_openrouter import split_speaker_prefix, attach_speaker_prefix, translate_cues

def test_split_and_attach():
    p, body = split_speaker_prefix("[S01] Hello")
    assert p == "[S01] "
    assert body == "Hello"
    assert attach_speaker_prefix(p, "你好") == "[S01] 你好"
    assert attach_speaker_prefix(p, "[S01] 你好") == "[S01] 你好"  # no double


def test_translate_cues_preserves_prefix_and_time(monkeypatch):
    cues = [
        {"id": 1, "time": "00:00:00,480 --> 00:00:01,660", "text": "[S01] Hello"},
        {"id": 2, "time": "00:00:02,000 --> 00:00:03,000", "text": "No label"},
    ]
    import translate_srt_openrouter as m

    def fake_batch(batch, api_key, model, session):
        table = {1: "你好", 2: "無標籤"}
        return {int(c["id"]): table[int(c["id"])] for c in batch}

    monkeypatch.setattr(m, "_translate_batch", fake_batch)
    out = translate_cues(cues, "key", model="m", batch_size=60)
    assert out[0]["text"] == "[S01] 你好"
    assert out[0]["time"] == "00:00:00,480 --> 00:00:01,660"
    assert out[1]["text"] == "無標籤"
    assert out[1]["time"] == "00:00:02,000 --> 00:00:03,000"
```

- [ ] **Step 2: Run — expect FAIL**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_translate_srt_openrouter.py -v`

- [ ] **Step 3: Implement preserve logic**

In `translate_srt_openrouter.py`:

1. Add `split_speaker_prefix(text) -> tuple[str, str]` and `attach_speaker_prefix(prefix, body) -> str`.
2. Change `translate_cues`:
   - Do **not** call `strip_speaker_labels` on the whole list for output.
   - For each cue, split prefix/body; send **body only** in `_translate_batch` input texts.
   - After response, set `text = attach_speaker_prefix(prefix, translated_body)`; keep original `time` and `id`.
3. Optional: tweak system prompt: do not add `[Sxx]` prefixes in output.
4. Keep `strip_speaker_labels` function for any external use but stop using it in the default translate path.

Important: `_translate_batch` currently builds `{"id","text"}` from cues — ensure those texts are body-only (either strip when building minimal_input, or pass pre-split cues).

- [ ] **Step 4: Fix `tests/test_run_subtitle.py`**

Change expected SRT from:

```python
== "1\n00:00:00,000 --> 00:00:01,000\nHi"
```

to:

```python
== "1\n00:00:00,000 --> 00:00:01,000\n[S01] Hi"
```

(because `translate_cues` is monkeypatched to identity and must no longer be stripped by `process_video`).

- [ ] **Step 5: Remove strip in `run_subtitle.py`**

In `process_video`, change:

```python
translated = strip_speaker_labels(translate_cues(...))
```

to:

```python
translated = translate_cues(cues, api_key, model_name)
```

Remove unused `strip_speaker_labels` import if unused.

- [ ] **Step 6: Run tests**

Run:
```
.\.venv\Scripts\python.exe -m pytest tests/test_translate_srt_openrouter.py tests/test_run_subtitle.py -v
```
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add translate_srt_openrouter.py run_subtitle.py tests/test_translate_srt_openrouter.py tests/test_run_subtitle.py
git commit -m "Keep speaker labels and cue times through translation"
```

---

### Task 4: Wire WEB_META into `run_download`

**Files:**
- Modify: `run_download.py`
- Create: `tests/test_run_download_meta.py` (or extend existing download tests if any)

- [ ] **Step 1: Write a focused unit test with mocks**

```python
# tests/test_run_download_meta.py
import run_download


def test_write_web_meta_for_download_calls_merge(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    captured = {}

    class FakeYDL:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def extract_info(self, url, download=False):
            return {"title": "FromNet", "uploader": "U", "tags": ["t"], "webpage_url": url}

    monkeypatch.setattr(run_download.yt_dlp, "YoutubeDL", FakeYDL)

    def fake_merge(path, **kwargs):
        captured["path"] = str(path)
        captured["web_meta"] = kwargs.get("web_meta")

    monkeypatch.setattr(run_download.video_meta, "merge_write_mp4_meta", fake_merge)
    # if import style is `import video_meta` then patch video_meta.merge_write_mp4_meta
    # and ensure run_download uses that name.

    run_download.write_web_meta_for_download(str(video), "https://example.com/v")
    assert captured["web_meta"]["title"] == "FromNet"
    assert captured["web_meta"]["tags"] == ["t"]
    assert "duration" in captured["web_meta"]  # schema keys present
```

Extract helper in `run_download.py`:

```python
def write_web_meta_for_download(video_path: str, video_url: str, info: dict | None = None) -> None:
    """Best-effort; log warning on failure; never raise."""
```

Download loop:

```python
if download_success:
    write_web_meta_for_download(target_video_file, video_url, info=None)
```

- [ ] **Step 2: Implement helper**

Logic:
1. If `info` is None, `yt_dlp.YoutubeDL({quiet, no_warnings}).extract_info(url, download=False)` inside try.
2. `web = build_web_meta(info)`
3. `merge_write_mp4_meta(path, web_meta=web)` only — do not pass original_srt/translated_srt (preserve any existing).
4. On any exception: `print` warning with exception; return.

Optional optimization (same task if easy): during successful `ydl.download`, capture info via a progress hook or `extract_info` before download to avoid second network call — not required for v1; double fetch OK.

- [ ] **Step 3: Call helper after every `download_success`** (both quality paths already funnel to same success block).

**Do not change:** EXIF read, JPG move logic, format selection, fallback parser.

- [ ] **Step 4: Run tests**

```
.\.venv\Scripts\python.exe -m pytest tests/test_run_download_meta.py tests/test_video_meta.py -v
```

- [ ] **Step 5: Commit**

```bash
git add run_download.py tests/test_run_download_meta.py
git commit -m "Write web page meta into MP4 after successful download"
```

---

### Task 5: Wire dual SRT meta into `run_subtitle`

**Files:**
- Modify: `run_subtitle.py`
- Modify: `tests/test_run_subtitle.py`

- [ ] **Step 1: Extend process_video tests**

```python
def test_process_video_writes_srt_meta(tmp_path, monkeypatch):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"video")
    # need a real minimal mp4 for mutagen OR mock merge_write_mp4_meta
    calls = {}
    def capture(path, **kwargs):
        calls["path"] = path
        calls.update(kwargs)
    monkeypatch.setattr(run_subtitle.video_meta, "merge_write_mp4_meta", capture)
    # or monkeypatch video_meta.merge_write_mp4_meta if imported as module

    backend = FakeBackend(cues=[{
        "id": 1,
        "time": "00:00:00,000 --> 00:00:01,000",
        "text": "[S01] Hi",
    }])
    monkeypatch.setattr(run_subtitle, "translate_cues", lambda cues, *_: [
        {**cues[0], "text": "[S01] 你好"}
    ])
    monkeypatch.setattr(run_subtitle, "_embed_soft_subtitle", lambda *a, **k: video)

    run_subtitle.process_video(video, backend, "key", "model", True)

    assert "-->" in calls["original_srt"]
    assert "[S01] Hi" in calls["original_srt"]
    assert "[S01] 你好" in calls["translated_srt"]
    assert calls["original_srt"].count("-->") >= 1
```

Also: skip-ASR path still calls merge with translated from file; original_srt only if already in comment (pass None for original to preserve).

- [ ] **Step 2: Restructure `process_video` then write meta**

Do **not** `return _burn_hard_subtitle(...)` / `return _embed_soft_subtitle(...)` directly.

```python
original_cues = None  # set when ASR runs
# ... ASR + translate + write output_srt as today ...

if _uses_hard_subtitle(video):
    output_video = _burn_hard_subtitle(video, output_srt, output_video, force)
else:
    output_video = _embed_soft_subtitle(video, output_srt, output_video, force)

# Always attempt meta write after packaging (including skip-ASR path)
try:
    translated_text = output_srt.read_text(encoding="utf-8-sig")
    original_text = (
        format_srt(original_cues) if original_cues is not None else None
    )
    video_meta.merge_write_mp4_meta(
        output_video,
        original_srt=original_text,      # None => leave existing ORIGINAL_SRT
        translated_srt=translated_text,  # full SRT file text, original format
    )
except Exception as exc:
    print(f"  [!] 寫入字幕 meta 失敗: {exc}", flush=True)

return output_video
```

`merge_write_mp4_meta`: **None = leave section unchanged** (aligned with Task 1–2).

Verify soft and hard paths both `-map_metadata 0` (already true); mutagen rewrite after is the safety net.

- [ ] **Step 3: Run subtitle tests**

```
.\.venv\Scripts\python.exe -m pytest tests/test_run_subtitle.py tests/test_translate_srt_openrouter.py -v
```

- [ ] **Step 4: Commit**

```bash
git add run_subtitle.py tests/test_run_subtitle.py
git commit -m "Embed original and translated SRT into MP4 metadata"
```

---

### Task 6: CLI show/export + README

**Files:**
- Modify: `video_meta.py` (`if __name__ == "__main__"`)
- Modify: `README.md`

- [ ] **Step 1: Add CLI**

```text
python video_meta.py show path.mp4
python video_meta.py export path.mp4 --out-dir dir/
```

- `show`: print title/artist/date, pretty web_meta JSON, SRT lengths / first cue line
- `export`: write `stem.web_meta.json`, `stem.orig.srt`, `stem.translated.srt` when present

- [ ] **Step 2: Manual smoke (optional if no network)**

If a local mp4 with meta exists after tests, run show once.

- [ ] **Step 3: README updates**

- Note MP4 embeds web meta after download.
- Note subtitle meta stores **full original-format SRT** (index + timestamps + text) for original and translated, including `[Sxx]`.
- Note translation **keeps** speaker labels (replace old “會移除” wording).
- Document `python video_meta.py show|export`.
- State JPG EXIF download flow unchanged.

- [ ] **Step 4: Full test suite**

```
.\.venv\Scripts\python.exe -m pytest tests/test_video_meta.py tests/test_translate_srt_openrouter.py tests/test_run_subtitle.py tests/test_run_download_meta.py -v
```

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add video_meta.py README.md
git commit -m "Add video_meta CLI and document MP4 metadata embed"
```

---

## Implementation notes (for agents)

1. **Full SRT format only** for subtitle meta: use `format_srt` / file contents — never strip to plain lines.
2. **Do not touch** `capture_frames.py` EXIF URL write or `get_video_url_from_image` behavior.
3. **Meta failure never fails** download or subtitle main result.
4. **Eporner:** no HTML scrape; missing fields null/`[]`.
5. Prefer TDD order: test → fail → implement → pass → commit per task.
6. Use project venv: `G:\pornhub\.venv\Scripts\python.exe`.
7. **Merge contract:** keyword `None` / omitted section = **preserve existing**; only pass strings/dicts for sections to update. Never wipe SRT when writing WEB, never wipe WEB when writing SRT-only.

---

## Execution handoff

After this plan is approved by the plan reviewer and the user picks an execution mode:

1. **Subagent-Driven (recommended)** — superpowers:subagent-driven-development  
2. **Inline Execution** — superpowers:executing-plans  

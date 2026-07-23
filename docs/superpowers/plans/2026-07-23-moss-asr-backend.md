# MOSS ASR Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Windows CUDA 環境加入可由 `ASR_BACKEND=moss` 選用的 MOSS-Transcribe-Diarize，同時保持 Whisper 為預設 backend。

**Architecture:** 把 ASR 模型載入與 segment 正規化抽到 `asr_backends.py`，由 `run_subtitle.py` 共用既有翻譯、SRT 與 FFmpeg 流程。Whisper 與 MOSS 使用各自 venv；`run_subtitle.bat` 按 backend 選 interpreter，`install_moss.bat` 建立固定版本的 Windows CUDA 環境並呼叫 `moss_setup.py` 下載 ModelScope snapshot。

**Tech Stack:** Python 3.12、PyTorch CUDA 12.8、Transformers 5.6.x、ModelScope、MOSS-Transcribe-Diarize commit `9990574e6ac62390a21bcce25a914d66ac92c25e`、pytest、Windows Batch。

---

## 檔案配置

- Create `asr_backends.py`：backend 選擇、Whisper adapter、MOSS adapter、MOSS segment → cue。
- Create `moss_setup.py`：CUDA 驗證與 ModelScope snapshot 下載。
- Create `install_moss.bat`：建立 MOSS venv 與安裝固定依賴。
- Create `moss/.gitignore`：排除 MOSS venv 與模型 cache。
- Create `tests/test_asr_backends.py`：backend 與 cue 單元測試。
- Create `tests/test_moss_setup.py`：CUDA guard 與 snapshot 下載單元測試。
- Create `tests/test_run_subtitle.py`：共用字幕流程 regression test。
- Create `tests/test_run_subtitle_bat.py`：Batch backend interpreter smoke test。
- Modify `run_subtitle.py`：改用 backend adapter。
- Modify `run_subtitle.bat`：依 `ASR_BACKEND` 選擇 venv。
- Modify `README.md`：補上安裝、切換與推理命令。

### Task 1: Backend 名稱與 MOSS cue 轉換

**Files:**
- Create: `tests/test_asr_backends.py`
- Create: `asr_backends.py`

- [ ] **Step 1: 寫 backend 與 cue 的 failing tests**

```python
from types import SimpleNamespace

import pytest

from asr_backends import moss_segments_to_cues, resolve_backend


def test_resolve_backend_defaults_to_whisper():
    assert resolve_backend({}) == "whisper"


def test_resolve_backend_accepts_moss_case_insensitively():
    assert resolve_backend({"ASR_BACKEND": " MOSS "}) == "moss"


def test_resolve_backend_rejects_unknown_value():
    with pytest.raises(ValueError, match="whisper、moss"):
        resolve_backend({"ASR_BACKEND": "other"})


def test_moss_segments_to_cues_preserves_speaker():
    segments = [
        SimpleNamespace(start=0.48, end=1.66, speaker="S01", text="Hello"),
        SimpleNamespace(start=12.26, end=13.81, speaker="[S02]", text="World"),
    ]

    assert moss_segments_to_cues(segments) == [
        {"id": 1, "time": "00:00:00,480 --> 00:00:01,660", "text": "[S01] Hello"},
        {"id": 2, "time": "00:00:12,260 --> 00:00:13,810", "text": "[S02] World"},
    ]


@pytest.mark.parametrize(
    "segment",
    [
        SimpleNamespace(start=-1, end=1, speaker="S01", text="bad"),
        SimpleNamespace(start=2, end=1, speaker="S01", text="bad"),
        SimpleNamespace(start=0, end=1, speaker="", text="bad"),
    ],
)
def test_moss_segments_to_cues_rejects_invalid_segment(segment):
    with pytest.raises(ValueError, match="MOSS segment"):
        moss_segments_to_cues([segment])


def test_moss_segments_to_cues_rejects_empty_transcript():
    with pytest.raises(RuntimeError, match="有效字幕"):
        moss_segments_to_cues([])
```

- [ ] **Step 2: 執行測試並確認因 module 不存在而失敗**

Run: `python -m pytest tests/test_asr_backends.py -v`

Expected: FAIL，包含 `ModuleNotFoundError: No module named 'asr_backends'`。

- [ ] **Step 3: 實作最小 backend 解析與 cue 轉換**

```python
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def resolve_backend(environment: Mapping[str, str] | None = None) -> str:
    environment = os.environ if environment is None else environment
    backend = environment.get("ASR_BACKEND", "whisper").strip().lower() or "whisper"
    if backend not in {"whisper", "moss"}:
        raise ValueError("ASR_BACKEND 只允許 whisper、moss。")
    return backend


def srt_time(seconds: float) -> str:
    milliseconds = round(float(seconds) * 1000)
    if milliseconds < 0:
        raise ValueError("時間戳不可小於 0。")
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_part, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d},{millis:03d}"


def moss_segments_to_cues(segments: list[Any]) -> list[dict[str, Any]]:
    cues = []
    for index, segment in enumerate(segments, start=1):
        start = float(segment.start)
        end = float(segment.end)
        speaker = str(segment.speaker).strip()
        text = str(segment.text).strip()
        if start < 0 or end <= start or not speaker:
            raise ValueError(f"MOSS segment 無效：{segment!r}")
        if not text:
            continue
        if not speaker.startswith("["):
            speaker = f"[{speaker}]"
        cues.append(
            {
                "id": index,
                "time": f"{srt_time(start)} --> {srt_time(end)}",
                "text": f"{speaker} {text}",
            }
        )
    if not cues:
        raise RuntimeError("MOSS 沒有產生有效字幕段落。")
    return cues
```

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_asr_backends.py -v`

Expected: PASS。

### Task 2: Whisper 與 MOSS adapter

**Files:**
- Modify: `tests/test_asr_backends.py`
- Modify: `asr_backends.py`

- [ ] **Step 1: 加入 factory、CUDA guard、hotwords 與推理測試**

測試使用 dependency injection/fake modules，驗證：

```python
def test_create_backend_defaults_to_whisper(monkeypatch):
    monkeypatch.delenv("ASR_BACKEND", raising=False)
    assert create_backend().name == "whisper"


def test_moss_backend_refuses_cpu():
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False),
    )
    with pytest.raises(RuntimeError, match="CUDA"):
        MossBackend(torch_module=fake_torch).load()


def test_build_moss_prompt_appends_hotwords(monkeypatch):
    monkeypatch.setenv("MOSS_HOTWORDS", "OpenMOSS,台灣")
    assert build_moss_prompt().endswith("熱詞提示：OpenMOSS, 台灣")
```

- [ ] **Step 2: 執行新增測試，確認 factory/class 尚不存在**

Run: `python -m pytest tests/test_asr_backends.py -v`

Expected: FAIL，指出 `create_backend`、`MossBackend` 或 `build_moss_prompt` 尚不存在。

- [ ] **Step 3: 實作兩個 adapter**

`WhisperBackend.load()` 延用 `WHISPER_MODEL=large-v3`、`WHISPER_DEVICE=cuda`、`WHISPER_COMPUTE_TYPE=float16`；`transcribe()` 延用 `beam_size=5` 與 `vad_filter=True`。

`MossBackend.load()` 必須：

```python
if not torch.cuda.is_available():
    raise RuntimeError("MOSS backend 需要 NVIDIA CUDA，不會自動退回 CPU。")
model_dir = snapshot_download(
    os.getenv("MOSS_MODEL", "openmoss/MOSS-Transcribe-Diarize"),
    cache_dir=str(MOSS_CACHE),
)
device = torch.device(os.getenv("MOSS_DEVICE", "cuda:0"))
dtype = {
    "bfloat16": torch.bfloat16,
    "float16": torch.float16,
}[os.getenv("MOSS_DTYPE", "bfloat16").lower()]
model = AutoModelForCausalLM.from_pretrained(
    model_dir,
    trust_remote_code=True,
    dtype="auto",
).to(dtype=dtype).to(device).eval()
processor = AutoProcessor.from_pretrained(model_dir, trust_remote_code=True)
```

`MossBackend.transcribe()` 使用官方 `build_transcription_messages`、`generate_transcription` 與 `parse_transcript`，設定 `do_sample=False` 及 `MOSS_MAX_NEW_TOKENS`（預設 `65536`），最後回傳 `(moss_segments_to_cues(...), "multilingual")`。

- [ ] **Step 4: 執行 backend 測試**

Run: `python -m pytest tests/test_asr_backends.py -v`

Expected: PASS，且不下載模型。

### Task 3: 把共用字幕流程接到 adapter

**Files:**
- Create: `tests/test_run_subtitle.py`
- Modify: `run_subtitle.py`

- [ ] **Step 1: 寫 regression tests**

用 fake backend、fake translator 與 fake embedder 測試：

```python
def test_process_video_uses_selected_backend(tmp_path, monkeypatch):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"video")
    backend = FakeBackend(
        cues=[{"id": 1, "time": "00:00:00,000 --> 00:00:01,000", "text": "[S01] Hi"}]
    )
    monkeypatch.setattr(run_subtitle, "translate_cues", lambda cues, *_: cues)
    monkeypatch.setattr(run_subtitle, "_embed_soft_subtitle", lambda *args: args[2])

    output = run_subtitle.process_video(video, backend, "key", "model", True)

    assert backend.videos == [video]
    assert output == video
    assert "[S01] Hi" in video.with_suffix(".srt").read_text(encoding="utf-8-sig")
```

另測試既有 SRT 時不呼叫 backend，以及 backend 回傳空 cues 時顯示通用 ASR 錯誤。

- [ ] **Step 2: 執行 regression tests，確認目前介面失敗**

Run: `python -m pytest tests/test_run_subtitle.py -v`

Expected: FAIL，因 `process_video` 仍直接呼叫 Whisper-specific function。

- [ ] **Step 3: 修改 `run_subtitle.py`**

- 移除 `_load_whisper_model`、`_transcribe_segments` 與本檔 `_srt_time`。
- import `create_backend`、`resolve_backend`。
- `process_video(..., backend, ...)` 呼叫 `backend.transcribe(video)`。
- 訊息改為 `backend.display_name`，不再硬編碼 Whisper。
- `main()` 在需要 ASR 時建立 backend；不需要 ASR 時維持 `None`，避免載入模型。

- [ ] **Step 4: 執行共用流程與 backend 測試**

Run: `python -m pytest tests/test_run_subtitle.py tests/test_asr_backends.py -v`

Expected: PASS。

### Task 4: Windows Batch backend interpreter 切換

**Files:**
- Create: `tests/test_run_subtitle_bat.py`
- Modify: `run_subtitle.bat`
- Create: `moss/.gitignore`

- [ ] **Step 1: 寫 Batch 文字契約與 smoke tests**

測試確認：

- 未設定 backend 時指向 `whisper\.venv\Scripts\python.exe`。
- `ASR_BACKEND=moss` 時指向 `moss\.venv\Scripts\python.exe`。
- 其他值回傳 exit code 2。
- MOSS venv 不存在時訊息包含 `install_moss.bat`。

- [ ] **Step 2: 執行測試並確認失敗**

Run: `python -m pytest tests/test_run_subtitle_bat.py -v`

Expected: FAIL，因 Batch 尚未包含 MOSS 分支。

- [ ] **Step 3: 實作 Batch 選擇**

```bat
if not defined ASR_BACKEND set "ASR_BACKEND=whisper"
if /I "%ASR_BACKEND%"=="whisper" (
    set "PYTHON=%ROOT%whisper\.venv\Scripts\python.exe"
) else if /I "%ASR_BACKEND%"=="moss" (
    set "PYTHON=%ROOT%moss\.venv\Scripts\python.exe"
) else (
    echo [錯誤] ASR_BACKEND 只允許 whisper 或 moss。
    exit /b 2
)
```

MOSS venv 缺少時顯示 `請先執行 install_moss.bat`；Whisper 維持原訊息。

- [ ] **Step 4: 加入 cache ignore**

`moss/.gitignore`：

```gitignore
.venv/
model-cache/
```

- [ ] **Step 5: 執行 Batch 測試**

Run: `python -m pytest tests/test_run_subtitle_bat.py -v`

Expected: PASS。

### Task 5: Windows CUDA 安裝與 ModelScope 下載

**Files:**
- Create: `tests/test_moss_setup.py`
- Create: `moss_setup.py`
- Create: `install_moss.bat`

- [ ] **Step 1: 寫 setup failing tests**

```python
def test_ensure_cuda_rejects_missing_cuda():
    torch_module = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    with pytest.raises(RuntimeError, match="CUDA"):
        ensure_cuda(torch_module)


def test_download_snapshot_uses_pinned_model_id(tmp_path):
    calls = []
    result = download_snapshot(
        lambda model_id, cache_dir: calls.append((model_id, cache_dir)) or str(tmp_path),
        cache_dir=tmp_path,
    )
    assert calls == [("openmoss/MOSS-Transcribe-Diarize", str(tmp_path))]
    assert result == tmp_path
```

- [ ] **Step 2: 執行 setup tests 並確認 module 不存在**

Run: `python -m pytest tests/test_moss_setup.py -v`

Expected: FAIL，包含 `ModuleNotFoundError: No module named 'moss_setup'`。

- [ ] **Step 3: 實作 `moss_setup.py`**

提供 `ensure_cuda(torch_module)`、`download_snapshot(snapshot_download_fn, cache_dir, model_id)` 與 `main()`。`main()` import `torch` 及 `modelscope.snapshot_download`，驗證 CUDA、下載 snapshot、用 `AutoProcessor.from_pretrained(local_path, trust_remote_code=True)` 做最小模型檔驗證，並以繁體中文輸出本機路徑。

- [ ] **Step 4: 實作固定版本安裝腳本**

`install_moss.bat` 必須依序執行：

```bat
where nvidia-smi
py -3.12 -c "import sys; assert sys.version_info[:2] == (3, 12)"
py -3.12 -m venv "%ROOT%moss\.venv"
"%PYTHON%" -m pip install --upgrade pip
"%PYTHON%" -m pip install --index-url https://download.pytorch.org/whl/cu128 torch torchaudio
"%PYTHON%" -m pip install "git+https://github.com/OpenMOSS/MOSS-Transcribe-Diarize.git@9990574e6ac62390a21bcce25a914d66ac92c25e" modelscope requests
"%PYTHON%" "%ROOT%moss_setup.py"
```

每一步檢查 `%ERRORLEVEL%`，失敗立即 `exit /b`，不得偷偷改用其他 Python、CUDA wheel 或 CPU。

- [ ] **Step 5: 執行 setup tests 與 Batch 靜態檢查**

Run: `python -m pytest tests/test_moss_setup.py tests/test_run_subtitle_bat.py -v`

Expected: PASS。

### Task 6: 文件與完整驗證

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 加入安裝與使用說明**

文件需列出：

```bat
install_moss.bat

set ASR_BACKEND=moss
run_subtitle.bat --force --limit 1
```

以及 PowerShell：

```powershell
$env:ASR_BACKEND = "moss"
.\run_subtitle.bat --force --limit 1
```

說明預設仍是 Whisper、MOSS 需要 Windows NVIDIA CUDA、權重來自 ModelScope、speaker label 會保留在 SRT、API key 從 OS 環境變數 `OPENROUTER_API_KEY` 取得。

- [ ] **Step 2: 執行所有單元測試**

Run: `python -m pytest -v`

Expected: 全部 PASS，0 failed。

- [ ] **Step 3: 執行語法與 dry-run**

Run:

```powershell
python -m py_compile asr_backends.py moss_setup.py run_subtitle.py
python run_subtitle.py --dry-run --limit 1
```

Expected: py_compile exit 0；dry-run 不載入 ASR 模型且正常列出待處理影片。

- [ ] **Step 4: 驗證差異範圍**

Run:

```powershell
git diff --check
git status --short
git diff -- asr_backends.py moss_setup.py install_moss.bat moss/.gitignore run_subtitle.py run_subtitle.bat tests README.md
```

Expected: 無 whitespace error；不包含既有 `capture_frames.py`、`run_capture.bat`、`run_download.py` 與 `scratch/` 的內容變更。

- [ ] **Step 5: 建立本機 commit**

只 stage 本計畫列出的 MOSS/字幕檔案，commit 名稱使用 15 個繁體中文內的通俗摘要：

```text
新增MOSS字幕切換
```

不得 push。

## 目標機真實 CUDA 驗證

目前開發機沒有 NVIDIA CUDA，以下步驟只能在目標 Windows CUDA 機器執行：

```bat
install_moss.bat
set ASR_BACKEND=moss
run_subtitle.bat --force --limit 1
```

成功條件：

- `torch.cuda.is_available()` 為 `True`。
- ModelScope snapshot 下載完成。
- MOSS 模型能載入 BF16 並完成一部影片推理。
- 產生的同名 SRT 含 `[S01]` 等 speaker label。
- OpenRouter 翻譯完成，FFmpeg 軟字幕封裝成功。

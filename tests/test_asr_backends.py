from types import SimpleNamespace

import pytest

from asr_backends import (
    MossBackend,
    WhisperBackend,
    build_moss_prompt,
    create_backend,
    moss_segments_to_cues,
    resolve_backend,
)


def test_resolve_backend_defaults_to_moss():
    assert resolve_backend({}) == "moss"


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
        {
            "id": 1,
            "time": "00:00:00,480 --> 00:00:01,660",
            "text": "[S01] Hello",
        },
        {
            "id": 2,
            "time": "00:00:12,260 --> 00:00:13,810",
            "text": "[S02] World",
        },
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


def test_create_backend_defaults_to_moss(monkeypatch):
    monkeypatch.delenv("ASR_BACKEND", raising=False)
    assert isinstance(create_backend(), MossBackend)


def test_create_backend_selects_whisper(monkeypatch):
    monkeypatch.setenv("ASR_BACKEND", "whisper")
    assert isinstance(create_backend(), WhisperBackend)


def test_moss_backend_refuses_cpu():
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False),
    )
    with pytest.raises(RuntimeError, match="CUDA"):
        MossBackend(torch_module=fake_torch).load()


def test_build_moss_prompt_appends_hotwords(monkeypatch):
    monkeypatch.setenv("MOSS_HOTWORDS", "OpenMOSS, 台灣")
    assert build_moss_prompt().endswith("熱詞提示：OpenMOSS, 台灣")


def test_build_moss_prompt_uses_default_without_hotwords(monkeypatch):
    monkeypatch.delenv("MOSS_HOTWORDS", raising=False)
    assert "說話人編號" in build_moss_prompt()
    assert "熱詞提示" not in build_moss_prompt()

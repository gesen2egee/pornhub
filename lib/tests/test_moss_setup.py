from pathlib import Path
from types import SimpleNamespace

import pytest

from moss_setup import download_snapshot, ensure_cuda


ROOT = Path(__file__).resolve().parents[2]


def test_ensure_cuda_rejects_missing_cuda():
    torch_module = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False),
    )
    with pytest.raises(RuntimeError, match="CUDA"):
        ensure_cuda(torch_module)


def test_ensure_cuda_returns_gpu_name():
    torch_module = SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: True,
            get_device_name=lambda index: "Test GPU",
        ),
    )
    assert ensure_cuda(torch_module) == "Test GPU"


def test_download_snapshot_uses_pinned_model_id(tmp_path):
    calls = []

    def fake_download(model_id, cache_dir):
        calls.append((model_id, cache_dir))
        return str(tmp_path)

    result = download_snapshot(fake_download, cache_dir=tmp_path)

    assert calls == [
        ("openmoss/MOSS-Transcribe-Diarize", str(tmp_path)),
    ]
    assert result == Path(tmp_path)


def test_download_snapshot_rejects_missing_result(tmp_path):
    missing = tmp_path / "missing"

    with pytest.raises(RuntimeError, match="snapshot"):
        download_snapshot(
            lambda model_id, cache_dir: str(missing),
            cache_dir=tmp_path,
        )


def test_installer_pins_cuda_wheel_and_moss_commit():
    content = (ROOT / "00_setup_or_update.bat").read_text(encoding="ascii")

    assert "https://download.pytorch.org/whl/cu128" in content
    assert "9990574e6ac62390a21bcce25a914d66ac92c25e" in content
    assert "ade1a82b4f8b97abf088280d22156448cc0a888f" in content
    assert "pyloudnorm" in content
    assert "xmlans/asmr-enhancer" in content
    assert "where git" in content
    assert 'py -3.12 -m venv "%MOSS_ROOT%\\.venv"' in content
    assert '"%MOSS_PYTHON%" "%LIB%\\moss_setup.py"' in content

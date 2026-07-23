from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_BATCH = ROOT / "run_download.bat"


def test_only_integrated_download_batch_remains():
    assert DOWNLOAD_BATCH.exists()
    assert not (ROOT / "run_subtitle.bat").exists()


def test_download_batch_checks_moss_environment():
    content = DOWNLOAD_BATCH.read_text(encoding="utf-8")
    assert 'set "MOSS_PYTHON=%ROOT%moss\\.venv\\Scripts\\python.exe"' in content
    assert "install_moss.bat" in content
    assert "下載與字幕使用獨立程序並行處理" in content

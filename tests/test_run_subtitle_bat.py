from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / "run_subtitle.bat"


def test_batch_uses_moss_interpreter():
    content = BATCH.read_text(encoding="utf-8")
    assert 'set "PYTHON=%ROOT%moss\\.venv\\Scripts\\python.exe"' in content


def test_batch_missing_moss_environment_mentions_installer():
    content = BATCH.read_text(encoding="utf-8")
    assert "install_moss.bat" in content

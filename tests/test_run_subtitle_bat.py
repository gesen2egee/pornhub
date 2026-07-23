import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / "run_subtitle.bat"


def run_batch(backend: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["ASR_BACKEND"] = backend
    return subprocess.run(
        ["cmd", "/d", "/c", str(BATCH), "--dry-run"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_batch_defaults_to_whisper_interpreter():
    content = BATCH.read_text(encoding="utf-8")
    assert 'if not defined ASR_BACKEND set "ASR_BACKEND=whisper"' in content
    assert 'set "PYTHON=%ROOT%whisper\\.venv\\Scripts\\python.exe"' in content


def test_batch_selects_moss_interpreter():
    content = BATCH.read_text(encoding="utf-8")
    assert 'if /I "%ASR_BACKEND%"=="moss"' in content
    assert 'set "PYTHON=%ROOT%moss\\.venv\\Scripts\\python.exe"' in content


def test_batch_rejects_unknown_backend():
    result = run_batch("other")
    assert result.returncode == 2
    assert "ASR_BACKEND" in result.stdout
    assert "whisper" in result.stdout
    assert "moss" in result.stdout


def test_batch_missing_moss_environment_mentions_installer():
    result = run_batch("moss")
    assert result.returncode == 2
    assert "install_moss.bat" in result.stdout

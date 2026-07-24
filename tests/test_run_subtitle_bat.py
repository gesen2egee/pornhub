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
    assert "Download and subtitle workers run in parallel" in content
    assert 'if /i "%~1"=="--check"' in content
    assert '"%ROOT%run_download.py" %*' in content
    assert (
        'if /i "%~1"=="--retry-subtitles" set "NO_PAUSE=1"'
        in content
    )
    assert (
        'if /i "%~1"=="--repair-over-1080" set "NO_PAUSE=1"'
        in content
    )


def test_download_batch_is_ascii_crlf_for_windows_cmd():
    content = DOWNLOAD_BATCH.read_bytes()
    assert all(byte < 128 for byte in content)
    assert b"\n" not in content.replace(b"\r\n", b"")

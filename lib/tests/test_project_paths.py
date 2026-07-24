from pathlib import Path

import project_paths
import run_download
import run_subtitle


def test_numbered_output_layout_is_centralized():
    root = project_paths.PROJECT_ROOT
    assert project_paths.TEMP_DIR == root / "output" / "00_temp"
    assert (
        project_paths.PREVIEW_IMAGES_DIR
        == root / "output" / "01_preview_images"
    )
    assert (
        project_paths.PREVIEW_VIDEOS_DIR
        == root / "output" / "02_preview_videos"
    )
    assert project_paths.VIDEOS_DIR == root / "output" / "03_videos"
    assert (
        project_paths.DOWNLOADED_DIR
        == root / "output" / "04_downloaded"
    )


def test_moss_uses_7_5_minute_chunks():
    assert run_subtitle.ASR_CHUNK_SECONDS == 450


def test_pipeline_temp_uses_target_directory_name(monkeypatch, tmp_path):
    monkeypatch.setattr(run_download, "WORK_TEMP_DIR", str(tmp_path))
    assert Path(run_download._pipeline_dir("G:/example/03_videos")) == (
        tmp_path / "pipeline" / "03_videos"
    )

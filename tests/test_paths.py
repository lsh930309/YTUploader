from __future__ import annotations

import sys
from pathlib import Path

from core import paths


def test_get_user_data_root_uses_localappdata(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.get_user_data_root() == tmp_path / paths.APP_NAME


def test_resource_path_prefers_meipass(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    try:
        assert paths.resource_path("bin", "ffmpeg.exe") == tmp_path / "bin" / "ffmpeg.exe"
    finally:
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)


def test_create_job_temp_dir_creates_unique_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    temp_dir = paths.create_job_temp_dir()
    assert temp_dir.exists()
    assert temp_dir.parent == tmp_path / paths.APP_NAME / "temp"


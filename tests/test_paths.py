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


def test_get_mpc_be_paths_use_isolated_runtime_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.get_mpc_be_runtime_dir() == tmp_path / paths.APP_NAME / "mpc-be" / "runtime"
    assert paths.get_mpc_be_runtime_executable_path() == tmp_path / paths.APP_NAME / "mpc-be" / "runtime" / "ytuploader-mpc-be.exe"
    assert paths.get_mpc_be_ini_path() == tmp_path / paths.APP_NAME / "mpc-be" / "runtime" / "ytuploader-mpc-be.ini"


def test_get_losslesscut_paths_use_isolated_runtime_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.get_losslesscut_runtime_dir() == tmp_path / paths.APP_NAME / "losslesscut" / "runtime"
    assert paths.get_losslesscut_config_dir() == tmp_path / paths.APP_NAME / "losslesscut" / "config"
    assert (
        paths.get_losslesscut_runtime_executable_path()
        == tmp_path / paths.APP_NAME / "losslesscut" / "runtime" / "LosslessCut.exe"
    )


def test_binary_path_prefers_private_tool_runtime(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    runtime_executable = paths.get_ffmpeg_runtime_binary_path("ffmpeg")
    runtime_executable.parent.mkdir(parents=True, exist_ok=True)
    runtime_executable.write_text("exe", encoding="utf-8")

    assert paths.binary_path("ffmpeg") == runtime_executable


def test_binary_path_prefers_losslesscut_runtime(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    runtime_executable = paths.get_losslesscut_runtime_executable_path()
    runtime_executable.parent.mkdir(parents=True, exist_ok=True)
    runtime_executable.write_text("exe", encoding="utf-8")

    assert paths.binary_path("losslesscut") == runtime_executable

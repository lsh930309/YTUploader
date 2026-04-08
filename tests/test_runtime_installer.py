from __future__ import annotations

from pathlib import Path

from core.mpc_be import MPCBEController
from core.runtime_installer import AppRuntimeInstaller


def test_install_ffmpeg_copies_ffmpeg_and_ffprobe(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    bundle_root = tmp_path / "bundle"
    bundle_bin = bundle_root / "bin"
    bundle_bin.mkdir(parents=True)
    (bundle_bin / "ffmpeg.exe").write_text("ffmpeg", encoding="utf-8")
    (bundle_bin / "ffprobe.exe").write_text("ffprobe", encoding="utf-8")

    monkeypatch.setattr("core.paths.get_bundle_root", lambda: bundle_root)
    installer = AppRuntimeInstaller()

    status = installer.install_package("ffmpeg")

    assert status.installed is True
    assert status.installed_paths[0].read_text(encoding="utf-8") == "ffmpeg"
    assert status.installed_paths[1].read_text(encoding="utf-8") == "ffprobe"


def test_mpc_be_status_reports_installable_when_source_exists(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    source_dir = tmp_path / "mpc-source"
    source_dir.mkdir()
    (source_dir / "mpc-be64.exe").write_text("exe", encoding="utf-8")

    controller = MPCBEController(
        runtime_dir=tmp_path / "runtime",
        executable_path=tmp_path / "runtime" / "ytuploader-mpc-be.exe",
        profile_path=tmp_path / "runtime" / "ytuploader-mpc-be.ini",
        runtime_sources=[source_dir],
    )
    installer = AppRuntimeInstaller(mpc_be_controller=controller)

    status = installer.get_status("mpc_be")

    assert status.installed is False
    assert status.status_text == "설치 가능"
    assert status.source_label == str(source_dir)

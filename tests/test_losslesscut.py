from __future__ import annotations

from pathlib import Path

from core.losslesscut import LosslessCutController, build_project_payload
from core.video_processor import ClipJob


def test_build_project_payload_uses_relative_media_name(tmp_path: Path) -> None:
    source_path = tmp_path / "recording.mkv"
    clip = ClipJob(
        clip_id="clip-1",
        clip_name="Opening",
        output_mp4=tmp_path / "Opening.mp4",
        start_time="00:00:03.500",
        end_time="00:00:12.000",
    )

    payload = build_project_payload(source_path, [clip], media_duration=30.0)

    assert payload["version"] == 2
    assert payload["mediaFileName"] == "recording.mkv"
    assert payload["cutSegments"] == [
        {
            "start": 3.5,
            "end": 12.0,
            "name": "Opening",
            "selected": True,
        }
    ]


def test_build_project_payload_fills_missing_end_with_media_duration(tmp_path: Path) -> None:
    source_path = tmp_path / "recording.mkv"
    clip = ClipJob(
        clip_id="clip-1",
        clip_name="FullRun",
        output_mp4=tmp_path / "FullRun.mp4",
        start_time="00:00:10",
        end_time=None,
    )

    payload = build_project_payload(source_path, [clip], media_duration=125.5)

    assert payload["cutSegments"] == [
        {
            "start": 10.0,
            "end": 125.5,
            "name": "FullRun",
            "selected": True,
        }
    ]


def test_losslesscut_install_from_source_dir_copies_runtime(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "LosslessCut.exe").write_text("exe", encoding="utf-8")
    (source_dir / "resources").mkdir()
    (source_dir / "resources" / "ffmpeg.exe").write_text("ffmpeg", encoding="utf-8")

    controller = LosslessCutController(
        runtime_dir=tmp_path / "runtime",
        executable_path=tmp_path / "runtime" / "LosslessCut.exe",
        config_dir=tmp_path / "config",
        runtime_sources=[source_dir],
    )

    installed_path = controller.install_from_source_dir(source_dir)

    assert installed_path == tmp_path / "runtime" / "LosslessCut.exe"
    assert installed_path.read_text(encoding="utf-8") == "exe"
    assert (tmp_path / "runtime" / "resources" / "ffmpeg.exe").read_text(encoding="utf-8") == "ffmpeg"

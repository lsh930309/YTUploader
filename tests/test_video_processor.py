from __future__ import annotations

from pathlib import Path

import pytest

from core.video_processor import (
    ClipJob,
    VideoJob,
    VideoValidationError,
    build_remux_command,
    build_sync_command,
    build_thumbnail_command,
    parse_timecode,
    validate_clip_job,
    validate_job,
)


def test_parse_timecode_accepts_hh_mm_ss() -> None:
    assert parse_timecode("01:02:03.5") == pytest.approx(3723.5)


def test_validate_job_rejects_invalid_time_range(tmp_path: Path) -> None:
    input_file = tmp_path / "input.mkv"
    input_file.write_text("fake", encoding="utf-8")
    job = VideoJob(input_mkv=input_file, output_mp4=tmp_path / "out.mp4", start_time="00:10", end_time="00:05")

    with pytest.raises(VideoValidationError):
        validate_job(job)


def test_build_sync_command_contains_expected_arguments(tmp_path: Path) -> None:
    job = VideoJob(input_mkv=tmp_path / "input.mkv", output_mp4=tmp_path / "out.mp4", delay_ms=250)
    command = build_sync_command(job, temp_mkv=tmp_path / "synced.mkv", mkvmerge_executable=Path("mkvmerge.exe"))

    assert command == [
        "mkvmerge.exe",
        "-o",
        str(tmp_path / "synced.mkv"),
        "--sync",
        "1:250",
        str(tmp_path / "input.mkv"),
    ]


def test_build_remux_command_contains_copy_pipeline(tmp_path: Path) -> None:
    job = VideoJob(
        input_mkv=tmp_path / "input.mkv",
        output_mp4=tmp_path / "out.mp4",
        start_time="00:00:05",
        end_time="00:00:10",
    )
    command = build_remux_command(job, temp_mkv=tmp_path / "synced.mkv", ffmpeg_executable=Path("ffmpeg.exe"))

    assert command == [
        "ffmpeg.exe",
        "-y",
        "-ss",
        "00:00:05",
        "-to",
        "00:00:10",
        "-i",
        str(tmp_path / "synced.mkv"),
        "-c",
        "copy",
        str(tmp_path / "out.mp4"),
    ]


def test_validate_clip_job_rejects_thumbnail_outside_range(tmp_path: Path) -> None:
    clip = ClipJob(
        clip_id="clip-1",
        clip_name="clip_01",
        output_mp4=tmp_path / "clip.mp4",
        start_time="00:00:10",
        end_time="00:00:20",
        thumbnail_time="00:00:05",
    )

    with pytest.raises(VideoValidationError):
        validate_clip_job(clip)


def test_build_thumbnail_command_contains_expected_arguments(tmp_path: Path) -> None:
    command = build_thumbnail_command(
        source_mkv=tmp_path / "input.mkv",
        thumbnail_time="00:01:23",
        output_path=tmp_path / "thumb.png",
        ffmpeg_executable=Path("ffmpeg.exe"),
    )

    assert command == [
        "ffmpeg.exe",
        "-y",
        "-ss",
        "00:01:23",
        "-i",
        str(tmp_path / "input.mkv"),
        "-frames:v",
        "1",
        str(tmp_path / "thumb.png"),
    ]

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from core.data_manager import DataManager, TemplateRenderError, build_upload_description, render_template


def test_load_creates_default_settings(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    manager = DataManager(settings_path=settings_path)

    settings = manager.load()

    assert settings_path.exists()
    assert settings["privacy_status"] == "private"
    assert settings["category_id"] == "22"


def test_render_template_supports_expected_tokens(tmp_path: Path) -> None:
    source = tmp_path / "clip.mkv"
    rendered = render_template("{date}-{filename}-{stem}", source=source, when=date(2026, 4, 7))
    assert rendered == "2026-04-07-clip.mkv-clip"


def test_render_template_rejects_unknown_tokens() -> None:
    with pytest.raises(TemplateRenderError):
        render_template("{channel}")


def test_build_upload_description_joins_sections() -> None:
    combined = build_upload_description("Main description", "00:00 Intro")
    assert combined == "Main description\n\n00:00 Intro"


def test_suggest_output_path_appends_suffix(tmp_path: Path) -> None:
    input_file = tmp_path / "recording.mkv"
    manager = DataManager(settings_path=tmp_path / "settings.json")

    assert manager.suggest_output_path(input_file) == tmp_path / "recording_edited.mp4"


def test_set_obs_source_dir_and_list_recent_recordings(tmp_path: Path) -> None:
    source_dir = tmp_path / "obs"
    source_dir.mkdir()
    newer = source_dir / "newer.mkv"
    older = source_dir / "older.mkv"
    older.write_text("older", encoding="utf-8")
    newer.write_text("newer", encoding="utf-8")

    manager = DataManager(settings_path=tmp_path / "settings.json")
    manager.set_obs_source_dir(source_dir)

    listed = manager.list_recent_obs_recordings(limit=5)
    assert newer in listed
    assert older in listed


def test_pick_recording_updates_recent_file_history(tmp_path: Path) -> None:
    manager = DataManager(settings_path=tmp_path / "settings.json")
    first = tmp_path / "one.mkv"
    second = tmp_path / "two.mkv"
    first.write_text("1", encoding="utf-8")
    second.write_text("2", encoding="utf-8")

    manager.pick_recording(first)
    settings = manager.pick_recording(second)

    assert settings["recent_source_files"][0] == str(second)
    assert settings["recent_source_files"][1] == str(first)

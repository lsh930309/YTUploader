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


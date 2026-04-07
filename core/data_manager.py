from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .paths import get_settings_path

SUPPORTED_TEMPLATE_TOKENS = {"date", "filename", "stem"}
TOKEN_PATTERN = re.compile(r"{([^{}]+)}")

DEFAULT_SETTINGS: dict[str, Any] = {
    "title_template": "[Recording] {date} {stem}",
    "description_template": "",
    "chapters_template": "",
    "tags": [],
    "playlist_id": "",
    "privacy_status": "private",
    "category_id": "22",
    "default_thumbnail_dir": "",
    "last_input_dir": "",
    "last_output_dir": "",
    "last_delay_ms": 0,
}


class DataManagerError(RuntimeError):
    pass


class TemplateRenderError(DataManagerError):
    pass


def merge_settings(raw_settings: dict[str, Any] | None) -> dict[str, Any]:
    settings = deepcopy(DEFAULT_SETTINGS)
    if not raw_settings:
        return settings

    for key, value in raw_settings.items():
        if key not in settings or value is None:
            continue
        if key == "tags":
            settings["tags"] = [str(item).strip() for item in value if str(item).strip()]
            continue
        settings[key] = value

    return settings


def template_context(source: str | Path | None = None, when: date | datetime | None = None) -> dict[str, str]:
    if when is None:
        when = date.today()
    elif isinstance(when, datetime):
        when = when.date()

    path = Path(source) if source else None
    return {
        "date": when.isoformat(),
        "filename": path.name if path else "",
        "stem": path.stem if path else "",
    }


def render_template(template: str, source: str | Path | None = None, when: date | datetime | None = None) -> str:
    unknown_tokens = set(TOKEN_PATTERN.findall(template)) - SUPPORTED_TEMPLATE_TOKENS
    if unknown_tokens:
        allowed = ", ".join(sorted(SUPPORTED_TEMPLATE_TOKENS))
        unknown = ", ".join(sorted(unknown_tokens))
        raise TemplateRenderError(f"Unsupported template token(s): {unknown}. Allowed tokens: {allowed}.")
    return template.format_map(template_context(source=source, when=when))


def build_upload_description(description: str, chapters: str) -> str:
    description = description.strip()
    chapters = chapters.strip()
    if description and chapters:
        return f"{description}\n\n{chapters}"
    return description or chapters


class DataManager:
    def __init__(self, settings_path: Path | None = None) -> None:
        self.settings_path = settings_path or get_settings_path()

    def load(self) -> dict[str, Any]:
        if not self.settings_path.exists():
            settings = deepcopy(DEFAULT_SETTINGS)
            self.save(settings)
            return settings

        try:
            raw_settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DataManagerError(f"Invalid settings JSON: {self.settings_path}") from exc

        return merge_settings(raw_settings)

    def save(self, settings: dict[str, Any]) -> Path:
        merged = merge_settings(settings)
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(merged, indent=2, ensure_ascii=True), encoding="utf-8")
        return self.settings_path

    def load_templates_for_source(self, source: str | Path | None = None) -> dict[str, Any]:
        settings = self.load()
        return {
            "title": render_template(settings["title_template"], source=source),
            "description": render_template(settings["description_template"], source=source),
            "chapters": render_template(settings["chapters_template"], source=source),
            "tags": list(settings["tags"]),
            "playlist_id": settings["playlist_id"],
            "privacy_status": settings["privacy_status"],
            "category_id": settings["category_id"],
            "default_thumbnail_dir": settings["default_thumbnail_dir"],
            "last_input_dir": settings["last_input_dir"],
            "last_output_dir": settings["last_output_dir"],
            "last_delay_ms": settings["last_delay_ms"],
        }

    def update_recent_paths(
        self,
        *,
        input_path: Path | None = None,
        output_path: Path | None = None,
        thumbnail_path: Path | None = None,
        delay_ms: int | None = None,
    ) -> dict[str, Any]:
        settings = self.load()
        if input_path is not None:
            settings["last_input_dir"] = str(input_path.parent)
        if output_path is not None:
            settings["last_output_dir"] = str(output_path.parent)
        if thumbnail_path is not None:
            settings["default_thumbnail_dir"] = str(thumbnail_path.parent)
        if delay_ms is not None:
            settings["last_delay_ms"] = int(delay_ms)
        self.save(settings)
        return settings

    def suggest_output_path(self, input_path: str | Path, output_dir: str | Path | None = None) -> Path:
        input_file = Path(input_path)
        base_dir = Path(output_dir) if output_dir else input_file.parent
        return base_dir / f"{input_file.stem}_edited.mp4"


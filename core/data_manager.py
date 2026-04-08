# -*- coding: utf-8 -*-
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
MAX_RECENT_SOURCE_FILES = 20

DEFAULT_SETTINGS: dict[str, Any] = {
    "title_prefix_template": "[녹화]",
    "description_template": "",
    "tags": [],
    "playlist_id": "",
    "privacy_status": "private",
    "category_id": "22",
    "default_thumbnail_dir": "",
    "last_input_dir": "",
    "last_output_dir": "",
    "last_delay_ms": 0,
    "obs_source_dir": "",
    "recent_source_files": [],
}


class DataManagerError(RuntimeError):
    pass


class TemplateRenderError(DataManagerError):
    pass


def _sanitize_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _sanitize_recent_files(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    unique_files: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique_files.append(text)
        if len(unique_files) >= MAX_RECENT_SOURCE_FILES:
            break
    return unique_files


def merge_settings(raw_settings: dict[str, Any] | None) -> dict[str, Any]:
    settings = deepcopy(DEFAULT_SETTINGS)
    if not raw_settings:
        return settings

    legacy_title = raw_settings.get("title_template")
    if legacy_title and not raw_settings.get("title_prefix_template"):
        settings["title_prefix_template"] = str(legacy_title).strip()

    for key, value in raw_settings.items():
        if value is None:
            continue
        if key == "tags":
            settings["tags"] = _sanitize_tags(value)
            continue
        if key == "recent_source_files":
            settings["recent_source_files"] = _sanitize_recent_files(value)
            continue
        if key in settings:
            settings[key] = value

    if str(settings.get("title_prefix_template", "")).strip() == "[Recording]":
        settings["title_prefix_template"] = "[녹화]"

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
        raise TemplateRenderError(f"지원하지 않는 템플릿 토큰입니다: {unknown}. 사용 가능한 토큰: {allowed}.")
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
            raise DataManagerError(f"설정 JSON이 올바르지 않습니다: {self.settings_path}") from exc

        return merge_settings(raw_settings)

    def save(self, settings: dict[str, Any]) -> Path:
        merged = merge_settings(settings)
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        return self.settings_path

    def load_templates_for_source(self, source: str | Path | None = None) -> dict[str, Any]:
        settings = self.load()
        return {
            "title_prefix": render_template(settings["title_prefix_template"], source=source),
            "description_template": render_template(settings["description_template"], source=source),
            "tags": list(settings["tags"]),
            "playlist_id": settings["playlist_id"],
            "privacy_status": settings["privacy_status"],
            "category_id": settings["category_id"],
            "default_thumbnail_dir": settings["default_thumbnail_dir"],
            "last_input_dir": settings["last_input_dir"],
            "last_output_dir": settings["last_output_dir"],
            "last_delay_ms": settings["last_delay_ms"],
            "obs_source_dir": settings["obs_source_dir"],
            "recent_source_files": list(settings["recent_source_files"]),
        }

    def set_obs_source_dir(self, path: str | Path) -> dict[str, Any]:
        obs_source_dir = str(Path(path))
        settings = self.load()
        settings["obs_source_dir"] = obs_source_dir
        settings["last_input_dir"] = obs_source_dir
        self.save(settings)
        return settings

    def pick_recording(self, path: str | Path) -> dict[str, Any]:
        selected_path = Path(path)
        settings = self.load()
        settings["last_input_dir"] = str(selected_path.parent)
        settings["obs_source_dir"] = settings["obs_source_dir"] or str(selected_path.parent)

        recent_files = [str(selected_path)]
        recent_files.extend(
            item for item in settings["recent_source_files"] if item != str(selected_path)
        )
        settings["recent_source_files"] = recent_files[:MAX_RECENT_SOURCE_FILES]
        self.save(settings)
        return settings

    def list_recent_obs_recordings(self, limit: int = 20) -> list[Path]:
        settings = self.load()
        obs_source_dir = settings["obs_source_dir"].strip()
        if obs_source_dir:
            source_dir = Path(obs_source_dir)
            if source_dir.exists():
                files = sorted(
                    source_dir.glob("*.mkv"),
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
                if files:
                    return files[:limit]

        existing_recent_files = [
            Path(item) for item in settings["recent_source_files"] if Path(item).exists()
        ]
        return existing_recent_files[:limit]

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
            settings = self.pick_recording(input_path)
        else:
            settings = self.load()

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

    def suggest_clip_output_path(
        self,
        input_path: str | Path,
        clip_name: str,
        output_dir: str | Path | None = None,
    ) -> Path:
        input_file = Path(input_path)
        base_dir = Path(output_dir) if output_dir else input_file.parent
        safe_clip_name = clip_name.strip().replace(" ", "_") or "clip"
        return base_dir / f"{input_file.stem}_{safe_clip_name}.mp4"

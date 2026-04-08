from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ChapterMarker:
    timecode: str
    title: str


@dataclass(slots=True)
class ClipDraft:
    clip_id: str
    clip_name: str
    start_time: str | None = None
    end_time: str | None = None
    thumbnail_time: str | None = None
    custom_title: str = ""
    custom_notes: str = ""
    upload_enabled: bool = False
    chapters: list[ChapterMarker] = field(default_factory=list)


@dataclass(slots=True)
class JobDraft:
    job_id: str
    source_path: Path
    obs_source_dir: Path | None = None
    delay_ms: int = 0
    game: str = ""
    preset: str = ""
    characters: str = ""
    build_info: str = ""
    tags: list[str] = field(default_factory=list)
    title_prefix: str = ""
    description_template: str = ""
    playlist_id: str = ""
    privacy_status: str = "private"
    category_id: str = "22"
    clips: list[ClipDraft] = field(default_factory=list)


@dataclass(slots=True)
class ClipExport:
    clip_id: str
    clip_name: str
    video_path: Path
    thumbnail_path: Path | None
    metadata_sidecar_path: Path
    clipboard_payload: str
    upload_enabled: bool = False
    youtube_upload_payload: dict | None = None


@dataclass(slots=True)
class ExportBundle:
    job_id: str
    source_path: Path
    clip_exports: list[ClipExport]

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

JOB_STAGE_SELECT = "SELECT_SOURCE"
JOB_STAGE_SYNC = "SYNC_AUDIO"
JOB_STAGE_SEGMENTS = "SPLIT_SEGMENTS"
JOB_STAGE_METADATA = "SEGMENT_METADATA"
JOB_STAGE_UPLOAD = "UPLOAD_AND_CLEANUP"
JOB_STAGE_DONE = "DONE"


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
class SegmentState:
    clip_id: str
    clip_name: str
    start_time: str | None = None
    end_time: str | None = None
    output_path: Path | None = None
    sidecar_path: Path | None = None
    thumbnail_path: Path | None = None
    thumbnail_time: str | None = None
    upload_enabled: bool = False
    metadata_ready: bool = False
    custom_title: str = ""
    custom_notes: str = ""
    description_text: str = ""
    category_id: str = "22"
    chapters: list[ChapterMarker] = field(default_factory=list)
    upload_status: str = "pending"
    upload_video_id: str = ""
    upload_url: str = ""
    upload_error: str = ""


@dataclass(slots=True)
class JobState:
    job_id: str
    source_path: Path
    output_dir: Path
    obs_source_dir: Path | None = None
    current_stage: str = JOB_STAGE_SELECT
    delay_ms: int = 0
    synced_source_path: Path | None = None
    title_prefix: str = ""
    description_template: str = ""
    tags: list[str] = field(default_factory=list)
    playlist_id: str = ""
    privacy_status: str = "private"
    category_id: str = "22"
    game: str = ""
    preset: str = ""
    characters: str = ""
    build_info: str = ""
    segment_drafts: list[ClipDraft] = field(default_factory=list)
    segments: list[SegmentState] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    source_deleted: bool = False
    cleanup_status: str = "pending"
    last_error: str = ""


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

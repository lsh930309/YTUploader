from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .metadata_exporter import read_sidecar
from .models import ChapterMarker, ClipDraft, JobState, SegmentState
from .paths import get_job_artifacts_dir, get_job_dir, get_job_state_path, get_jobs_dir


class JobStoreError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_path(value: Path | None) -> str:
    return str(value) if value is not None else ""


def _deserialize_path(value: Any) -> Path | None:
    text = str(value).strip() if value is not None else ""
    return Path(text) if text else None


def _serialize_chapters(chapters: list[ChapterMarker]) -> list[dict[str, str]]:
    return [{"timecode": chapter.timecode, "title": chapter.title} for chapter in chapters]


def _deserialize_chapters(raw_value: Any) -> list[ChapterMarker]:
    if not isinstance(raw_value, list):
        return []
    chapters: list[ChapterMarker] = []
    for item in raw_value:
        if not isinstance(item, dict):
            continue
        chapters.append(
            ChapterMarker(
                timecode=str(item.get("timecode", "")).strip(),
                title=str(item.get("title", "")).strip(),
            )
        )
    return chapters


def _clip_draft_to_dict(clip: ClipDraft) -> dict[str, Any]:
    return {
        "clip_id": clip.clip_id,
        "clip_name": clip.clip_name,
        "start_time": clip.start_time,
        "end_time": clip.end_time,
        "thumbnail_time": clip.thumbnail_time,
        "custom_title": clip.custom_title,
        "custom_notes": clip.custom_notes,
        "upload_enabled": clip.upload_enabled,
        "chapters": _serialize_chapters(clip.chapters),
    }


def _clip_draft_from_dict(raw_value: Any) -> ClipDraft:
    data = raw_value if isinstance(raw_value, dict) else {}
    return ClipDraft(
        clip_id=str(data.get("clip_id", uuid4().hex)),
        clip_name=str(data.get("clip_name", "")).strip(),
        start_time=data.get("start_time"),
        end_time=data.get("end_time"),
        thumbnail_time=data.get("thumbnail_time"),
        custom_title=str(data.get("custom_title", "")).strip(),
        custom_notes=str(data.get("custom_notes", "")).strip(),
        upload_enabled=bool(data.get("upload_enabled", False)),
        chapters=_deserialize_chapters(data.get("chapters")),
    )


def _segment_to_dict(segment: SegmentState) -> dict[str, Any]:
    return {
        "clip_id": segment.clip_id,
        "clip_name": segment.clip_name,
        "start_time": segment.start_time,
        "end_time": segment.end_time,
        "output_path": _serialize_path(segment.output_path),
        "sidecar_path": _serialize_path(segment.sidecar_path),
        "thumbnail_path": _serialize_path(segment.thumbnail_path),
        "thumbnail_time": segment.thumbnail_time,
        "upload_enabled": segment.upload_enabled,
        "metadata_ready": segment.metadata_ready,
        "custom_title": segment.custom_title,
        "custom_notes": segment.custom_notes,
        "description_text": segment.description_text,
        "category_id": segment.category_id,
        "chapters": _serialize_chapters(segment.chapters),
        "upload_status": segment.upload_status,
        "upload_video_id": segment.upload_video_id,
        "upload_url": segment.upload_url,
        "upload_error": segment.upload_error,
    }


def _segment_from_dict(raw_value: Any) -> SegmentState:
    data = raw_value if isinstance(raw_value, dict) else {}
    segment = SegmentState(
        clip_id=str(data.get("clip_id", uuid4().hex)),
        clip_name=str(data.get("clip_name", "")).strip(),
        start_time=data.get("start_time"),
        end_time=data.get("end_time"),
        output_path=_deserialize_path(data.get("output_path")),
        sidecar_path=_deserialize_path(data.get("sidecar_path")),
        thumbnail_path=_deserialize_path(data.get("thumbnail_path")),
        thumbnail_time=data.get("thumbnail_time"),
        upload_enabled=bool(data.get("upload_enabled", False)),
        metadata_ready=bool(data.get("metadata_ready", False)),
        custom_title=str(data.get("custom_title", "")).strip(),
        custom_notes=str(data.get("custom_notes", "")).strip(),
        description_text=str(data.get("description_text", "")).strip(),
        category_id=str(data.get("category_id", "22")).strip() or "22",
        chapters=_deserialize_chapters(data.get("chapters")),
        upload_status=str(data.get("upload_status", "pending")).strip() or "pending",
        upload_video_id=str(data.get("upload_video_id", "")).strip(),
        upload_url=str(data.get("upload_url", "")).strip(),
        upload_error=str(data.get("upload_error", "")).strip(),
    )
    return segment


def _load_segment_sidecar(segment: SegmentState, default_category_id: str) -> SegmentState:
    if segment.sidecar_path is None or not segment.sidecar_path.exists():
        return segment

    payload = read_sidecar(segment.sidecar_path)
    clip_payload = payload.get("clip", {})
    metadata_payload = payload.get("metadata", {})
    outputs_payload = payload.get("outputs", {})
    upload_payload = payload.get("upload", {})

    if isinstance(clip_payload, dict):
        segment.clip_name = str(clip_payload.get("clip_name", segment.clip_name)).strip() or segment.clip_name
        segment.start_time = clip_payload.get("start_time", segment.start_time)
        segment.end_time = clip_payload.get("end_time", segment.end_time)
        segment.thumbnail_time = clip_payload.get("thumbnail_time", segment.thumbnail_time)
        segment.custom_title = str(clip_payload.get("custom_title", segment.custom_title)).strip()
        segment.custom_notes = str(clip_payload.get("custom_notes", segment.custom_notes)).strip()
        segment.upload_enabled = bool(clip_payload.get("upload_enabled", segment.upload_enabled))
        segment.chapters = _deserialize_chapters(clip_payload.get("chapters"))

    if isinstance(metadata_payload, dict):
        segment.description_text = str(metadata_payload.get("description", segment.description_text)).strip()
        segment.category_id = str(metadata_payload.get("category_id", default_category_id)).strip() or default_category_id

    if isinstance(outputs_payload, dict):
        segment.output_path = _deserialize_path(outputs_payload.get("video_path")) or segment.output_path
        segment.thumbnail_path = _deserialize_path(outputs_payload.get("thumbnail_path")) or segment.thumbnail_path

    if isinstance(upload_payload, dict):
        segment.upload_status = str(upload_payload.get("status", segment.upload_status)).strip() or segment.upload_status
        segment.upload_video_id = str(upload_payload.get("video_id", segment.upload_video_id)).strip()
        segment.upload_url = str(upload_payload.get("url", segment.upload_url)).strip()
        segment.upload_error = str(upload_payload.get("error_message", segment.upload_error)).strip()

    segment.metadata_ready = segment.sidecar_path.exists()
    return segment


class JobStore:
    def __init__(self, jobs_dir: Path | None = None) -> None:
        self.jobs_dir = jobs_dir or get_jobs_dir()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def create_job(
        self,
        *,
        source_path: Path,
        output_dir: Path,
        obs_source_dir: Path | None = None,
        title_prefix: str = "",
        description_template: str = "",
        tags: list[str] | None = None,
        playlist_id: str = "",
        privacy_status: str = "private",
        category_id: str = "22",
        game: str = "",
        preset: str = "",
        characters: str = "",
        build_info: str = "",
        segment_drafts: list[ClipDraft] | None = None,
    ) -> JobState:
        job_id = uuid4().hex
        timestamp = _utc_now()
        job = JobState(
            job_id=job_id,
            source_path=source_path,
            output_dir=output_dir,
            obs_source_dir=obs_source_dir,
            title_prefix=title_prefix,
            description_template=description_template,
            tags=list(tags or []),
            playlist_id=playlist_id,
            privacy_status=privacy_status,
            category_id=category_id,
            game=game,
            preset=preset,
            characters=characters,
            build_info=build_info,
            segment_drafts=list(segment_drafts or []),
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.save_job(job)
        return job

    def save_job(self, job: JobState) -> Path:
        if not job.created_at:
            job.created_at = _utc_now()
        job.updated_at = _utc_now()
        state_path = get_job_state_path(job.job_id)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        get_job_artifacts_dir(job.job_id).mkdir(parents=True, exist_ok=True)
        payload = {
            "job_id": job.job_id,
            "source_path": str(job.source_path),
            "output_dir": str(job.output_dir),
            "obs_source_dir": _serialize_path(job.obs_source_dir),
            "current_stage": job.current_stage,
            "delay_ms": job.delay_ms,
            "synced_source_path": _serialize_path(job.synced_source_path),
            "title_prefix": job.title_prefix,
            "description_template": job.description_template,
            "tags": list(job.tags),
            "playlist_id": job.playlist_id,
            "privacy_status": job.privacy_status,
            "category_id": job.category_id,
            "game": job.game,
            "preset": job.preset,
            "characters": job.characters,
            "build_info": job.build_info,
            "segment_drafts": [_clip_draft_to_dict(clip) for clip in job.segment_drafts],
            "segments": [_segment_to_dict(segment) for segment in job.segments],
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "source_deleted": job.source_deleted,
            "cleanup_status": job.cleanup_status,
            "last_error": job.last_error,
        }
        state_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return state_path

    def load_job(self, job_id: str) -> JobState:
        state_path = get_job_state_path(job_id)
        if not state_path.exists():
            raise JobStoreError(f"job 상태 파일이 존재하지 않습니다: {state_path}")
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise JobStoreError(f"job 상태 JSON이 올바르지 않습니다: {state_path}") from exc
        return self._job_from_payload(payload)

    def list_jobs(self) -> list[JobState]:
        jobs: list[JobState] = []
        for state_path in sorted(self.jobs_dir.glob("*/job.json")):
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                jobs.append(self._job_from_payload(payload))
            except (OSError, json.JSONDecodeError, JobStoreError):
                continue
        jobs.sort(key=lambda item: item.updated_at, reverse=True)
        return jobs

    def load_latest_job(self) -> JobState | None:
        jobs = self.list_jobs()
        return jobs[0] if jobs else None

    def delete_job(self, job_id: str) -> None:
        job_dir = get_job_dir(job_id)
        if not job_dir.exists():
            return
        shutil.rmtree(job_dir, ignore_errors=True)

    def _job_from_payload(self, payload: dict[str, Any]) -> JobState:
        if not isinstance(payload, dict):
            raise JobStoreError("job 상태 JSON 형식이 올바르지 않습니다.")
        job = JobState(
            job_id=str(payload.get("job_id", "")).strip(),
            source_path=Path(str(payload.get("source_path", "")).strip()),
            output_dir=Path(str(payload.get("output_dir", "")).strip()),
            obs_source_dir=_deserialize_path(payload.get("obs_source_dir")),
            current_stage=str(payload.get("current_stage", "SELECT_SOURCE")).strip() or "SELECT_SOURCE",
            delay_ms=int(payload.get("delay_ms", 0)),
            synced_source_path=_deserialize_path(payload.get("synced_source_path")),
            title_prefix=str(payload.get("title_prefix", "")).strip(),
            description_template=str(payload.get("description_template", "")).strip(),
            tags=[str(tag).strip() for tag in payload.get("tags", []) if str(tag).strip()],
            playlist_id=str(payload.get("playlist_id", "")).strip(),
            privacy_status=str(payload.get("privacy_status", "private")).strip() or "private",
            category_id=str(payload.get("category_id", "22")).strip() or "22",
            game=str(payload.get("game", "")).strip(),
            preset=str(payload.get("preset", "")).strip(),
            characters=str(payload.get("characters", "")).strip(),
            build_info=str(payload.get("build_info", "")).strip(),
            segment_drafts=[_clip_draft_from_dict(item) for item in payload.get("segment_drafts", [])],
            segments=[_segment_from_dict(item) for item in payload.get("segments", [])],
            created_at=str(payload.get("created_at", "")).strip(),
            updated_at=str(payload.get("updated_at", "")).strip(),
            source_deleted=bool(payload.get("source_deleted", False)),
            cleanup_status=str(payload.get("cleanup_status", "pending")).strip() or "pending",
            last_error=str(payload.get("last_error", "")).strip(),
        )
        if not job.job_id:
            raise JobStoreError("job_id가 없는 상태 파일입니다.")
        for index, segment in enumerate(job.segments):
            job.segments[index] = _load_segment_sidecar(segment, job.category_id)
        return job

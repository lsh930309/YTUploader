from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import ChapterMarker, ClipDraft, ClipExport, ExportBundle, JobDraft


def format_chapters(chapters: list[ChapterMarker]) -> str:
    lines = [f"{chapter.timecode} {chapter.title}".strip() for chapter in chapters if chapter.timecode.strip()]
    return "\n".join(lines)


def build_clip_title(title_prefix: str, custom_title: str) -> str:
    prefix = title_prefix.strip()
    title = custom_title.strip()
    if prefix and title:
        return f"{prefix} {title}"
    return prefix or title


def build_clip_description(job: JobDraft, clip: ClipDraft) -> str:
    sections: list[str] = []

    template = job.description_template.strip()
    if template:
        sections.append(template)

    details: list[str] = []
    if job.game.strip():
        details.append(f"Game: {job.game.strip()}")
    if job.preset.strip():
        details.append(f"Preset: {job.preset.strip()}")
    if job.characters.strip():
        details.append(f"Characters: {job.characters.strip()}")
    if job.build_info.strip():
        details.append(f"Build: {job.build_info.strip()}")
    if clip.custom_notes.strip():
        details.append(f"Notes: {clip.custom_notes.strip()}")

    if details:
        sections.append("\n".join(details))

    chapter_text = format_chapters(clip.chapters)
    if chapter_text:
        sections.append(chapter_text)

    return "\n\n".join(section for section in sections if section.strip())


def build_clip_sidecar_payload(job: JobDraft, clip: ClipDraft, output_path: Path, thumbnail_path: Path | None) -> dict[str, Any]:
    title = build_clip_title(job.title_prefix, clip.custom_title)
    description = build_clip_description(job, clip)
    return {
        "job_id": job.job_id,
        "source_path": str(job.source_path),
        "obs_source_dir": str(job.obs_source_dir) if job.obs_source_dir else "",
        "clip": {
            "clip_id": clip.clip_id,
            "clip_name": clip.clip_name,
            "start_time": clip.start_time,
            "end_time": clip.end_time,
            "thumbnail_time": clip.thumbnail_time,
            "upload_enabled": clip.upload_enabled,
            "chapters": [
                {"timecode": chapter.timecode, "title": chapter.title}
                for chapter in clip.chapters
            ],
        },
        "metadata": {
            "title": title,
            "description": description,
            "tags": list(job.tags),
            "playlist_id": job.playlist_id,
            "privacy_status": job.privacy_status,
            "category_id": job.category_id,
            "game": job.game,
            "preset": job.preset,
            "characters": job.characters,
            "build_info": job.build_info,
        },
        "outputs": {
            "video_path": str(output_path),
            "thumbnail_path": str(thumbnail_path) if thumbnail_path else "",
        },
    }


def write_sidecar(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return path


def build_clipboard_payload(payload: dict[str, Any]) -> str:
    metadata = payload["metadata"]
    parts = [metadata["title"].strip(), metadata["description"].strip()]
    return "\n\n".join(part for part in parts if part)


def build_export_bundle(job: JobDraft, clip_exports: list[ClipExport]) -> ExportBundle:
    return ExportBundle(job_id=job.job_id, source_path=job.source_path, clip_exports=clip_exports)

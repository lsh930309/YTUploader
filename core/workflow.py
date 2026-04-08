from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .metadata_exporter import (
    build_clip_sidecar_payload,
    build_clipboard_payload,
    build_export_bundle,
    write_sidecar,
)
from .models import ClipDraft, ClipExport, ExportBundle, JobDraft
from .video_processor import ClipJob, VideoProcessor
from .youtube_uploader import UploadJob, YouTubeUploader

StageCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[Optional[int]], None]]
LogCallback = Optional[Callable[[str], None]]


def _build_clip_jobs(job: JobDraft, output_dir: Path) -> list[ClipJob]:
    return VideoProcessor.clip_jobs_from_drafts(job.source_path, output_dir, job.clips)


def build_sidecar_payload(job: JobDraft, clip: ClipDraft, output_path: Path, thumbnail_path: Path | None) -> dict:
    return build_clip_sidecar_payload(job, clip, output_path, thumbnail_path)


class WorkflowRunner:
    def __init__(
        self,
        *,
        video_processor: VideoProcessor | None = None,
        uploader: YouTubeUploader | None = None,
    ) -> None:
        self.video_processor = video_processor or VideoProcessor()
        self.uploader = uploader or YouTubeUploader()

    def cancel(self) -> None:
        self.video_processor.cancel()
        self.uploader.cancel()

    def process_job(
        self,
        job: JobDraft,
        *,
        output_dir: Path,
        stage_callback: StageCallback = None,
        log_callback: LogCallback = None,
    ) -> ExportBundle:
        clip_jobs = _build_clip_jobs(job, output_dir)
        rendered_paths = self.video_processor.process_clips(
            input_mkv=job.source_path,
            delay_ms=job.delay_ms,
            clips=clip_jobs,
            stage_callback=stage_callback,
            log_callback=log_callback,
        )

        clip_exports: list[ClipExport] = []
        for clip_draft, clip_job, rendered_path in zip(job.clips, clip_jobs, rendered_paths, strict=True):
            thumbnail_path = None
            if clip_draft.thumbnail_time:
                thumbnail_path = rendered_path.with_name(f"{rendered_path.stem}_thumbnail.png")
                self.video_processor.capture_thumbnail(
                    source_mkv=job.source_path,
                    thumbnail_time=clip_draft.thumbnail_time,
                    output_path=thumbnail_path,
                    stage_callback=stage_callback,
                    log_callback=log_callback,
                )

            sidecar_path = rendered_path.with_suffix(".json")
            payload = build_sidecar_payload(job, clip_draft, rendered_path, thumbnail_path)
            write_sidecar(sidecar_path, payload)
            clip_exports.append(
                ClipExport(
                    clip_id=clip_draft.clip_id,
                    clip_name=clip_job.clip_name,
                    video_path=rendered_path,
                    thumbnail_path=thumbnail_path,
                    metadata_sidecar_path=sidecar_path,
                    clipboard_payload=build_clipboard_payload(payload),
                    upload_enabled=clip_draft.upload_enabled,
                    youtube_upload_payload=payload["metadata"],
                )
            )

        return build_export_bundle(job, clip_exports)

    def upload_selected_clips(
        self,
        bundle: ExportBundle,
        *,
        stage_callback: StageCallback = None,
        progress_callback: ProgressCallback = None,
        log_callback: LogCallback = None,
    ) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        selected_exports = [
            export for export in bundle.clip_exports if export.upload_enabled and export.youtube_upload_payload
        ]
        clip_count = max(len(selected_exports), 1)

        for index, clip_export in enumerate(selected_exports, start=1):
            payload = clip_export.youtube_upload_payload or {}
            upload_job = UploadJob(
                video_path=clip_export.video_path,
                title=str(payload.get("title", "")),
                description=str(payload.get("description", "")),
                tags=list(payload.get("tags", [])),
                playlist_id=str(payload.get("playlist_id", "")),
                privacy_status=str(payload.get("privacy_status", "private")),
                thumbnail_path=clip_export.thumbnail_path,
                category_id=str(payload.get("category_id", "22")),
                made_for_kids=False,
            )
            result = self.uploader.upload_video(
                upload_job,
                interactive=True,
                stage_callback=stage_callback,
                progress_callback=lambda value, i=index: progress_callback(
                    None if value is None else int(((i - 1) / clip_count) * 100 + (value / clip_count))
                )
                if progress_callback
                else None,
                log_callback=log_callback,
            )
            results.append(result)
        return results

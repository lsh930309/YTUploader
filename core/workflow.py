from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable, Optional

from .metadata_exporter import (
    build_segment_sidecar_payload,
    build_clip_sidecar_payload,
    build_clipboard_payload,
    build_export_bundle,
    read_sidecar,
    write_sidecar,
)
from .models import ClipDraft, ClipExport, ExportBundle, JobDraft, JobState, SegmentState
from .video_processor import ClipJob, VideoProcessor
from .youtube_uploader import UploadJob, YouTubeUploader
from .youtube_uploader import YouTubeUploadCancelled

StageCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[Optional[int]], None]]
LogCallback = Optional[Callable[[str], None]]

FILENAME_SANITIZE_PATTERN = re.compile(r'[<>:"/\\\\|?*]+')


def _build_clip_jobs(job: JobDraft, output_dir: Path) -> list[ClipJob]:
    return VideoProcessor.clip_jobs_from_drafts(job.source_path, output_dir, job.clips)


def build_sidecar_payload(job: JobDraft, clip: ClipDraft, output_path: Path, thumbnail_path: Path | None) -> dict:
    return build_clip_sidecar_payload(job, clip, output_path, thumbnail_path)


def sanitize_filename(value: str, fallback: str = "segment") -> str:
    cleaned = FILENAME_SANITIZE_PATTERN.sub("_", value).strip().strip(".")
    return cleaned or fallback


def _segment_to_clip_export(segment: SegmentState) -> ClipExport | None:
    if segment.output_path is None or segment.sidecar_path is None:
        return None
    payload = read_sidecar(segment.sidecar_path) if segment.sidecar_path.exists() else {}
    clipboard_payload = build_clipboard_payload(payload) if payload else ""
    return ClipExport(
        clip_id=segment.clip_id,
        clip_name=segment.clip_name,
        video_path=segment.output_path,
        thumbnail_path=segment.thumbnail_path,
        metadata_sidecar_path=segment.sidecar_path,
        clipboard_payload=clipboard_payload,
        upload_enabled=segment.upload_enabled,
        youtube_upload_payload=payload.get("metadata") if isinstance(payload, dict) else None,
    )


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

    def apply_audio_sync(
        self,
        job: JobState,
        *,
        synced_path: Path,
        stage_callback: StageCallback = None,
        log_callback: LogCallback = None,
    ) -> JobState:
        job.synced_source_path = self.video_processor.apply_audio_sync(
            input_mkv=job.source_path,
            delay_ms=job.delay_ms,
            output_path=synced_path,
            stage_callback=stage_callback,
            log_callback=log_callback,
        )
        return job

    def split_segments(
        self,
        job: JobState,
        *,
        clips: list[ClipDraft],
        stage_callback: StageCallback = None,
        log_callback: LogCallback = None,
    ) -> list[SegmentState]:
        source_path = job.synced_source_path or job.source_path
        clip_jobs = _build_clip_jobs(
            JobDraft(
                job_id=job.job_id,
                source_path=source_path,
                obs_source_dir=job.obs_source_dir,
                delay_ms=job.delay_ms,
                game=job.game,
                preset=job.preset,
                characters=job.characters,
                build_info=job.build_info,
                tags=list(job.tags),
                title_prefix=job.title_prefix,
                description_template=job.description_template,
                playlist_id=job.playlist_id,
                privacy_status=job.privacy_status,
                category_id=job.category_id,
                clips=clips,
            ),
            job.output_dir,
        )
        rendered_paths = self.video_processor.split_clips(
            source_mkv=source_path,
            clips=clip_jobs,
            stage_callback=stage_callback,
            log_callback=log_callback,
        )
        segments: list[SegmentState] = []
        for clip_draft, clip_job, rendered_path in zip(clips, clip_jobs, rendered_paths, strict=True):
            segments.append(
                SegmentState(
                    clip_id=clip_draft.clip_id,
                    clip_name=clip_job.clip_name,
                    start_time=clip_draft.start_time,
                    end_time=clip_draft.end_time,
                    output_path=rendered_path,
                    sidecar_path=rendered_path.with_suffix(".json"),
                    thumbnail_time=clip_draft.thumbnail_time,
                    upload_enabled=clip_draft.upload_enabled,
                    custom_title=clip_draft.custom_title,
                    custom_notes=clip_draft.custom_notes,
                    category_id=job.category_id,
                    chapters=list(clip_draft.chapters),
                )
            )
        return segments

    def save_segment_metadata(
        self,
        job: JobState,
        segment: SegmentState,
        *,
        stage_callback: StageCallback = None,
        log_callback: LogCallback = None,
    ) -> SegmentState:
        if segment.output_path is None:
            raise RuntimeError("세그먼트 산출물 경로가 없어 메타데이터를 저장할 수 없습니다.")
        if segment.thumbnail_time and segment.thumbnail_time.strip():
            thumbnail_path = segment.output_path.with_name(f"{segment.output_path.stem}_thumbnail.png")
            segment.thumbnail_path = self.video_processor.capture_thumbnail(
                source_mkv=segment.output_path,
                thumbnail_time=segment.thumbnail_time,
                output_path=thumbnail_path,
                stage_callback=stage_callback,
                log_callback=log_callback,
            )
        elif segment.thumbnail_path and segment.thumbnail_path.exists():
            segment.thumbnail_path.unlink(missing_ok=True)
            segment.thumbnail_path = None

        segment.sidecar_path = segment.sidecar_path or segment.output_path.with_suffix(".json")
        payload = build_segment_sidecar_payload(job, segment)
        write_sidecar(segment.sidecar_path, payload)
        segment.metadata_ready = True
        return segment

    def build_export_bundle_from_job(self, job: JobState) -> ExportBundle:
        clip_exports = [
            clip_export
            for segment in job.segments
            for clip_export in [_segment_to_clip_export(segment)]
            if clip_export is not None
        ]
        return ExportBundle(job_id=job.job_id, source_path=job.source_path, clip_exports=clip_exports)

    def upload_ready_segments(
        self,
        job: JobState,
        *,
        stage_callback: StageCallback = None,
        progress_callback: ProgressCallback = None,
        log_callback: LogCallback = None,
    ) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        ready_segments = [
            segment
            for segment in job.segments
            if (
                segment.upload_enabled
                and segment.metadata_ready
                and segment.output_path is not None
                and segment.sidecar_path
                and segment.sidecar_path.exists()
            )
        ]
        segment_count = max(len(ready_segments), 1)
        for index, segment in enumerate(ready_segments, start=1):
            payload = read_sidecar(segment.sidecar_path)
            metadata = payload.get("metadata", {})
            upload_job = UploadJob(
                video_path=segment.output_path,
                title=str(metadata.get("title", "")),
                description=str(metadata.get("description", "")),
                tags=list(metadata.get("tags", [])),
                playlist_id=str(metadata.get("playlist_id", "")),
                privacy_status=str(metadata.get("privacy_status", "private")),
                thumbnail_path=segment.thumbnail_path,
                category_id=str(metadata.get("category_id", job.category_id or "22")),
                made_for_kids=False,
            )
            try:
                result = self.uploader.upload_video(
                    upload_job,
                    interactive=True,
                    stage_callback=stage_callback,
                    progress_callback=lambda value, i=index: progress_callback(
                        None if value is None else int(((i - 1) / segment_count) * 100 + (value / segment_count))
                    )
                    if progress_callback
                    else None,
                    log_callback=log_callback,
                )
            except YouTubeUploadCancelled:
                raise
            except Exception as exc:
                segment.upload_status = "failed"
                segment.upload_error = str(exc)
                payload["upload"] = {
                    "status": segment.upload_status,
                    "video_id": "",
                    "url": "",
                    "error_message": segment.upload_error,
                }
                write_sidecar(segment.sidecar_path, payload)
                if log_callback:
                    log_callback(f"{segment.clip_name} 업로드 실패: {exc}")
                continue

            segment.upload_status = "uploaded"
            segment.upload_video_id = result.get("video_id", "")
            segment.upload_url = result.get("url", "")
            segment.upload_error = ""
            payload["upload"] = {
                "status": segment.upload_status,
                "video_id": segment.upload_video_id,
                "url": segment.upload_url,
                "error_message": "",
            }
            write_sidecar(segment.sidecar_path, payload)
            results.append(result)
        return results

    def finalize_job_cleanup(
        self,
        job: JobState,
        *,
        log_callback: LogCallback = None,
    ) -> JobState:
        for segment in job.segments:
            self._finalize_segment_paths(segment, log_callback=log_callback)

        if job.synced_source_path and job.synced_source_path.exists():
            job.synced_source_path.unlink(missing_ok=True)
            if log_callback:
                log_callback(f"동기화 중간 파일을 정리했습니다: {job.synced_source_path.name}")

        if job.source_path.exists():
            job.source_path.unlink(missing_ok=True)
            job.source_deleted = True
            if log_callback:
                log_callback(f"원본 MKV를 삭제했습니다: {job.source_path.name}")

        active_segments = [segment for segment in job.segments if segment.upload_enabled]
        if not active_segments or all(segment.upload_status == "uploaded" for segment in active_segments):
            job.cleanup_status = "done"
        else:
            job.cleanup_status = "pending_retry"
        return job

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

    def _finalize_segment_paths(self, segment: SegmentState, *, log_callback: LogCallback = None) -> None:
        if segment.output_path is None or not segment.output_path.exists():
            return
        base_dir = segment.output_path.parent
        payload = read_sidecar(segment.sidecar_path) if segment.sidecar_path and segment.sidecar_path.exists() else {}
        metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
        target_stem = sanitize_filename(str(metadata.get("title", "")).strip() or segment.clip_name, segment.clip_name)

        target_video_path = self._resolve_unique_path(base_dir / f"{target_stem}{segment.output_path.suffix}", segment.output_path)
        if target_video_path != segment.output_path:
            shutil.move(str(segment.output_path), str(target_video_path))
            segment.output_path = target_video_path
            if log_callback:
                log_callback(f"세그먼트 파일명을 정리했습니다: {target_video_path.name}")

        if segment.thumbnail_path and segment.thumbnail_path.exists():
            target_thumb_path = self._resolve_unique_path(
                base_dir / f"{target_stem}_thumbnail{segment.thumbnail_path.suffix}",
                segment.thumbnail_path,
            )
            if target_thumb_path != segment.thumbnail_path:
                shutil.move(str(segment.thumbnail_path), str(target_thumb_path))
                segment.thumbnail_path = target_thumb_path

        if segment.sidecar_path and segment.sidecar_path.exists():
            target_sidecar_path = self._resolve_unique_path(
                base_dir / f"{target_stem}{segment.sidecar_path.suffix}",
                segment.sidecar_path,
            )
            if target_sidecar_path != segment.sidecar_path:
                shutil.move(str(segment.sidecar_path), str(target_sidecar_path))
                segment.sidecar_path = target_sidecar_path
            updated_payload = read_sidecar(segment.sidecar_path)
            updated_payload["outputs"] = {
                "video_path": str(segment.output_path),
                "thumbnail_path": str(segment.thumbnail_path) if segment.thumbnail_path else "",
            }
            write_sidecar(segment.sidecar_path, updated_payload)

    @staticmethod
    def _resolve_unique_path(target_path: Path, current_path: Path) -> Path:
        if not target_path.exists() or target_path.resolve() == current_path.resolve():
            return target_path
        index = 2
        while True:
            candidate = target_path.with_name(f"{target_path.stem}_{index}{target_path.suffix}")
            if not candidate.exists() or candidate.resolve() == current_path.resolve():
                return candidate
            index += 1

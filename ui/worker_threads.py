from __future__ import annotations

from enum import Enum
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from core.video_processor import VideoJob, VideoProcessingCancelled, VideoProcessor
from core.youtube_uploader import UploadJob, YouTubeUploadCancelled, YouTubeUploader


class AppWorkflowState(str, Enum):
    IDLE = "IDLE"
    PROCESSING = "PROCESSING"
    READY_TO_UPLOAD = "READY_TO_UPLOAD"
    AUTHENTICATING = "AUTHENTICATING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


class WorkerAction(str, Enum):
    AUTHENTICATE = "authenticate"
    PROCESS = "process"
    UPLOAD = "upload"
    PROCESS_AND_UPLOAD = "process_and_upload"


class JobWorker(QObject):
    stage_changed = pyqtSignal(str)
    progress = pyqtSignal(object)
    log = pyqtSignal(str)
    error = pyqtSignal(str)
    completed = pyqtSignal(dict)
    finished = pyqtSignal()

    def __init__(
        self,
        *,
        action: WorkerAction,
        video_job: VideoJob | None = None,
        upload_job: UploadJob | None = None,
    ) -> None:
        super().__init__()
        self.action = action
        self.video_job = video_job
        self.upload_job = upload_job
        self.video_processor = VideoProcessor() if action in {WorkerAction.PROCESS, WorkerAction.PROCESS_AND_UPLOAD} else None
        self.youtube_uploader = YouTubeUploader() if action in {WorkerAction.AUTHENTICATE, WorkerAction.UPLOAD, WorkerAction.PROCESS_AND_UPLOAD} else None

    def cancel(self) -> None:
        if self.video_processor is not None:
            self.video_processor.cancel()
        if self.youtube_uploader is not None:
            self.youtube_uploader.cancel()

    @pyqtSlot()
    def run(self) -> None:
        try:
            if self.action == WorkerAction.AUTHENTICATE:
                self._authenticate()
            elif self.action == WorkerAction.PROCESS:
                self._process()
            elif self.action == WorkerAction.UPLOAD:
                self._upload()
            else:
                self._process_and_upload()
        except (VideoProcessingCancelled, YouTubeUploadCancelled) as exc:
            self.log.emit(str(exc))
            self.completed.emit({"status": "cancelled", "action": self.action.value, "message": str(exc)})
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()

    def _authenticate(self) -> None:
        assert self.youtube_uploader is not None
        self.stage_changed.emit("AUTHENTICATING")
        self.progress.emit(None)
        credentials = self.youtube_uploader.ensure_credentials(interactive=True, log_callback=self.log.emit)
        self.youtube_uploader.build_service(credentials)
        self.stage_changed.emit("DONE")
        self.completed.emit({"status": "success", "action": self.action.value})

    def _process(self) -> Path:
        output_path = self._process_video_only()
        self.completed.emit(
            {
                "status": "success",
                "action": self.action.value,
                "output_path": str(output_path),
            }
        )
        return output_path

    def _process_video_only(self) -> Path:
        assert self.video_processor is not None
        assert self.video_job is not None
        self.progress.emit(None)
        return self.video_processor.process_video(
            self.video_job,
            stage_callback=self.stage_changed.emit,
            log_callback=self.log.emit,
        )

    def _upload(self, upload_job: UploadJob | None = None) -> dict[str, str]:
        assert self.youtube_uploader is not None
        job = upload_job or self.upload_job
        assert job is not None
        result = self.youtube_uploader.upload_video(
            job,
            interactive=True,
            stage_callback=self.stage_changed.emit,
            progress_callback=self.progress.emit,
            log_callback=self.log.emit,
        )
        if upload_job is None:
            self.completed.emit(
                {
                    "status": "success",
                    "action": self.action.value,
                    **result,
                }
            )
        return result

    def _process_and_upload(self) -> None:
        assert self.upload_job is not None
        output_path = self._process_video_only()
        upload_job = UploadJob(
            video_path=output_path,
            title=self.upload_job.title,
            description=self.upload_job.description,
            tags=self.upload_job.tags,
            playlist_id=self.upload_job.playlist_id,
            privacy_status=self.upload_job.privacy_status,
            thumbnail_path=self.upload_job.thumbnail_path,
            category_id=self.upload_job.category_id,
            made_for_kids=self.upload_job.made_for_kids,
        )
        result = self._upload(upload_job=upload_job)
        self.completed.emit(
            {
                "status": "success",
                "action": self.action.value,
                "output_path": str(output_path),
                **result,
            }
        )


def create_worker_thread(
    *,
    action: WorkerAction,
    video_job: VideoJob | None = None,
    upload_job: UploadJob | None = None,
) -> tuple[QThread, JobWorker]:
    thread = QThread()
    worker = JobWorker(action=action, video_job=video_job, upload_job=upload_job)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    return thread, worker

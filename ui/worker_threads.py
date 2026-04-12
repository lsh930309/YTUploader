from __future__ import annotations

from enum import Enum
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from core.models import ClipDraft, JobState
from core.mpc_be import MPCBEController
from core.runtime_installer import AppRuntimeInstaller
from core.workflow import WorkflowRunner
from core.youtube_uploader import YouTubeUploadCancelled, YouTubeUploader


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
    IMPORT_MPC_BE = "import_mpc_be"
    INSTALL_RUNTIME = "install_runtime"
    APPLY_SYNC = "apply_sync"
    SPLIT_SEGMENTS = "split_segments"
    UPLOAD_AND_CLEANUP = "upload_and_cleanup"


class JobWorker(QObject):
    stage_changed = pyqtSignal(str)
    progress = pyqtSignal(object)
    log = pyqtSignal(str)
    error = pyqtSignal(str)
    completed = pyqtSignal(object)
    finished = pyqtSignal()

    def __init__(
        self,
        *,
        action: WorkerAction,
        job_state: JobState | None = None,
        runtime_package: str | None = None,
        sync_output_path: Path | None = None,
        segment_drafts: list[ClipDraft] | None = None,
    ) -> None:
        super().__init__()
        self.action = action
        self.job_state = job_state
        self.runtime_package = runtime_package
        self.sync_output_path = sync_output_path
        self.segment_drafts = list(segment_drafts or [])
        self.workflow_runner = WorkflowRunner()
        self.youtube_uploader = YouTubeUploader()
        self.mpc_be_controller = MPCBEController()
        self.runtime_installer = AppRuntimeInstaller(mpc_be_controller=self.mpc_be_controller)

    def cancel(self) -> None:
        self.workflow_runner.cancel()
        self.youtube_uploader.cancel()

    @pyqtSlot()
    def run(self) -> None:
        try:
            if self.action == WorkerAction.AUTHENTICATE:
                self._authenticate()
            elif self.action == WorkerAction.IMPORT_MPC_BE:
                self._import_mpc_be_settings()
            elif self.action == WorkerAction.INSTALL_RUNTIME:
                self._install_runtime_package()
            elif self.action == WorkerAction.APPLY_SYNC:
                self._apply_sync()
            elif self.action == WorkerAction.SPLIT_SEGMENTS:
                self._split_segments()
            else:
                self._upload_and_cleanup()
        except YouTubeUploadCancelled as exc:
            self.log.emit(str(exc))
            self.completed.emit({"status": "cancelled", "action": self.action.value, "message": str(exc)})
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()

    def _authenticate(self) -> None:
        self.stage_changed.emit("AUTHENTICATING")
        self.progress.emit(None)
        credentials = self.youtube_uploader.ensure_credentials(interactive=True, log_callback=self.log.emit)
        self.youtube_uploader.build_service(credentials)
        self.stage_changed.emit("DONE")
        self.completed.emit({"status": "success", "action": self.action.value})

    def _import_mpc_be_settings(self) -> None:
        source = self.mpc_be_controller.import_settings()
        self.completed.emit({"status": "success", "action": self.action.value, "source": source})

    def _install_runtime_package(self) -> None:
        assert self.runtime_package is not None
        self.stage_changed.emit("INSTALLING_RUNTIME")
        self.progress.emit(None)
        status = self.runtime_installer.install_package(self.runtime_package, log_callback=self.log.emit)
        self.stage_changed.emit("DONE")
        self.completed.emit(
            {
                "status": "success",
                "action": self.action.value,
                "package_id": status.package_id,
                "package_label": status.label,
                "message": f"{status.label} 설치를 완료했습니다.",
            }
        )

    def _apply_sync(self) -> None:
        assert self.job_state is not None
        assert self.sync_output_path is not None
        self.progress.emit(None)
        job_state = self.workflow_runner.apply_audio_sync(
            self.job_state,
            synced_path=self.sync_output_path,
            stage_callback=self.stage_changed.emit,
            log_callback=self.log.emit,
        )
        self.stage_changed.emit("DONE")
        self.completed.emit({"status": "success", "action": self.action.value, "job": job_state})

    def _split_segments(self) -> None:
        assert self.job_state is not None
        clips = self.segment_drafts or list(self.job_state.segment_drafts)
        if not clips:
            raise RuntimeError("분할할 세그먼트 초안이 없습니다.")
        self.progress.emit(None)
        segments = self.workflow_runner.split_segments(
            self.job_state,
            clips=clips,
            stage_callback=self.stage_changed.emit,
            log_callback=self.log.emit,
        )
        self.job_state.segments = segments
        self.stage_changed.emit("DONE")
        self.completed.emit({"status": "success", "action": self.action.value, "job": self.job_state})

    def _upload_and_cleanup(self) -> None:
        assert self.job_state is not None
        results = self.workflow_runner.upload_ready_segments(
            self.job_state,
            stage_callback=self.stage_changed.emit,
            progress_callback=self.progress.emit,
            log_callback=self.log.emit,
        )
        self.stage_changed.emit("CLEANUP")
        job_state = self.workflow_runner.finalize_job_cleanup(self.job_state, log_callback=self.log.emit)
        self.stage_changed.emit("DONE")
        self.completed.emit(
            {
                "status": "success",
                "action": self.action.value,
                "results": results,
                "job": job_state,
            }
        )


def create_worker_thread(
    *,
    action: WorkerAction,
    job_state: JobState | None = None,
    runtime_package: str | None = None,
    sync_output_path: Path | None = None,
    segment_drafts: list[ClipDraft] | None = None,
) -> tuple[QThread, JobWorker]:
    thread = QThread()
    worker = JobWorker(
        action=action,
        job_state=job_state,
        runtime_package=runtime_package,
        sync_output_path=sync_output_path,
        segment_drafts=segment_drafts,
    )
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    return thread, worker

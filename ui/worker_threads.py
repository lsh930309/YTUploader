from __future__ import annotations

from enum import Enum
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from core.models import ExportBundle, JobDraft
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
    PROCESS = "process"
    UPLOAD = "upload"
    PROCESS_AND_UPLOAD = "process_and_upload"


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
        job_draft: JobDraft | None = None,
        output_dir: Path | None = None,
        export_bundle: ExportBundle | None = None,
        runtime_package: str | None = None,
    ) -> None:
        super().__init__()
        self.action = action
        self.job_draft = job_draft
        self.output_dir = output_dir
        self.export_bundle = export_bundle
        self.runtime_package = runtime_package
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
            elif self.action == WorkerAction.PROCESS:
                self._process()
            elif self.action == WorkerAction.UPLOAD:
                self._upload()
            else:
                self._process_and_upload()
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
        self.completed.emit(
            {
                "status": "success",
                "action": self.action.value,
                "source": source,
            }
        )

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

    def _process(self, *, emit_completed: bool = True) -> ExportBundle:
        assert self.job_draft is not None
        assert self.output_dir is not None
        self.progress.emit(None)
        bundle = self.workflow_runner.process_job(
            self.job_draft,
            output_dir=self.output_dir,
            stage_callback=self.stage_changed.emit,
            log_callback=self.log.emit,
        )
        if emit_completed:
            self.completed.emit(bundle)
        return bundle

    def _upload(self, bundle: ExportBundle | None = None) -> list[dict[str, str]]:
        selected_bundle = bundle or self.export_bundle
        assert selected_bundle is not None
        results = self.workflow_runner.upload_selected_clips(
            selected_bundle,
            stage_callback=self.stage_changed.emit,
            progress_callback=self.progress.emit,
            log_callback=self.log.emit,
        )
        if bundle is None:
            self.completed.emit(
                {
                    "status": "success",
                    "action": self.action.value,
                    "results": results,
                    "bundle": selected_bundle,
                }
            )
        return results

    def _process_and_upload(self) -> None:
        bundle = self._process(emit_completed=False)
        results = self._upload(bundle=bundle)
        self.completed.emit(
            {
                "status": "success",
                "action": self.action.value,
                "results": results,
                "bundle": bundle,
            }
        )


def create_worker_thread(
    *,
    action: WorkerAction,
    job_draft: JobDraft | None = None,
    output_dir: Path | None = None,
    export_bundle: ExportBundle | None = None,
    runtime_package: str | None = None,
) -> tuple[QThread, JobWorker]:
    thread = QThread()
    worker = JobWorker(
        action=action,
        job_draft=job_draft,
        output_dir=output_dir,
        export_bundle=export_bundle,
        runtime_package=runtime_package,
    )
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    return thread, worker

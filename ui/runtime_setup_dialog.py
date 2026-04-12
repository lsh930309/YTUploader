from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from core.runtime_installer import AppRuntimeInstaller
from ui.worker_threads import WorkerAction, create_worker_thread


class RuntimeSetupDialog(QDialog):
    def __init__(self, runtime_installer: AppRuntimeInstaller | None = None) -> None:
        super().__init__()
        self.runtime_installer = runtime_installer or AppRuntimeInstaller()
        self._active_thread = None
        self._active_worker = None
        self._current_package_id: str | None = None
        self._pending_packages: list[str] = []
        self._status_labels: dict[str, QLabel] = {}
        self._install_buttons: dict[str, QPushButton] = {}

        self.setWindowTitle("필수 도구 준비")
        self.resize(760, 520)
        self.setModal(True)

        self._build_ui()
        self._refresh_statuses()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.info_label = QLabel(self)
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        status_layout = QFormLayout()
        for package_id in self.runtime_installer.REQUIRED_PACKAGE_IDS:
            label = QLabel("-", self)
            button = QPushButton("설치", self)
            row = QHBoxLayout()
            row.addWidget(label)
            row.addStretch(1)
            row.addWidget(button)
            status_layout.addRow(self.runtime_installer.PACKAGE_LABELS[package_id], row)
            button.clicked.connect(lambda _checked=False, pkg=package_id: self._install_single(pkg))
            self._status_labels[package_id] = label
            self._install_buttons[package_id] = button

        layout.addLayout(status_layout)

        self.log_view = QPlainTextEdit(self)
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, stretch=1)

        actions_row = QHBoxLayout()
        self.retry_button = QPushButton("다시 검사", self)
        self.install_all_button = QPushButton("누락 항목 모두 설치", self)
        self.close_button = QPushButton("종료", self)
        self.continue_button = QPushButton("작업 시작", self)

        actions_row.addWidget(self.retry_button)
        actions_row.addWidget(self.install_all_button)
        actions_row.addStretch(1)
        actions_row.addWidget(self.close_button)
        actions_row.addWidget(self.continue_button)
        layout.addLayout(actions_row)

        self.retry_button.clicked.connect(self._refresh_statuses)
        self.install_all_button.clicked.connect(self._install_all_missing)
        self.close_button.clicked.connect(self.reject)
        self.continue_button.clicked.connect(self.accept)

    def _refresh_statuses(self) -> None:
        statuses = {status.package_id: status for status in self.runtime_installer.get_required_statuses()}
        missing_labels = [
            self.runtime_installer.PACKAGE_LABELS[package_id]
            for package_id, status in statuses.items()
            if not status.installed
        ]
        ready = not missing_labels
        busy = self._active_thread is not None

        if ready:
            self.info_label.setText(
                "필수 도구 준비가 완료되었습니다. 이제 메인 워크플로우로 진입할 수 있습니다."
            )
        else:
            self.info_label.setText(
                "메인 워크플로우에 진입하기 전에 아래 필수 도구를 모두 준비해야 합니다: "
                + ", ".join(missing_labels)
            )

        for package_id, status in statuses.items():
            label_text = status.status_text
            if status.source_label and status.status_text != "설치됨":
                label_text = f"{label_text} ({status.source_label})"
            self._status_labels[package_id].setText(label_text)
            self._install_buttons[package_id].setEnabled(not busy and not status.installed)
            self._install_buttons[package_id].setText("설치됨" if status.installed else "설치")

        self.retry_button.setEnabled(not busy)
        self.install_all_button.setEnabled(not busy and bool(missing_labels))
        self.close_button.setEnabled(not busy)
        self.continue_button.setEnabled(not busy and ready)

    def _install_single(self, package_id: str) -> None:
        if self._active_thread is not None:
            return
        self._pending_packages = []
        self._start_install(package_id)

    def _install_all_missing(self) -> None:
        if self._active_thread is not None:
            return
        self._pending_packages = self.runtime_installer.missing_required_package_ids()
        if not self._pending_packages:
            self._refresh_statuses()
            return
        self._start_install(self._pending_packages.pop(0))

    def _start_install(self, package_id: str) -> None:
        thread, worker = create_worker_thread(action=WorkerAction.INSTALL_RUNTIME, runtime_package=package_id)
        worker.log.connect(self._append_log)
        worker.error.connect(self._on_worker_error)
        worker.completed.connect(self._on_worker_completed)
        worker.finished.connect(self._on_worker_finished)
        thread.finished.connect(self._on_thread_finished)
        self._active_thread = thread
        self._active_worker = worker
        self._current_package_id = package_id
        self._append_log(f"{self.runtime_installer.PACKAGE_LABELS[package_id]} 설치를 시작합니다.")
        self._refresh_statuses()
        thread.start()

    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def _on_worker_error(self, message: str) -> None:
        self._pending_packages = []
        self._append_log(message)
        QMessageBox.critical(self, "도구 설치 실패", message)

    def _on_worker_completed(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        message = str(payload.get("message", "")).strip()
        if message:
            self._append_log(message)

    def _on_worker_finished(self) -> None:
        if self._current_package_id is not None:
            self._append_log(f"{self.runtime_installer.PACKAGE_LABELS[self._current_package_id]} 작업이 종료되었습니다.")

    def _on_thread_finished(self) -> None:
        self._active_worker = None
        self._active_thread = None
        self._current_package_id = None
        self._refresh_statuses()
        if self._pending_packages:
            self._start_install(self._pending_packages.pop(0))

    def reject(self) -> None:
        if self._active_thread is not None:
            QMessageBox.information(self, "진행 중", "설치 작업이 진행 중입니다. 완료 후 다시 시도해 주세요.")
            return
        super().reject()

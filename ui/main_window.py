from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.data_manager import DataManager, TemplateRenderError, build_upload_description, render_template
from core.paths import ensure_runtime_dirs, get_client_secrets_path, get_credentials_dir
from core.video_processor import CLEANUP, DONE, REMUXING, SYNCING, VALIDATING, VideoJob
from core.youtube_uploader import AUTHENTICATING, UPLOADING, UploadJob
from .worker_threads import AppWorkflowState, WorkerAction, create_worker_thread

LOGGER = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        ensure_runtime_dirs()
        self.data_manager = DataManager()
        self._active_thread = None
        self._active_worker = None

        self.setWindowTitle("YTUploader")
        self.resize(1200, 900)

        self._build_ui()
        self._load_settings_into_ui()
        self.set_state(AppWorkflowState.IDLE)

    def _build_ui(self) -> None:
        central_widget = QWidget(self)
        root_layout = QVBoxLayout(central_widget)

        root_layout.addWidget(self._build_action_group())
        root_layout.addWidget(self._build_file_group())
        root_layout.addWidget(self._build_edit_group())
        root_layout.addWidget(self._build_metadata_group())
        root_layout.addWidget(self._build_log_group())

        self.setCentralWidget(central_widget)

    def _build_action_group(self) -> QGroupBox:
        group = QGroupBox("Actions", self)
        layout = QHBoxLayout(group)

        self.auth_button = QPushButton("Google Login", group)
        self.load_template_button = QPushButton("Load Template", group)
        self.save_template_button = QPushButton("Save Template", group)
        self.process_button = QPushButton("Process Only", group)
        self.upload_button = QPushButton("Upload Existing MP4", group)
        self.process_upload_button = QPushButton("Process + Upload", group)
        self.cancel_button = QPushButton("Cancel", group)

        for button in (
            self.auth_button,
            self.load_template_button,
            self.save_template_button,
            self.process_button,
            self.upload_button,
            self.process_upload_button,
            self.cancel_button,
        ):
            layout.addWidget(button)

        self.auth_button.clicked.connect(self._authenticate_google)
        self.load_template_button.clicked.connect(self._apply_saved_templates)
        self.save_template_button.clicked.connect(self._save_template)
        self.process_button.clicked.connect(self._process_only)
        self.upload_button.clicked.connect(self._upload_existing)
        self.process_upload_button.clicked.connect(self._process_and_upload)
        self.cancel_button.clicked.connect(self._cancel_active_job)
        return group

    def _build_file_group(self) -> QGroupBox:
        group = QGroupBox("Input / Output Files", self)
        layout = QGridLayout(group)

        self.input_path_edit = QLineEdit(group)
        self.output_path_edit = QLineEdit(group)
        self.thumbnail_path_edit = QLineEdit(group)

        input_browse = QPushButton("Browse", group)
        output_browse = QPushButton("Browse", group)
        thumbnail_browse = QPushButton("Browse", group)

        layout.addWidget(QLabel("Input MKV"), 0, 0)
        layout.addWidget(self.input_path_edit, 0, 1)
        layout.addWidget(input_browse, 0, 2)

        layout.addWidget(QLabel("Output MP4"), 1, 0)
        layout.addWidget(self.output_path_edit, 1, 1)
        layout.addWidget(output_browse, 1, 2)

        layout.addWidget(QLabel("Thumbnail"), 2, 0)
        layout.addWidget(self.thumbnail_path_edit, 2, 1)
        layout.addWidget(thumbnail_browse, 2, 2)

        input_browse.clicked.connect(self._choose_input_file)
        output_browse.clicked.connect(self._choose_output_file)
        thumbnail_browse.clicked.connect(self._choose_thumbnail_file)
        return group

    def _build_edit_group(self) -> QGroupBox:
        group = QGroupBox("Edit Options", self)
        layout = QGridLayout(group)

        self.delay_spin = QSpinBox(group)
        self.delay_spin.setRange(-300000, 300000)
        self.delay_spin.setSingleStep(50)

        self.start_time_edit = QLineEdit(group)
        self.start_time_edit.setPlaceholderText("00:00:00")
        self.end_time_edit = QLineEdit(group)
        self.end_time_edit.setPlaceholderText("00:00:00")

        layout.addWidget(QLabel("Audio Delay (ms)"), 0, 0)
        layout.addWidget(self.delay_spin, 0, 1)
        layout.addWidget(QLabel("Start Time"), 1, 0)
        layout.addWidget(self.start_time_edit, 1, 1)
        layout.addWidget(QLabel("End Time"), 2, 0)
        layout.addWidget(self.end_time_edit, 2, 1)
        return group

    def _build_metadata_group(self) -> QGroupBox:
        group = QGroupBox("Upload Metadata", self)
        layout = QGridLayout(group)

        self.title_edit = QLineEdit(group)
        self.description_edit = QPlainTextEdit(group)
        self.description_edit.setPlaceholderText("Description or description template")
        self.chapters_edit = QPlainTextEdit(group)
        self.chapters_edit.setPlaceholderText("Optional chapter text")
        self.tags_edit = QLineEdit(group)
        self.tags_edit.setPlaceholderText("tag1, tag2, tag3")
        self.playlist_edit = QLineEdit(group)
        self.category_edit = QLineEdit(group)
        self.privacy_combo = QComboBox(group)
        self.privacy_combo.addItems(["private", "unlisted", "public"])

        layout.addWidget(QLabel("Title"), 0, 0)
        layout.addWidget(self.title_edit, 0, 1)
        layout.addWidget(QLabel("Description"), 1, 0)
        layout.addWidget(self.description_edit, 1, 1)
        layout.addWidget(QLabel("Chapters"), 2, 0)
        layout.addWidget(self.chapters_edit, 2, 1)
        layout.addWidget(QLabel("Tags"), 3, 0)
        layout.addWidget(self.tags_edit, 3, 1)
        layout.addWidget(QLabel("Playlist ID"), 4, 0)
        layout.addWidget(self.playlist_edit, 4, 1)
        layout.addWidget(QLabel("Privacy"), 5, 0)
        layout.addWidget(self.privacy_combo, 5, 1)
        layout.addWidget(QLabel("Category ID"), 6, 0)
        layout.addWidget(self.category_edit, 6, 1)
        return group

    def _build_log_group(self) -> QGroupBox:
        group = QGroupBox("Status / Logs", self)
        layout = QGridLayout(group)

        self.state_value = QLabel("-", group)
        self.stage_value = QLabel("-", group)
        self.progress_bar = QProgressBar(group)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.log_view = QPlainTextEdit(group)
        self.log_view.setReadOnly(True)

        layout.addWidget(QLabel("State"), 0, 0)
        layout.addWidget(self.state_value, 0, 1)
        layout.addWidget(QLabel("Stage"), 1, 0)
        layout.addWidget(self.stage_value, 1, 1)
        layout.addWidget(QLabel("Progress"), 2, 0)
        layout.addWidget(self.progress_bar, 2, 1)
        layout.addWidget(self.log_view, 3, 0, 1, 2)
        return group

    def _load_settings_into_ui(self) -> None:
        settings = self.data_manager.load()
        self.title_edit.setText(settings["title_template"])
        self.description_edit.setPlainText(settings["description_template"])
        self.chapters_edit.setPlainText(settings["chapters_template"])
        self.tags_edit.setText(", ".join(settings["tags"]))
        self.playlist_edit.setText(settings["playlist_id"])
        self.category_edit.setText(settings["category_id"])
        self.delay_spin.setValue(int(settings["last_delay_ms"]))
        if settings["privacy_status"]:
            index = self.privacy_combo.findText(settings["privacy_status"])
            if index >= 0:
                self.privacy_combo.setCurrentIndex(index)

    def _choose_input_file(self) -> None:
        settings = self.data_manager.load()
        start_dir = settings["last_input_dir"] or str(Path.home())
        selected, _ = QFileDialog.getOpenFileName(self, "Select MKV File", start_dir, "MKV Files (*.mkv);;All Files (*)")
        if not selected:
            return

        input_path = Path(selected)
        self.input_path_edit.setText(str(input_path))
        output_dir = settings["last_output_dir"] or input_path.parent
        self.output_path_edit.setText(str(self.data_manager.suggest_output_path(input_path, output_dir)))
        self.data_manager.update_recent_paths(input_path=input_path)
        self._apply_saved_templates()

    def _choose_output_file(self) -> None:
        start_path = self.output_path_edit.text().strip()
        initial = start_path or str(Path.home() / "output.mp4")
        selected, _ = QFileDialog.getSaveFileName(self, "Select Output MP4", initial, "MP4 Files (*.mp4)")
        if not selected:
            return
        output_path = Path(selected)
        if output_path.suffix.lower() != ".mp4":
            output_path = output_path.with_suffix(".mp4")
        self.output_path_edit.setText(str(output_path))
        self.data_manager.update_recent_paths(output_path=output_path)

    def _choose_thumbnail_file(self) -> None:
        settings = self.data_manager.load()
        start_dir = settings["default_thumbnail_dir"] or str(Path.home())
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Select Thumbnail",
            start_dir,
            "Image Files (*.png *.jpg *.jpeg *.webp);;All Files (*)",
        )
        if not selected:
            return
        thumbnail_path = Path(selected)
        self.thumbnail_path_edit.setText(str(thumbnail_path))
        self.data_manager.update_recent_paths(thumbnail_path=thumbnail_path)

    def _apply_saved_templates(self) -> None:
        source_path = self._current_source_path()
        try:
            templates = self.data_manager.load_templates_for_source(source_path)
        except TemplateRenderError as exc:
            self._show_error(str(exc))
            return

        self.title_edit.setText(templates["title"])
        self.description_edit.setPlainText(templates["description"])
        self.chapters_edit.setPlainText(templates["chapters"])
        self.tags_edit.setText(", ".join(templates["tags"]))
        self.playlist_edit.setText(templates["playlist_id"])
        self.category_edit.setText(templates["category_id"])
        self.delay_spin.setValue(int(templates["last_delay_ms"]))
        index = self.privacy_combo.findText(templates["privacy_status"])
        if index >= 0:
            self.privacy_combo.setCurrentIndex(index)
        self._append_log("Loaded saved template values.")

    def _save_template(self) -> None:
        settings = self.data_manager.load()
        input_path = self._path_or_none(self.input_path_edit.text())
        output_path = self._path_or_none(self.output_path_edit.text())
        thumbnail_path = self._path_or_none(self.thumbnail_path_edit.text())

        settings.update(
            {
                "title_template": self.title_edit.text().strip(),
                "description_template": self.description_edit.toPlainText(),
                "chapters_template": self.chapters_edit.toPlainText(),
                "tags": self._parse_tags(),
                "playlist_id": self.playlist_edit.text().strip(),
                "privacy_status": self.privacy_combo.currentText(),
                "category_id": self.category_edit.text().strip() or "22",
                "default_thumbnail_dir": str(thumbnail_path.parent) if thumbnail_path else settings["default_thumbnail_dir"],
                "last_input_dir": str(input_path.parent) if input_path else settings["last_input_dir"],
                "last_output_dir": str(output_path.parent) if output_path else settings["last_output_dir"],
                "last_delay_ms": self.delay_spin.value(),
            }
        )
        self.data_manager.save(settings)
        self._append_log("Saved current UI values as the default template.")

    def _authenticate_google(self) -> None:
        if not self._ensure_client_secrets_available():
            return
        self._start_worker(action=WorkerAction.AUTHENTICATE)

    def _process_only(self) -> None:
        try:
            video_job = self._collect_video_job()
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self._persist_recent_preferences(video_job=video_job)
        self._start_worker(action=WorkerAction.PROCESS, video_job=video_job)

    def _upload_existing(self) -> None:
        if not self._ensure_client_secrets_available():
            return

        output_path = self._path_or_none(self.output_path_edit.text())
        if output_path is None or not output_path.exists():
            selected, _ = QFileDialog.getOpenFileName(self, "Select Existing MP4", str(Path.home()), "MP4 Files (*.mp4)")
            if not selected:
                return
            output_path = Path(selected)
            self.output_path_edit.setText(str(output_path))

        try:
            upload_job = self._collect_upload_job(video_path=output_path)
        except (ValueError, TemplateRenderError) as exc:
            self._show_error(str(exc))
            return

        self._persist_recent_preferences(output_path=output_path)
        self._start_worker(action=WorkerAction.UPLOAD, upload_job=upload_job)

    def _process_and_upload(self) -> None:
        if not self._ensure_client_secrets_available():
            return

        try:
            video_job = self._collect_video_job()
            upload_job = self._collect_upload_job(video_path=video_job.output_mp4)
        except (ValueError, TemplateRenderError) as exc:
            self._show_error(str(exc))
            return

        self._persist_recent_preferences(video_job=video_job, output_path=video_job.output_mp4)
        self._start_worker(action=WorkerAction.PROCESS_AND_UPLOAD, video_job=video_job, upload_job=upload_job)

    def _cancel_active_job(self) -> None:
        if self._active_worker is None:
            return
        self._active_worker.cancel()
        self._append_log("Cancellation requested.")

    def _start_worker(
        self,
        *,
        action: WorkerAction,
        video_job: VideoJob | None = None,
        upload_job: UploadJob | None = None,
    ) -> None:
        if self._active_thread is not None:
            self._show_error("A job is already running.")
            return

        thread, worker = create_worker_thread(action=action, video_job=video_job, upload_job=upload_job)
        worker.stage_changed.connect(self._on_stage_changed)
        worker.progress.connect(self._on_progress)
        worker.log.connect(self._append_log)
        worker.error.connect(self._on_worker_error)
        worker.completed.connect(self._on_worker_completed)
        worker.finished.connect(self._on_worker_finished)
        thread.finished.connect(self._on_thread_finished)

        self._active_thread = thread
        self._active_worker = worker

        if action == WorkerAction.AUTHENTICATE:
            self.set_state(AppWorkflowState.AUTHENTICATING)
        elif action == WorkerAction.UPLOAD:
            self.set_state(AppWorkflowState.UPLOADING)
        else:
            self.set_state(AppWorkflowState.PROCESSING)

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        thread.start()

    def _on_stage_changed(self, stage: str) -> None:
        self.stage_value.setText(stage)

        if stage in {VALIDATING, SYNCING, REMUXING, CLEANUP}:
            self.set_state(AppWorkflowState.PROCESSING)
            self.progress_bar.setRange(0, 0)
        elif stage == AUTHENTICATING:
            self.set_state(AppWorkflowState.AUTHENTICATING)
            self.progress_bar.setRange(0, 0)
        elif stage == UPLOADING:
            self.set_state(AppWorkflowState.UPLOADING)
            self.progress_bar.setRange(0, 100)
        elif stage == DONE:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)

    def _on_progress(self, value: int | None) -> None:
        if value is None:
            self.progress_bar.setRange(0, 0)
            return
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(value)

    def _on_worker_error(self, message: str) -> None:
        LOGGER.error("Worker error: %s", message)
        self.set_state(AppWorkflowState.ERROR)
        self._append_log(message)
        self._show_error(message)

    def _on_worker_completed(self, payload: dict) -> None:
        action = payload.get("action")
        status = payload.get("status")

        if status == "cancelled":
            self.set_state(AppWorkflowState.CANCELLED)
            self._append_log(payload.get("message", "Job cancelled."))
            return

        if action == WorkerAction.PROCESS.value:
            output_path = payload.get("output_path")
            if output_path:
                self.output_path_edit.setText(output_path)
            self.set_state(AppWorkflowState.READY_TO_UPLOAD)
            self._append_log(f"Processing complete: {output_path}")
            return

        self.set_state(AppWorkflowState.DONE)
        if action == WorkerAction.AUTHENTICATE.value:
            self._append_log("Google login completed.")
            QMessageBox.information(self, "Google Login", "Google authentication completed successfully.")
            return

        output_path = payload.get("output_path")
        if output_path:
            self.output_path_edit.setText(output_path)
        url = payload.get("url")
        if url:
            self._append_log(f"YouTube upload complete: {url}")
            QMessageBox.information(self, "Upload Complete", f"Video uploaded successfully.\n{url}")

    def _on_worker_finished(self) -> None:
        self._append_log("Background job finished.")

    def _on_thread_finished(self) -> None:
        self._active_worker = None
        self._active_thread = None

    def set_state(self, state: AppWorkflowState) -> None:
        self.state_value.setText(state.value)
        busy = state in {
            AppWorkflowState.PROCESSING,
            AppWorkflowState.AUTHENTICATING,
            AppWorkflowState.UPLOADING,
        }

        controls = [
            self.auth_button,
            self.load_template_button,
            self.save_template_button,
            self.process_button,
            self.upload_button,
            self.process_upload_button,
            self.input_path_edit,
            self.output_path_edit,
            self.thumbnail_path_edit,
            self.delay_spin,
            self.start_time_edit,
            self.end_time_edit,
            self.title_edit,
            self.description_edit,
            self.chapters_edit,
            self.tags_edit,
            self.playlist_edit,
            self.privacy_combo,
            self.category_edit,
        ]
        for control in controls:
            control.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)

    def _ensure_client_secrets_available(self) -> bool:
        client_secrets_path = get_client_secrets_path()
        if client_secrets_path.exists():
            return True

        dialog = QMessageBox(self)
        dialog.setWindowTitle("Google OAuth Setup")
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setText("client_secrets.json is missing.")
        dialog.setInformativeText(f"Place your Google OAuth desktop app credentials in:\n{get_credentials_dir()}")
        open_button = dialog.addButton("Open Folder", QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton(QMessageBox.StandardButton.Close)
        dialog.exec()
        if dialog.clickedButton() is open_button:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(get_credentials_dir())))
        return False

    def _collect_video_job(self) -> VideoJob:
        input_path = self._required_path(self.input_path_edit.text(), "Select an input MKV file.")
        output_path = self._required_path(self.output_path_edit.text(), "Select an output MP4 path.")
        return VideoJob(
            input_mkv=input_path,
            output_mp4=output_path,
            delay_ms=self.delay_spin.value(),
            start_time=self.start_time_edit.text().strip() or None,
            end_time=self.end_time_edit.text().strip() or None,
        )

    def _collect_upload_job(self, *, video_path: Path) -> UploadJob:
        source_path = self._current_source_path() or video_path
        title = render_template(self.title_edit.text().strip(), source=source_path)
        if not title:
            raise ValueError("A YouTube title is required.")

        description = render_template(self.description_edit.toPlainText(), source=source_path)
        chapters = render_template(self.chapters_edit.toPlainText(), source=source_path)
        thumbnail_path = self._path_or_none(self.thumbnail_path_edit.text())

        return UploadJob(
            video_path=video_path,
            title=title,
            description=build_upload_description(description, chapters),
            tags=self._parse_tags(),
            playlist_id=self.playlist_edit.text().strip(),
            privacy_status=self.privacy_combo.currentText(),
            thumbnail_path=thumbnail_path,
            category_id=self.category_edit.text().strip() or "22",
            made_for_kids=False,
        )

    def _persist_recent_preferences(self, *, video_job: VideoJob | None = None, output_path: Path | None = None) -> None:
        input_path = video_job.input_mkv if video_job else self._path_or_none(self.input_path_edit.text())
        final_output = output_path or (video_job.output_mp4 if video_job else self._path_or_none(self.output_path_edit.text()))
        thumbnail = self._path_or_none(self.thumbnail_path_edit.text())
        self.data_manager.update_recent_paths(
            input_path=input_path,
            output_path=final_output,
            thumbnail_path=thumbnail,
            delay_ms=self.delay_spin.value(),
        )

    def _parse_tags(self) -> list[str]:
        return [tag.strip() for tag in self.tags_edit.text().split(",") if tag.strip()]

    def _current_source_path(self) -> Path | None:
        input_path = self._path_or_none(self.input_path_edit.text())
        if input_path is not None:
            return input_path
        return self._path_or_none(self.output_path_edit.text())

    @staticmethod
    def _path_or_none(value: str) -> Path | None:
        text = value.strip()
        return Path(text) if text else None

    @staticmethod
    def _required_path(value: str, message: str) -> Path:
        path = MainWindow._path_or_none(value)
        if path is None:
            raise ValueError(message)
        return path

    def _append_log(self, message: str) -> None:
        if not message:
            return
        self.log_view.appendPlainText(message)

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "Error", message)

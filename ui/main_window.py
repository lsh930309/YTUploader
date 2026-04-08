# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.catalog_store import CatalogStore
from core.data_manager import DataManager, TemplateRenderError, render_template
from core.models import ChapterMarker, ClipDraft, ExportBundle, JobDraft
from core.mpc_be import MPCBEController
from core.paths import ensure_runtime_dirs, get_client_secrets_path, get_credentials_dir
from core.runtime_installer import AppRuntimeInstaller
from ui.mpc_preview import MPCBEPreviewHost
from ui.worker_threads import AppWorkflowState, WorkerAction, create_worker_thread

LOGGER = logging.getLogger(__name__)

STATE_LABELS = {
    AppWorkflowState.IDLE: "대기",
    AppWorkflowState.PROCESSING: "처리 중",
    AppWorkflowState.READY_TO_UPLOAD: "업로드 준비",
    AppWorkflowState.AUTHENTICATING: "인증 중",
    AppWorkflowState.UPLOADING: "업로드 중",
    AppWorkflowState.DONE: "완료",
    AppWorkflowState.ERROR: "오류",
    AppWorkflowState.CANCELLED: "취소됨",
}

STAGE_LABELS = {
    "VALIDATING": "검증 중",
    "SYNCING": "오디오 싱크 적용 중",
    "REMUXING": "클립 추출 중",
    "THUMBNAIL": "썸네일 추출 중",
    "EXPORTING": "메타데이터 저장 중",
    "CLEANUP": "정리 중",
    "INSTALLING_RUNTIME": "도구 설치 중",
    "AUTHENTICATING": "구글 인증 중",
    "UPLOADING": "유튜브 업로드 중",
    "DONE": "완료",
}

CLIP_TABLE_NAME = 0
CLIP_TABLE_START = 1
CLIP_TABLE_END = 2
CLIP_TABLE_THUMB = 3
CLIP_TABLE_TITLE = 4
CLIP_TABLE_UPLOAD = 5


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        ensure_runtime_dirs()
        self.data_manager = DataManager()
        self.catalog_store = CatalogStore()
        self.mpc_be_controller = MPCBEController()
        self.runtime_installer = AppRuntimeInstaller(mpc_be_controller=self.mpc_be_controller)
        self.clip_drafts: list[ClipDraft] = []
        self._active_thread = None
        self._active_worker = None
        self._last_bundle: ExportBundle | None = None
        self._syncing_clip_table = False
        self._syncing_clip_details = False

        self.setWindowTitle("YTUploader")
        self.resize(1400, 960)

        self._build_ui()
        self._refresh_runtime_statuses()
        self._load_settings_into_ui()
        self._refresh_recordings()
        self._ensure_default_clip()
        self.set_state(AppWorkflowState.IDLE)

    def _build_ui(self) -> None:
        central_widget = QWidget(self)
        root_layout = QVBoxLayout(central_widget)

        self.tabs = QTabWidget(central_widget)
        self.tabs.addTab(self._build_source_tab(), "1. 소스")
        self.tabs.addTab(self._build_clips_tab(), "2. 클립")
        self.tabs.addTab(self._build_metadata_tab(), "3. 메타데이터")
        self.tabs.addTab(self._build_run_tab(), "4. 실행")

        root_layout.addWidget(self.tabs)
        self.setCentralWidget(central_widget)

    def _build_source_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)

        settings_group = QGroupBox("OBS 소스", tab)
        settings_layout = QFormLayout(settings_group)
        obs_row = QHBoxLayout()
        self.obs_source_dir_edit = QLineEdit(settings_group)
        self.obs_source_dir_browse_button = QPushButton("찾아보기", settings_group)
        self.obs_source_dir_save_button = QPushButton("저장", settings_group)
        self.refresh_recordings_button = QPushButton("새로고침", settings_group)
        obs_row.addWidget(self.obs_source_dir_edit)
        obs_row.addWidget(self.obs_source_dir_browse_button)
        obs_row.addWidget(self.obs_source_dir_save_button)
        obs_row.addWidget(self.refresh_recordings_button)
        settings_layout.addRow("OBS 폴더", obs_row)

        files_group = QGroupBox("녹화 목록", tab)
        files_layout = QVBoxLayout(files_group)
        self.recordings_list = QListWidget(files_group)
        self.recordings_list.setAlternatingRowColors(True)
        files_layout.addWidget(self.recordings_list)

        input_row = QHBoxLayout()
        self.input_path_edit = QLineEdit(files_group)
        self.input_browse_button = QPushButton("MKV 선택", files_group)
        input_row.addWidget(self.input_path_edit)
        input_row.addWidget(self.input_browse_button)
        files_layout.addLayout(input_row)

        workflow_group = QGroupBox("소스 옵션", tab)
        workflow_layout = QFormLayout(workflow_group)
        output_row = QHBoxLayout()
        self.output_dir_edit = QLineEdit(workflow_group)
        self.output_dir_browse_button = QPushButton("찾아보기", workflow_group)
        output_row.addWidget(self.output_dir_edit)
        output_row.addWidget(self.output_dir_browse_button)
        workflow_layout.addRow("출력 폴더", output_row)

        self.delay_spin = QSpinBox(workflow_group)
        self.delay_spin.setRange(-300000, 300000)
        self.delay_spin.setSingleStep(50)
        workflow_layout.addRow("오디오 지연 (ms)", self.delay_spin)

        preview_row = QHBoxLayout()
        self.preview_button = QPushButton("미리보기 불러오기", workflow_group)
        self.import_mpc_be_button = QPushButton("MPC-BE 설정 가져오기", workflow_group)
        preview_row.addWidget(self.preview_button)
        preview_row.addWidget(self.import_mpc_be_button)
        workflow_layout.addRow("미리보기", preview_row)

        runtime_group = QGroupBox("내장 도구", tab)
        runtime_layout = QFormLayout(runtime_group)

        self.ffmpeg_runtime_status_value = QLabel("-", runtime_group)
        self.install_ffmpeg_button = QPushButton("FFmpeg 설치하기", runtime_group)
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.addWidget(self.ffmpeg_runtime_status_value)
        ffmpeg_row.addStretch(1)
        ffmpeg_row.addWidget(self.install_ffmpeg_button)
        runtime_layout.addRow("FFmpeg / FFprobe", ffmpeg_row)

        self.mkvmerge_runtime_status_value = QLabel("-", runtime_group)
        self.install_mkvmerge_button = QPushButton("MKVMerge 설치하기", runtime_group)
        mkvmerge_row = QHBoxLayout()
        mkvmerge_row.addWidget(self.mkvmerge_runtime_status_value)
        mkvmerge_row.addStretch(1)
        mkvmerge_row.addWidget(self.install_mkvmerge_button)
        runtime_layout.addRow("MKVMerge", mkvmerge_row)

        self.mpc_be_runtime_status_value = QLabel("-", runtime_group)
        self.install_mpc_be_runtime_button = QPushButton("MPC-BE 설치하기", runtime_group)
        mpc_be_row = QHBoxLayout()
        mpc_be_row.addWidget(self.mpc_be_runtime_status_value)
        mpc_be_row.addStretch(1)
        mpc_be_row.addWidget(self.install_mpc_be_runtime_button)
        runtime_layout.addRow("MPC-BE", mpc_be_row)

        self.refresh_runtime_status_button = QPushButton("도구 상태 새로고침", runtime_group)
        runtime_layout.addRow("", self.refresh_runtime_status_button)

        preview_group = QGroupBox("내장 미리보기", tab)
        preview_layout = QVBoxLayout(preview_group)
        self.preview_host = MPCBEPreviewHost(self.mpc_be_controller, preview_group)
        preview_layout.addWidget(self.preview_host)

        preview_status_row = QHBoxLayout()
        self.preview_connection_value = QLabel("연결 안 됨", preview_group)
        self.preview_time_value = QLabel("00:00:00.000", preview_group)
        preview_status_row.addWidget(QLabel("상태", preview_group))
        preview_status_row.addWidget(self.preview_connection_value)
        preview_status_row.addStretch(1)
        preview_status_row.addWidget(QLabel("현재 시점", preview_group))
        preview_status_row.addWidget(self.preview_time_value)
        preview_layout.addLayout(preview_status_row)

        preview_controls_row = QHBoxLayout()
        self.preview_play_pause_button = QPushButton("재생/일시정지", preview_group)
        self.preview_refresh_time_button = QPushButton("현재 시점 읽기", preview_group)
        self.preview_mark_start_button = QPushButton("현재 시점 -> 시작", preview_group)
        self.preview_mark_end_button = QPushButton("현재 시점 -> 끝", preview_group)
        self.preview_mark_thumb_button = QPushButton("현재 시점 -> 썸네일", preview_group)
        preview_controls_row.addWidget(self.preview_play_pause_button)
        preview_controls_row.addWidget(self.preview_refresh_time_button)
        preview_controls_row.addWidget(self.preview_mark_start_button)
        preview_controls_row.addWidget(self.preview_mark_end_button)
        preview_controls_row.addWidget(self.preview_mark_thumb_button)
        preview_layout.addLayout(preview_controls_row)

        chapter_row = QHBoxLayout()
        self.preview_chapter_title_edit = QLineEdit(preview_group)
        self.preview_chapter_title_edit.setPlaceholderText("챕터 제목")
        self.preview_add_chapter_button = QPushButton("현재 시점에 챕터 추가", preview_group)
        chapter_row.addWidget(self.preview_chapter_title_edit)
        chapter_row.addWidget(self.preview_add_chapter_button)
        preview_layout.addLayout(chapter_row)

        layout.addWidget(settings_group)
        layout.addWidget(files_group)
        layout.addWidget(workflow_group)
        layout.addWidget(runtime_group)
        layout.addWidget(preview_group)
        layout.addStretch(1)

        self.obs_source_dir_browse_button.clicked.connect(self._browse_obs_source_dir)
        self.obs_source_dir_save_button.clicked.connect(self._apply_obs_source_dir)
        self.refresh_recordings_button.clicked.connect(self._refresh_recordings)
        self.input_browse_button.clicked.connect(self._choose_input_file)
        self.output_dir_browse_button.clicked.connect(self._choose_output_dir)
        self.preview_button.clicked.connect(self._launch_preview)
        self.import_mpc_be_button.clicked.connect(self._import_mpc_be_settings)
        self.install_ffmpeg_button.clicked.connect(lambda: self._install_runtime_package("ffmpeg"))
        self.install_mkvmerge_button.clicked.connect(lambda: self._install_runtime_package("mkvmerge"))
        self.install_mpc_be_runtime_button.clicked.connect(lambda: self._install_runtime_package("mpc_be"))
        self.refresh_runtime_status_button.clicked.connect(self._refresh_runtime_statuses)
        self.recordings_list.itemDoubleClicked.connect(self._select_recording_item)
        self.preview_play_pause_button.clicked.connect(self.preview_host.play_pause)
        self.preview_refresh_time_button.clicked.connect(self.preview_host.request_current_position)
        self.preview_mark_start_button.clicked.connect(lambda: self._apply_preview_time_to_selected_clip("start"))
        self.preview_mark_end_button.clicked.connect(lambda: self._apply_preview_time_to_selected_clip("end"))
        self.preview_mark_thumb_button.clicked.connect(lambda: self._apply_preview_time_to_selected_clip("thumb"))
        self.preview_add_chapter_button.clicked.connect(self._add_preview_chapter_to_selected_clip)
        self.preview_host.log.connect(self._append_log)
        self.preview_host.error.connect(self._show_error)
        self.preview_host.position_changed.connect(self._on_preview_position_changed)
        self.preview_host.connection_changed.connect(self._on_preview_connection_changed)
        return tab

    def _build_clips_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)

        table_group = QGroupBox("클립 목록", tab)
        table_layout = QVBoxLayout(table_group)

        self.clip_table = QTableWidget(0, 6, table_group)
        self.clip_table.setHorizontalHeaderLabels(["이름", "시작", "끝", "썸네일", "제목", "업로드"])
        self.clip_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.clip_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.clip_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table_layout.addWidget(self.clip_table)

        buttons_row = QHBoxLayout()
        self.add_clip_button = QPushButton("클립 추가", table_group)
        self.remove_clip_button = QPushButton("클립 제거", table_group)
        buttons_row.addWidget(self.add_clip_button)
        buttons_row.addWidget(self.remove_clip_button)
        buttons_row.addStretch(1)
        table_layout.addLayout(buttons_row)

        details_group = QGroupBox("선택한 클립 상세", tab)
        details_layout = QFormLayout(details_group)
        self.clip_upload_checkbox = QCheckBox("이 클립 업로드", details_group)
        self.clip_notes_edit = QPlainTextEdit(details_group)
        self.clip_notes_edit.setPlaceholderText("클립별 메모를 입력하면 설명과 사이드카에 반영됩니다.")
        self.clip_chapters_edit = QPlainTextEdit(details_group)
        self.clip_chapters_edit.setPlaceholderText("00:00 인트로")
        details_layout.addRow("업로드", self.clip_upload_checkbox)
        details_layout.addRow("메모", self.clip_notes_edit)
        details_layout.addRow("챕터", self.clip_chapters_edit)

        layout.addWidget(table_group)
        layout.addWidget(details_group)

        self.add_clip_button.clicked.connect(self._add_clip)
        self.remove_clip_button.clicked.connect(self._remove_selected_clip)
        self.clip_table.itemSelectionChanged.connect(self._on_clip_selection_changed)
        self.clip_table.itemChanged.connect(self._on_clip_table_item_changed)
        self.clip_notes_edit.textChanged.connect(self._store_selected_clip_details)
        self.clip_chapters_edit.textChanged.connect(self._store_selected_clip_details)
        self.clip_upload_checkbox.stateChanged.connect(self._store_selected_clip_details)
        return tab

    def _build_metadata_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)

        metadata_group = QGroupBox("작업 메타데이터", tab)
        form = QFormLayout(metadata_group)

        self.title_prefix_edit = QLineEdit(metadata_group)
        self.title_prefix_edit.setPlaceholderText("[제목 말머리]")
        self.game_edit = QLineEdit(metadata_group)
        self.preset_edit = QLineEdit(metadata_group)
        self.characters_edit = QLineEdit(metadata_group)
        self.build_info_edit = QLineEdit(metadata_group)
        self.tags_edit = QLineEdit(metadata_group)
        self.tags_edit.setPlaceholderText("태그1, 태그2, 태그3")
        self.description_edit = QPlainTextEdit(metadata_group)
        self.description_edit.setPlaceholderText("모든 클립에 공통으로 들어갈 설명 템플릿")
        self.playlist_edit = QLineEdit(metadata_group)
        self.category_edit = QLineEdit(metadata_group)
        self.privacy_combo = QComboBox(metadata_group)
        self.privacy_combo.addItem("비공개", "private")
        self.privacy_combo.addItem("일부 공개", "unlisted")
        self.privacy_combo.addItem("공개", "public")

        form.addRow("제목 말머리", self.title_prefix_edit)
        form.addRow("게임", self.game_edit)
        form.addRow("프리셋", self.preset_edit)
        form.addRow("캐릭터", self.characters_edit)
        form.addRow("세팅 정보", self.build_info_edit)
        form.addRow("태그", self.tags_edit)
        form.addRow("설명 템플릿", self.description_edit)
        form.addRow("재생목록 ID", self.playlist_edit)
        form.addRow("공개 범위", self.privacy_combo)
        form.addRow("카테고리 ID", self.category_edit)

        defaults_row = QHBoxLayout()
        self.load_defaults_button = QPushButton("기본값 불러오기", metadata_group)
        self.save_defaults_button = QPushButton("기본값 저장", metadata_group)
        defaults_row.addWidget(self.load_defaults_button)
        defaults_row.addWidget(self.save_defaults_button)
        defaults_row.addStretch(1)
        form.addRow("기본값", defaults_row)

        layout.addWidget(metadata_group)
        layout.addStretch(1)

        self.load_defaults_button.clicked.connect(self._load_settings_into_ui)
        self.save_defaults_button.clicked.connect(self._save_defaults)
        return tab

    def _build_run_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)

        action_group = QGroupBox("작업", tab)
        action_layout = QHBoxLayout(action_group)
        self.auth_button = QPushButton("구글 로그인", action_group)
        self.process_button = QPushButton("클립 처리", action_group)
        self.upload_button = QPushButton("선택 클립 업로드", action_group)
        self.process_upload_button = QPushButton("처리 후 업로드", action_group)
        self.copy_clipboard_button = QPushButton("선택 메타데이터 복사", action_group)
        self.cancel_button = QPushButton("취소", action_group)

        for button in (
            self.auth_button,
            self.process_button,
            self.upload_button,
            self.process_upload_button,
            self.copy_clipboard_button,
            self.cancel_button,
        ):
            action_layout.addWidget(button)

        status_group = QGroupBox("상태 / 로그", tab)
        status_layout = QFormLayout(status_group)
        self.state_value = QLabel("-", status_group)
        self.stage_value = QLabel("-", status_group)
        self.progress_bar = QProgressBar(status_group)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.log_view = QPlainTextEdit(status_group)
        self.log_view.setReadOnly(True)
        status_layout.addRow("상태", self.state_value)
        status_layout.addRow("단계", self.stage_value)
        status_layout.addRow("진행률", self.progress_bar)
        status_layout.addRow("로그", self.log_view)

        layout.addWidget(action_group)
        layout.addWidget(status_group)

        self.auth_button.clicked.connect(self._authenticate_google)
        self.process_button.clicked.connect(self._process_only)
        self.upload_button.clicked.connect(self._upload_selected)
        self.process_upload_button.clicked.connect(self._process_and_upload)
        self.copy_clipboard_button.clicked.connect(self._copy_selected_clipboard)
        self.cancel_button.clicked.connect(self._cancel_active_job)
        return tab

    def _load_settings_into_ui(self) -> None:
        settings = self.data_manager.load()
        self.title_prefix_edit.setText(settings["title_prefix_template"])
        self.description_edit.setPlainText(settings["description_template"])
        self.tags_edit.setText(", ".join(settings["tags"]))
        self.playlist_edit.setText(settings["playlist_id"])
        self.category_edit.setText(settings["category_id"])
        self.delay_spin.setValue(int(settings["last_delay_ms"]))
        self.obs_source_dir_edit.setText(settings["obs_source_dir"])
        self.output_dir_edit.setText(settings["last_output_dir"])
        if settings["privacy_status"]:
            index = self.privacy_combo.findData(settings["privacy_status"])
            if index >= 0:
                self.privacy_combo.setCurrentIndex(index)

    def _save_defaults(self) -> None:
        settings = self.data_manager.load()
        settings.update(
            {
                "title_prefix_template": self.title_prefix_edit.text().strip(),
                "description_template": self.description_edit.toPlainText(),
                "tags": self._parse_tags(),
                "playlist_id": self.playlist_edit.text().strip(),
                "privacy_status": str(self.privacy_combo.currentData()),
                "category_id": self.category_edit.text().strip() or "22",
                "obs_source_dir": self.obs_source_dir_edit.text().strip(),
                "last_output_dir": self.output_dir_edit.text().strip(),
                "last_delay_ms": self.delay_spin.value(),
            }
        )
        self.data_manager.save(settings)
        self._append_log("현재 기본값을 저장했습니다.")

    def _browse_obs_source_dir(self) -> None:
        start_dir = self.obs_source_dir_edit.text().strip() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "OBS 녹화 폴더 선택", start_dir)
        if not selected:
            return
        self.obs_source_dir_edit.setText(selected)
        self._apply_obs_source_dir()

    def _apply_obs_source_dir(self) -> None:
        path = self.obs_source_dir_edit.text().strip()
        if not path:
            self._show_error("OBS 녹화 폴더를 지정해 주세요.")
            return
        self.data_manager.set_obs_source_dir(path)
        self._refresh_recordings()
        self._append_log(f"OBS 녹화 폴더를 설정했습니다: {path}")

    def _refresh_recordings(self) -> None:
        self.recordings_list.clear()
        for path in self.data_manager.list_recent_obs_recordings(limit=50):
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.recordings_list.addItem(item)

    def _select_recording_item(self, item: QListWidgetItem) -> None:
        path_text = item.data(Qt.ItemDataRole.UserRole)
        if path_text:
            self._set_current_source(Path(path_text))

    def _choose_input_file(self) -> None:
        start_dir = self.obs_source_dir_edit.text().strip() or self.data_manager.load()["last_input_dir"] or str(Path.home())
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "MKV 파일 선택",
            start_dir,
            "MKV 파일 (*.mkv);;모든 파일 (*)",
        )
        if not selected:
            return
        self._set_current_source(Path(selected))

    def _set_current_source(self, input_path: Path) -> None:
        self.input_path_edit.setText(str(input_path))
        self.data_manager.pick_recording(input_path)
        if not self.output_dir_edit.text().strip():
            saved_output_dir = self.data_manager.load()["last_output_dir"]
            self.output_dir_edit.setText(saved_output_dir or str(input_path.parent))
        self._refresh_recordings()
        self._append_log(f"선택한 소스: {input_path.name}")

    def _choose_output_dir(self) -> None:
        start_dir = self.output_dir_edit.text().strip() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "출력 폴더 선택", start_dir)
        if not selected:
            return
        self.output_dir_edit.setText(selected)

    def _launch_preview(self) -> None:
        source_path = self._required_path(self.input_path_edit.text(), "입력 MKV 파일을 선택해 주세요.")
        try:
            selected_clip = self._selected_clip()
            start_time = selected_clip.start_time if selected_clip else None
            self.preview_host.load_media(source_path, start_time=start_time)
        except Exception as exc:
            self._show_error(str(exc))

    def _on_preview_position_changed(self, timecode: str) -> None:
        self.preview_time_value.setText(timecode)

    def _on_preview_connection_changed(self, connected: bool) -> None:
        self.preview_connection_value.setText("연결됨" if connected else "연결 안 됨")

    def _apply_preview_time_to_selected_clip(self, field_name: str) -> None:
        clip = self._selected_clip()
        if clip is None:
            self._show_error("먼저 클립을 선택해 주세요.")
            return

        timecode = self.preview_host.current_timecode.strip()
        if not timecode:
            self._show_error("아직 미리보기 시점을 읽지 못했습니다.")
            return

        if field_name == "start":
            clip.start_time = timecode
        elif field_name == "end":
            clip.end_time = timecode
        elif field_name == "thumb":
            clip.thumbnail_time = timecode
        else:
            raise ValueError(f"지원하지 않는 클립 시점 대상입니다: {field_name}")

        self._sync_clip_table()
        row = self._selected_clip_index()
        if row is not None:
            self.clip_table.selectRow(row)
        field_label = {"start": "시작", "end": "끝", "thumb": "썸네일"}.get(field_name, field_name)
        self._append_log(f"{clip.clip_name}의 {field_label} 시점에 {timecode}를 적용했습니다.")

    def _add_preview_chapter_to_selected_clip(self) -> None:
        clip = self._selected_clip()
        if clip is None:
            self._show_error("먼저 클립을 선택해 주세요.")
            return

        timecode = self.preview_host.current_timecode.strip()
        if not timecode:
            self._show_error("아직 미리보기 시점을 읽지 못했습니다.")
            return

        chapter_title = self.preview_chapter_title_edit.text().strip()
        clip.chapters.append(ChapterMarker(timecode=timecode, title=chapter_title))
        self._load_selected_clip_details()
        self.preview_chapter_title_edit.clear()
        self._append_log(f"{clip.clip_name}에 {timecode} 챕터를 추가했습니다.")

    def _import_mpc_be_settings(self) -> None:
        self._start_worker(action=WorkerAction.IMPORT_MPC_BE)

    def _install_runtime_package(self, package_id: str) -> None:
        self._start_worker(action=WorkerAction.INSTALL_RUNTIME, runtime_package=package_id)

    def _refresh_runtime_statuses(self) -> None:
        statuses = {status.package_id: status for status in self.runtime_installer.list_statuses()}
        self._apply_runtime_status(
            status_text=statuses["ffmpeg"].status_text,
            source_label=statuses["ffmpeg"].source_label,
            status_label=self.ffmpeg_runtime_status_value,
            button=self.install_ffmpeg_button,
            default_button_text="FFmpeg 설치하기",
            enable_button=statuses["ffmpeg"].installed or bool(statuses["ffmpeg"].source_label),
        )
        self._apply_runtime_status(
            status_text=statuses["mkvmerge"].status_text,
            source_label=statuses["mkvmerge"].source_label,
            status_label=self.mkvmerge_runtime_status_value,
            button=self.install_mkvmerge_button,
            default_button_text="MKVMerge 설치하기",
            enable_button=statuses["mkvmerge"].installed or bool(statuses["mkvmerge"].source_label),
        )
        self._apply_runtime_status(
            status_text=statuses["mpc_be"].status_text,
            source_label=statuses["mpc_be"].source_label,
            status_label=self.mpc_be_runtime_status_value,
            button=self.install_mpc_be_runtime_button,
            default_button_text="MPC-BE 설치하기",
            enable_button=statuses["mpc_be"].installed or bool(statuses["mpc_be"].source_label),
        )

    @staticmethod
    def _apply_runtime_status(
        *,
        status_text: str,
        source_label: str,
        status_label: QLabel,
        button: QPushButton,
        default_button_text: str,
        enable_button: bool,
    ) -> None:
        status_label.setText(
            f"{status_text} ({source_label})" if source_label and status_text != "설치됨" else status_text
        )
        button.setEnabled(enable_button)
        button.setText("다시 설치하기" if status_text == "설치됨" else default_button_text)
        if source_label:
            button.setToolTip(f"설치 원본: {source_label}")
        elif not enable_button:
            button.setToolTip("설치 원본을 찾지 못했습니다. bin 폴더 또는 시스템 설치본을 확인해 주세요.")
        else:
            button.setToolTip("")

    def _ensure_default_clip(self) -> None:
        if self.clip_drafts:
            return
        self.clip_drafts.append(self._new_clip_draft(1))
        self._sync_clip_table()
        self.clip_table.selectRow(0)

    def _new_clip_draft(self, index: int) -> ClipDraft:
        return ClipDraft(clip_id=uuid.uuid4().hex, clip_name=f"클립_{index:02d}", upload_enabled=False)

    def _add_clip(self) -> None:
        self._store_selected_clip_details()
        self.clip_drafts.append(self._new_clip_draft(len(self.clip_drafts) + 1))
        self._sync_clip_table()
        self.clip_table.selectRow(len(self.clip_drafts) - 1)

    def _remove_selected_clip(self) -> None:
        row = self._selected_clip_index()
        if row is None:
            return
        self.clip_drafts.pop(row)
        if not self.clip_drafts:
            self.clip_drafts.append(self._new_clip_draft(1))
        self._sync_clip_table()
        self.clip_table.selectRow(min(row, len(self.clip_drafts) - 1))

    def _sync_clip_table(self) -> None:
        self._syncing_clip_table = True
        try:
            self.clip_table.setRowCount(len(self.clip_drafts))
            for row, clip in enumerate(self.clip_drafts):
                self._set_clip_table_item(row, CLIP_TABLE_NAME, clip.clip_name)
                self._set_clip_table_item(row, CLIP_TABLE_START, clip.start_time or "")
                self._set_clip_table_item(row, CLIP_TABLE_END, clip.end_time or "")
                self._set_clip_table_item(row, CLIP_TABLE_THUMB, clip.thumbnail_time or "")
                self._set_clip_table_item(row, CLIP_TABLE_TITLE, clip.custom_title)
                upload_item = self.clip_table.item(row, CLIP_TABLE_UPLOAD)
                if upload_item is None:
                    upload_item = QTableWidgetItem()
                    upload_item.setFlags(upload_item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                    self.clip_table.setItem(row, CLIP_TABLE_UPLOAD, upload_item)
                upload_item.setCheckState(Qt.CheckState.Checked if clip.upload_enabled else Qt.CheckState.Unchecked)
        finally:
            self._syncing_clip_table = False

    def _set_clip_table_item(self, row: int, column: int, text: str) -> None:
        item = self.clip_table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self.clip_table.setItem(row, column, item)
        item.setText(text)

    def _on_clip_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._syncing_clip_table:
            return
        row = item.row()
        if row < 0 or row >= len(self.clip_drafts):
            return
        clip = self.clip_drafts[row]
        if item.column() == CLIP_TABLE_NAME:
            clip.clip_name = item.text().strip()
        elif item.column() == CLIP_TABLE_START:
            clip.start_time = item.text().strip() or None
        elif item.column() == CLIP_TABLE_END:
            clip.end_time = item.text().strip() or None
        elif item.column() == CLIP_TABLE_THUMB:
            clip.thumbnail_time = item.text().strip() or None
        elif item.column() == CLIP_TABLE_TITLE:
            clip.custom_title = item.text().strip()
        elif item.column() == CLIP_TABLE_UPLOAD:
            clip.upload_enabled = item.checkState() == Qt.CheckState.Checked
            self._load_selected_clip_details()

    def _selected_clip_index(self) -> int | None:
        selected_items = self.clip_table.selectionModel().selectedRows()
        if not selected_items:
            return None
        return selected_items[0].row()

    def _selected_clip(self) -> ClipDraft | None:
        row = self._selected_clip_index()
        if row is None or row >= len(self.clip_drafts):
            return None
        return self.clip_drafts[row]

    def _on_clip_selection_changed(self) -> None:
        self._load_selected_clip_details()

    def _load_selected_clip_details(self) -> None:
        clip = self._selected_clip()
        self._syncing_clip_details = True
        try:
            if clip is None:
                self.clip_notes_edit.clear()
                self.clip_chapters_edit.clear()
                self.clip_upload_checkbox.setChecked(False)
                return
            self.clip_notes_edit.setPlainText(clip.custom_notes)
            self.clip_chapters_edit.setPlainText(self._chapters_to_text(clip.chapters))
            self.clip_upload_checkbox.setChecked(clip.upload_enabled)
        finally:
            self._syncing_clip_details = False

    def _store_selected_clip_details(self) -> None:
        if self._syncing_clip_details:
            return
        clip = self._selected_clip()
        if clip is None:
            return
        clip.custom_notes = self.clip_notes_edit.toPlainText().strip()
        clip.chapters = self._parse_chapters(self.clip_chapters_edit.toPlainText())
        clip.upload_enabled = self.clip_upload_checkbox.isChecked()
        row = self._selected_clip_index()
        if row is None:
            return
        upload_item = self.clip_table.item(row, CLIP_TABLE_UPLOAD)
        if upload_item is not None:
            self._syncing_clip_table = True
            try:
                upload_item.setCheckState(Qt.CheckState.Checked if clip.upload_enabled else Qt.CheckState.Unchecked)
            finally:
                self._syncing_clip_table = False

    @staticmethod
    def _parse_chapters(text: str) -> list[ChapterMarker]:
        chapters: list[ChapterMarker] = []
        for line in text.splitlines():
            cleaned = line.strip()
            if not cleaned:
                continue
            if " " in cleaned:
                timecode, title = cleaned.split(" ", 1)
            else:
                timecode, title = cleaned, ""
            chapters.append(ChapterMarker(timecode=timecode.strip(), title=title.strip()))
        return chapters

    @staticmethod
    def _chapters_to_text(chapters: list[ChapterMarker]) -> str:
        return "\n".join(f"{chapter.timecode} {chapter.title}".strip() for chapter in chapters)

    def _authenticate_google(self) -> None:
        if not self._ensure_client_secrets_available():
            return
        self._start_worker(action=WorkerAction.AUTHENTICATE)

    def _process_only(self) -> None:
        try:
            job_draft, output_dir = self._collect_job_and_output_dir()
        except (ValueError, TemplateRenderError) as exc:
            self._show_error(str(exc))
            return
        self._persist_preferences(job_draft, output_dir)
        self._start_worker(action=WorkerAction.PROCESS, job_draft=job_draft, output_dir=output_dir)

    def _upload_selected(self) -> None:
        if not self._ensure_client_secrets_available():
            return
        if self._last_bundle is None:
            self._show_error("먼저 클립 처리를 완료해 주세요.")
            return
        self._start_worker(action=WorkerAction.UPLOAD, export_bundle=self._last_bundle)

    def _process_and_upload(self) -> None:
        if not self._ensure_client_secrets_available():
            return
        try:
            job_draft, output_dir = self._collect_job_and_output_dir()
        except (ValueError, TemplateRenderError) as exc:
            self._show_error(str(exc))
            return
        self._persist_preferences(job_draft, output_dir)
        self._start_worker(action=WorkerAction.PROCESS_AND_UPLOAD, job_draft=job_draft, output_dir=output_dir)

    def _copy_selected_clipboard(self) -> None:
        if self._last_bundle is None:
            self._show_error("아직 복사할 메타데이터가 없습니다.")
            return
        selected_clip = self._selected_clip()
        if selected_clip is None:
            self._show_error("복사할 클립을 선택해 주세요.")
            return
        for clip_export in self._last_bundle.clip_exports:
            if clip_export.clip_id == selected_clip.clip_id:
                QApplication.clipboard().setText(clip_export.clipboard_payload)
                self._append_log(f"{clip_export.clip_name} 메타데이터를 클립보드에 복사했습니다.")
                return
        self._show_error("선택한 클립의 처리된 메타데이터를 찾지 못했습니다.")

    def _collect_job_and_output_dir(self) -> tuple[JobDraft, Path]:
        self._store_selected_clip_details()

        source_path = self._required_path(self.input_path_edit.text(), "입력 MKV 파일을 선택해 주세요.")
        if not source_path.exists():
            raise ValueError(f"입력 MKV 파일이 존재하지 않습니다: {source_path}")

        output_dir_text = self.output_dir_edit.text().strip()
        output_dir = Path(output_dir_text) if output_dir_text else source_path.parent / "exports"

        title_prefix = render_template(self.title_prefix_edit.text().strip(), source=source_path)
        description_template = render_template(self.description_edit.toPlainText(), source=source_path)

        clips: list[ClipDraft] = []
        for clip in self.clip_drafts:
            clip_name = clip.clip_name.strip()
            if not clip_name:
                raise ValueError("모든 클립에는 이름이 있어야 합니다.")
            if clip.upload_enabled and not clip.custom_title.strip():
                raise ValueError(f"클립 '{clip_name}'은 업로드 전에 수동 제목을 입력해야 합니다.")
            clips.append(
                ClipDraft(
                    clip_id=clip.clip_id,
                    clip_name=clip_name,
                    start_time=clip.start_time,
                    end_time=clip.end_time,
                    thumbnail_time=clip.thumbnail_time,
                    custom_title=clip.custom_title.strip(),
                    custom_notes=clip.custom_notes.strip(),
                    upload_enabled=clip.upload_enabled,
                    chapters=list(clip.chapters),
                )
            )

        if not clips:
            raise ValueError("처리할 클립을 하나 이상 추가해 주세요.")

        job_draft = JobDraft(
            job_id=uuid.uuid4().hex,
            source_path=source_path,
            obs_source_dir=self._path_or_none(self.obs_source_dir_edit.text()),
            delay_ms=self.delay_spin.value(),
            game=self.game_edit.text().strip(),
            preset=self.preset_edit.text().strip(),
            characters=self.characters_edit.text().strip(),
            build_info=self.build_info_edit.text().strip(),
            tags=self._parse_tags(),
            title_prefix=title_prefix,
            description_template=description_template,
            playlist_id=self.playlist_edit.text().strip(),
            privacy_status=str(self.privacy_combo.currentData()),
            category_id=self.category_edit.text().strip() or "22",
            clips=clips,
        )
        return job_draft, output_dir

    def _persist_preferences(self, job_draft: JobDraft, output_dir: Path) -> None:
        self._save_defaults()
        if job_draft.obs_source_dir:
            self.data_manager.set_obs_source_dir(job_draft.obs_source_dir)
        self.data_manager.update_recent_paths(
            input_path=job_draft.source_path,
            output_path=output_dir / "placeholder.mp4",
            delay_ms=job_draft.delay_ms,
        )

    def _cancel_active_job(self) -> None:
        if self._active_worker is None:
            return
        self._active_worker.cancel()
        self._append_log("취소를 요청했습니다.")

    def _start_worker(
        self,
        *,
        action: WorkerAction,
        job_draft: JobDraft | None = None,
        output_dir: Path | None = None,
        export_bundle: ExportBundle | None = None,
        runtime_package: str | None = None,
    ) -> None:
        if self._active_thread is not None:
            self._show_error("이미 다른 작업이 실행 중입니다.")
            return
        thread, worker = create_worker_thread(
            action=action,
            job_draft=job_draft,
            output_dir=output_dir,
            export_bundle=export_bundle,
            runtime_package=runtime_package,
        )
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
        else:
            self.set_state(AppWorkflowState.PROCESSING)

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        thread.start()

    def _on_stage_changed(self, stage: str) -> None:
        self.stage_value.setText(STAGE_LABELS.get(stage, stage))
        if stage in {"VALIDATING", "SYNCING", "REMUXING", "THUMBNAIL", "EXPORTING", "CLEANUP", "INSTALLING_RUNTIME"}:
            self.set_state(AppWorkflowState.PROCESSING)
            self.progress_bar.setRange(0, 0)
        elif stage == "AUTHENTICATING":
            self.set_state(AppWorkflowState.AUTHENTICATING)
            self.progress_bar.setRange(0, 0)
        elif stage == "UPLOADING":
            self.set_state(AppWorkflowState.UPLOADING)
            self.progress_bar.setRange(0, 100)
        elif stage == "DONE":
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

    def _on_worker_completed(self, payload: object) -> None:
        if isinstance(payload, ExportBundle):
            self._last_bundle = payload
            self.set_state(AppWorkflowState.READY_TO_UPLOAD)
            self._append_log(f"클립 {len(payload.clip_exports)}개 처리를 완료했습니다.")
            return

        if not isinstance(payload, dict):
            return

        if payload.get("status") == "cancelled":
            self.set_state(AppWorkflowState.CANCELLED)
            self._append_log(payload.get("message", "작업이 취소되었습니다."))
            return

        action = payload.get("action")
        if action == WorkerAction.AUTHENTICATE.value:
            self.set_state(AppWorkflowState.DONE)
            self._append_log("구글 로그인을 완료했습니다.")
            QMessageBox.information(self, "구글 로그인", "구글 인증이 완료되었습니다.")
            return

        if action == WorkerAction.IMPORT_MPC_BE.value:
            source = payload.get("source")
            label = getattr(source, "label", None) if source else None
            message = (
                f"MPC-BE 설정을 가져왔습니다: {label}"
                if label
                else "가져올 MPC-BE 설정을 찾지 못했습니다."
            )
            self._append_log(message)
            QMessageBox.information(self, "MPC-BE 설정", message)
            self.set_state(AppWorkflowState.IDLE)
            return

        if action == WorkerAction.INSTALL_RUNTIME.value:
            package_label = str(payload.get("package_label", "내장 도구"))
            message = str(payload.get("message", f"{package_label} 설치를 완료했습니다."))
            self._refresh_runtime_statuses()
            self._append_log(message)
            QMessageBox.information(self, f"{package_label} 설치", message)
            self.set_state(AppWorkflowState.IDLE)
            return

        bundle = payload.get("bundle")
        if isinstance(bundle, ExportBundle):
            self._last_bundle = bundle

        if action in {WorkerAction.UPLOAD.value, WorkerAction.PROCESS_AND_UPLOAD.value}:
            self.set_state(AppWorkflowState.DONE)
            results = payload.get("results", [])
            urls = "\n".join(result.get("url", "") for result in results if result.get("url"))
            self._append_log(f"클립 {len(results)}개 업로드를 완료했습니다.")
            if urls:
                QMessageBox.information(self, "업로드 완료", urls)
            return

        self.set_state(AppWorkflowState.DONE)

    def _on_worker_finished(self) -> None:
        self._refresh_runtime_statuses()
        self._append_log("백그라운드 작업이 종료되었습니다.")

    def _on_thread_finished(self) -> None:
        self._active_worker = None
        self._active_thread = None

    def set_state(self, state: AppWorkflowState) -> None:
        self.state_value.setText(STATE_LABELS.get(state, state.value))
        busy = state in {
            AppWorkflowState.PROCESSING,
            AppWorkflowState.AUTHENTICATING,
            AppWorkflowState.UPLOADING,
        }
        controls = [
            self.obs_source_dir_edit,
            self.obs_source_dir_browse_button,
            self.obs_source_dir_save_button,
            self.refresh_recordings_button,
            self.input_path_edit,
            self.input_browse_button,
            self.output_dir_edit,
            self.output_dir_browse_button,
            self.delay_spin,
            self.preview_button,
            self.import_mpc_be_button,
            self.install_ffmpeg_button,
            self.install_mkvmerge_button,
            self.install_mpc_be_runtime_button,
            self.refresh_runtime_status_button,
            self.preview_host,
            self.preview_play_pause_button,
            self.preview_refresh_time_button,
            self.preview_mark_start_button,
            self.preview_mark_end_button,
            self.preview_mark_thumb_button,
            self.preview_chapter_title_edit,
            self.preview_add_chapter_button,
            self.add_clip_button,
            self.remove_clip_button,
            self.clip_table,
            self.clip_notes_edit,
            self.clip_chapters_edit,
            self.clip_upload_checkbox,
            self.title_prefix_edit,
            self.game_edit,
            self.preset_edit,
            self.characters_edit,
            self.build_info_edit,
            self.tags_edit,
            self.description_edit,
            self.playlist_edit,
            self.category_edit,
            self.privacy_combo,
            self.save_defaults_button,
            self.load_defaults_button,
            self.auth_button,
            self.process_button,
            self.upload_button,
            self.process_upload_button,
            self.copy_clipboard_button,
        ]
        for control in controls:
            control.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)

    def _ensure_client_secrets_available(self) -> bool:
        client_secrets_path = get_client_secrets_path()
        if client_secrets_path.exists():
            return True

        dialog = QMessageBox(self)
        dialog.setWindowTitle("구글 OAuth 설정")
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setText("client_secrets.json 파일이 없습니다.")
        dialog.setInformativeText(
            f"구글 OAuth 데스크톱 앱 자격 증명을 아래 폴더에 넣어 주세요:\n{get_credentials_dir()}"
        )
        open_button = dialog.addButton("폴더 열기", QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton(QMessageBox.StandardButton.Close)
        dialog.exec()
        if dialog.clickedButton() is open_button:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(get_credentials_dir())))
        return False

    def _parse_tags(self) -> list[str]:
        return [tag.strip() for tag in self.tags_edit.text().split(",") if tag.strip()]

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
        QMessageBox.critical(self, "오류", message)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.preview_host.shutdown()
        super().closeEvent(event)

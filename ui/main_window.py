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
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.data_manager import DataManager, TemplateRenderError, render_template
from core.job_store import JobStore
from core.metadata_exporter import build_clipboard_payload, read_sidecar
from core.models import (
    JOB_STAGE_DONE,
    JOB_STAGE_METADATA,
    JOB_STAGE_SEGMENTS,
    JOB_STAGE_SELECT,
    JOB_STAGE_SYNC,
    JOB_STAGE_UPLOAD,
    ChapterMarker,
    ClipDraft,
    JobState,
    SegmentState,
)
from core.mpc_be import MPCBEController
from core.paths import (
    ensure_runtime_dirs,
    get_client_secrets_path,
    get_credentials_dir,
    get_job_artifacts_dir,
)
from core.runtime_installer import AppRuntimeInstaller
from core.video_processor import parse_timecode
from core.workflow import WorkflowRunner
from ui.mpc_preview import MPCBEPreviewHost
from ui.worker_threads import AppWorkflowState, WorkerAction, create_worker_thread

LOGGER = logging.getLogger(__name__)

STEP_TITLES = [
    "파일 선택",
    "사운드 싱크",
    "세그먼트 분할",
    "세그먼트 메타데이터",
    "업로드 / 정리",
]

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
    "REMUXING": "세그먼트 분할 / remux 중",
    "THUMBNAIL": "썸네일 생성 중",
    "CLEANUP": "정리 중",
    "INSTALLING_RUNTIME": "도구 설치 중",
    "AUTHENTICATING": "구글 인증 중",
    "UPLOADING": "유튜브 업로드 중",
    "DONE": "완료",
}

DRAFT_TABLE_NAME = 0
DRAFT_TABLE_START = 1
DRAFT_TABLE_END = 2
DRAFT_TABLE_UPLOAD = 3


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        ensure_runtime_dirs()
        self.data_manager = DataManager()
        self.job_store = JobStore()
        self.workflow_runner = WorkflowRunner()
        self.mpc_be_controller = MPCBEController()
        self.runtime_installer = AppRuntimeInstaller(mpc_be_controller=self.mpc_be_controller)

        self.current_job: JobState | None = None
        self.completed_job_snapshot: JobState | None = None
        self._metadata_segment_index = 0
        self._active_thread = None
        self._active_worker = None
        self._busy_controls: list[QWidget] = []
        self._preview_expanded = False
        self._syncing_step_selection = False
        self._syncing_draft_table = False
        self._syncing_draft_details = False
        self._syncing_upload_results = False

        self.setWindowTitle("YTUploader")
        self.resize(1640, 980)

        self._build_ui()
        self._load_settings_into_ui()
        self._refresh_runtime_statuses()
        self._refresh_recordings()
        self._refresh_jobs_list()
        self._restore_latest_job()
        self.set_state(AppWorkflowState.IDLE)
        self._refresh_workflow_shell()

    def _build_ui(self) -> None:
        central_widget = QWidget(self)
        root_layout = QVBoxLayout(central_widget)

        summary_group = QGroupBox("현재 파이프라인", central_widget)
        summary_layout = QVBoxLayout(summary_group)
        self.workflow_heading_value = QLabel(summary_group)
        self.workflow_heading_value.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.workflow_summary_value = QLabel(summary_group)
        self.workflow_summary_value.setWordWrap(True)
        summary_layout.addWidget(self.workflow_heading_value)
        summary_layout.addWidget(self.workflow_summary_value)
        root_layout.addWidget(summary_group)

        body_layout = QHBoxLayout()

        navigation_group = QGroupBox("단계", central_widget)
        navigation_layout = QVBoxLayout(navigation_group)
        self.step_list = QListWidget(navigation_group)
        self.step_list.setFixedWidth(220)
        self.step_list.setAlternatingRowColors(True)
        for index, title in enumerate(STEP_TITLES, start=1):
            self.step_list.addItem(f"{index}. {title}")
        navigation_layout.addWidget(self.step_list)
        body_layout.addWidget(navigation_group)

        content_layout = QVBoxLayout()
        self.content_splitter = QSplitter(Qt.Orientation.Horizontal, central_widget)
        self.preview_container = self._build_preview_panel()
        self.step_stack = QStackedWidget(central_widget)
        self.step_stack.addWidget(self._build_source_step())
        self.step_stack.addWidget(self._build_sync_step())
        self.step_stack.addWidget(self._build_segments_step())
        self.step_stack.addWidget(self._build_metadata_step())
        self.step_stack.addWidget(self._build_upload_step())
        self.content_splitter.addWidget(self.preview_container)
        self.content_splitter.addWidget(self.step_stack)
        self.content_splitter.setStretchFactor(0, 3)
        self.content_splitter.setStretchFactor(1, 2)
        self.content_splitter.setSizes([980, 520])
        content_layout.addWidget(self.content_splitter)

        navigation_buttons_row = QHBoxLayout()
        self.previous_step_button = QPushButton("이전 단계", central_widget)
        self.next_step_button = QPushButton("다음 단계", central_widget)
        navigation_buttons_row.addStretch(1)
        navigation_buttons_row.addWidget(self.previous_step_button)
        navigation_buttons_row.addWidget(self.next_step_button)
        content_layout.addLayout(navigation_buttons_row)

        body_layout.addLayout(content_layout, stretch=1)
        root_layout.addLayout(body_layout, stretch=1)

        status_group = QGroupBox("백그라운드 상태 / 로그", central_widget)
        status_layout = QFormLayout(status_group)
        self.state_value = QLabel("-", status_group)
        self.stage_value = QLabel("-", status_group)
        self.progress_bar = QProgressBar(status_group)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.cancel_button = QPushButton("취소", status_group)
        self.log_view = QPlainTextEdit(status_group)
        self.log_view.setReadOnly(True)
        status_layout.addRow("상태", self.state_value)
        status_layout.addRow("단계", self.stage_value)
        status_layout.addRow("진행률", self.progress_bar)
        status_layout.addRow("작업 제어", self.cancel_button)
        status_layout.addRow("로그", self.log_view)
        root_layout.addWidget(status_group, stretch=1)

        self.setCentralWidget(central_widget)

        self.step_list.currentRowChanged.connect(self._on_step_selected)
        self.previous_step_button.clicked.connect(lambda: self._go_to_step(self._current_step_index() - 1))
        self.next_step_button.clicked.connect(lambda: self._go_to_step(self._current_step_index() + 1))
        self.cancel_button.clicked.connect(self._cancel_active_job)
        self.preview_host.log.connect(self._append_log)
        self.preview_host.error.connect(self._show_error)
        self.preview_host.position_changed.connect(self._on_preview_position_changed)
        self.preview_host.connection_changed.connect(self._on_preview_connection_changed)
        self.preview_host.double_clicked.connect(self._toggle_preview_expanded)
        self.preview_play_pause_button.clicked.connect(self.preview_host.play_pause)
        self.preview_refresh_time_button.clicked.connect(self.preview_host.request_current_position)
        self.preview_expand_button.clicked.connect(self._toggle_preview_expanded)
        self._sync_step_selection()

    def _build_preview_panel(self) -> QWidget:
        container = QGroupBox("MPC-BE 미리보기", self)
        layout = QVBoxLayout(container)

        header_row = QHBoxLayout()
        self.preview_media_value = QLabel("불러온 미디어 없음", container)
        self.preview_connection_value = QLabel("연결 안 됨", container)
        self.preview_time_value = QLabel("00:00:00.000", container)
        self.preview_expand_button = QPushButton("확장", container)
        header_row.addWidget(self.preview_media_value, stretch=1)
        header_row.addWidget(QLabel("상태", container))
        header_row.addWidget(self.preview_connection_value)
        header_row.addSpacing(12)
        header_row.addWidget(QLabel("현재 시점", container))
        header_row.addWidget(self.preview_time_value)
        header_row.addSpacing(12)
        header_row.addWidget(self.preview_expand_button)

        self.preview_host = MPCBEPreviewHost(self.mpc_be_controller, container)

        controls_row = QHBoxLayout()
        self.preview_play_pause_button = QPushButton("재생/일시정지", container)
        self.preview_refresh_time_button = QPushButton("현재 시점 읽기", container)
        controls_row.addWidget(self.preview_play_pause_button)
        controls_row.addWidget(self.preview_refresh_time_button)
        controls_row.addStretch(1)

        hint_label = QLabel(
            "미리보기 단계에서는 이 영역을 크게 사용합니다. 내부 확장은 버튼이나 컨테이너 더블클릭으로 토글할 수 있습니다.",
            container,
        )
        hint_label.setWordWrap(True)

        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addLayout(header_row)
        layout.addWidget(self.preview_host, stretch=1)
        layout.addLayout(controls_row)
        layout.addWidget(hint_label)
        return container

    def _build_source_step(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        active_jobs_group = QGroupBox("미완료 job", page)
        active_jobs_layout = QVBoxLayout(active_jobs_group)
        self.jobs_list = QListWidget(active_jobs_group)
        self.jobs_list.setAlternatingRowColors(True)
        jobs_button_row = QHBoxLayout()
        self.load_job_button = QPushButton("선택한 job 불러오기", active_jobs_group)
        jobs_button_row.addWidget(self.load_job_button)
        jobs_button_row.addStretch(1)
        active_jobs_layout.addWidget(self.jobs_list)
        active_jobs_layout.addLayout(jobs_button_row)

        source_group = QGroupBox("작업 대상 파일", page)
        source_layout = QFormLayout(source_group)
        obs_row = QHBoxLayout()
        self.obs_source_dir_edit = QLineEdit(source_group)
        self.obs_source_dir_browse_button = QPushButton("찾아보기", source_group)
        self.obs_source_dir_save_button = QPushButton("저장", source_group)
        self.refresh_recordings_button = QPushButton("새로고침", source_group)
        obs_row.addWidget(self.obs_source_dir_edit)
        obs_row.addWidget(self.obs_source_dir_browse_button)
        obs_row.addWidget(self.obs_source_dir_save_button)
        obs_row.addWidget(self.refresh_recordings_button)
        source_layout.addRow("OBS 폴더", obs_row)

        self.recordings_list = QListWidget(source_group)
        self.recordings_list.setAlternatingRowColors(True)
        source_layout.addRow("최근 녹화", self.recordings_list)

        input_row = QHBoxLayout()
        self.input_path_edit = QLineEdit(source_group)
        self.input_browse_button = QPushButton("MKV 선택", source_group)
        input_row.addWidget(self.input_path_edit)
        input_row.addWidget(self.input_browse_button)
        source_layout.addRow("입력 파일", input_row)

        output_row = QHBoxLayout()
        self.output_dir_edit = QLineEdit(source_group)
        self.output_dir_browse_button = QPushButton("찾아보기", source_group)
        output_row.addWidget(self.output_dir_edit)
        output_row.addWidget(self.output_dir_browse_button)
        source_layout.addRow("출력 폴더", output_row)

        self.current_job_value = QLabel("선택된 job 없음", source_group)
        self.current_job_value.setWordWrap(True)
        source_layout.addRow("현재 job", self.current_job_value)

        create_job_row = QHBoxLayout()
        self.create_job_button = QPushButton("현재 파일로 새 job 시작", source_group)
        create_job_row.addWidget(self.create_job_button)
        create_job_row.addStretch(1)
        source_layout.addRow("", create_job_row)

        runtime_group = QGroupBox("필수 도구 / MPC-BE 설정", page)
        runtime_layout = QFormLayout(runtime_group)

        self.losslesscut_runtime_status_value = QLabel("-", runtime_group)
        self.install_losslesscut_button = QPushButton("LosslessCut 설치하기", runtime_group)
        losslesscut_row = QHBoxLayout()
        losslesscut_row.addWidget(self.losslesscut_runtime_status_value)
        losslesscut_row.addStretch(1)
        losslesscut_row.addWidget(self.install_losslesscut_button)
        runtime_layout.addRow("LosslessCut", losslesscut_row)

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

        mpc_settings_row = QHBoxLayout()
        self.import_mpc_be_button = QPushButton("MPC-BE 설정 가져오기", runtime_group)
        self.refresh_runtime_status_button = QPushButton("도구 상태 새로고침", runtime_group)
        mpc_settings_row.addWidget(self.import_mpc_be_button)
        mpc_settings_row.addWidget(self.refresh_runtime_status_button)
        mpc_settings_row.addStretch(1)
        runtime_layout.addRow("", mpc_settings_row)

        layout.addWidget(active_jobs_group)
        layout.addWidget(source_group)
        layout.addWidget(runtime_group)
        layout.addStretch(1)

        self.jobs_list.itemDoubleClicked.connect(self._load_selected_job_from_list)
        self.load_job_button.clicked.connect(self._load_selected_job_from_list)
        self.obs_source_dir_browse_button.clicked.connect(self._browse_obs_source_dir)
        self.obs_source_dir_save_button.clicked.connect(self._apply_obs_source_dir)
        self.refresh_recordings_button.clicked.connect(self._refresh_recordings)
        self.recordings_list.itemDoubleClicked.connect(self._select_recording_item)
        self.input_browse_button.clicked.connect(self._choose_input_file)
        self.output_dir_browse_button.clicked.connect(self._choose_output_dir)
        self.create_job_button.clicked.connect(self._create_job_from_source)
        self.install_losslesscut_button.clicked.connect(lambda: self._install_runtime_package("losslesscut"))
        self.install_mkvmerge_button.clicked.connect(lambda: self._install_runtime_package("mkvmerge"))
        self.install_mpc_be_runtime_button.clicked.connect(lambda: self._install_runtime_package("mpc_be"))
        self.import_mpc_be_button.clicked.connect(self._import_mpc_be_settings)
        self.refresh_runtime_status_button.clicked.connect(self._refresh_runtime_statuses)
        self._register_busy_controls(
            self.load_job_button,
            self.obs_source_dir_edit,
            self.obs_source_dir_browse_button,
            self.obs_source_dir_save_button,
            self.refresh_recordings_button,
            self.jobs_list,
            self.recordings_list,
            self.input_path_edit,
            self.input_browse_button,
            self.output_dir_edit,
            self.output_dir_browse_button,
            self.create_job_button,
            self.install_losslesscut_button,
            self.install_mkvmerge_button,
            self.install_mpc_be_runtime_button,
            self.import_mpc_be_button,
            self.refresh_runtime_status_button,
        )
        return page

    def _build_sync_step(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        summary_group = QGroupBox("싱크 점검", page)
        summary_layout = QVBoxLayout(summary_group)
        self.sync_summary_value = QLabel(summary_group)
        self.sync_summary_value.setWordWrap(True)
        summary_layout.addWidget(self.sync_summary_value)

        controls_group = QGroupBox("싱크 적용", page)
        controls_layout = QFormLayout(controls_group)
        self.delay_spin = QSpinBox(controls_group)
        self.delay_spin.setRange(-300000, 300000)
        self.delay_spin.setSingleStep(50)
        controls_layout.addRow("오디오 지연 (ms)", self.delay_spin)

        preview_row = QHBoxLayout()
        self.sync_preview_source_button = QPushButton("원본 미리보기", controls_group)
        self.sync_preview_synced_button = QPushButton("동기화 결과 미리보기", controls_group)
        preview_row.addWidget(self.sync_preview_source_button)
        preview_row.addWidget(self.sync_preview_synced_button)
        preview_row.addStretch(1)
        controls_layout.addRow("미리보기", preview_row)

        apply_row = QHBoxLayout()
        self.apply_sync_button = QPushButton("현재 delay로 싱크 적용", controls_group)
        apply_row.addWidget(self.apply_sync_button)
        apply_row.addStretch(1)
        controls_layout.addRow("", apply_row)

        self.synced_source_value = QLabel("동기화 결과 없음", controls_group)
        self.synced_source_value.setWordWrap(True)
        controls_layout.addRow("결과 파일", self.synced_source_value)

        layout.addWidget(summary_group)
        layout.addWidget(controls_group)
        layout.addStretch(1)

        self.sync_preview_source_button.clicked.connect(self._load_source_preview_for_sync)
        self.sync_preview_synced_button.clicked.connect(self._load_synced_preview)
        self.apply_sync_button.clicked.connect(self._apply_sync_for_current_job)
        self.delay_spin.valueChanged.connect(self._on_delay_changed)
        self._register_busy_controls(
            self.delay_spin,
            self.sync_preview_source_button,
            self.sync_preview_synced_button,
            self.apply_sync_button,
        )
        return page

    def _build_segments_step(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        self.segment_stage_summary_value = QLabel(page)
        self.segment_stage_summary_value.setWordWrap(True)
        layout.addWidget(self.segment_stage_summary_value)

        table_group = QGroupBox("세그먼트 초안", page)
        table_layout = QVBoxLayout(table_group)
        self.segment_table = QTableWidget(0, 4, table_group)
        self.segment_table.setHorizontalHeaderLabels(["이름", "시작", "끝", "업로드"])
        self.segment_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.segment_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.segment_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table_layout.addWidget(self.segment_table)

        buttons_row = QHBoxLayout()
        self.add_segment_button = QPushButton("세그먼트 추가", table_group)
        self.remove_segment_button = QPushButton("세그먼트 제거", table_group)
        self.segment_preview_button = QPushButton("현재 세그먼트 기준 미리보기", table_group)
        self.mark_segment_start_button = QPushButton("현재 시점 -> 시작", table_group)
        self.mark_segment_end_button = QPushButton("현재 시점 -> 끝", table_group)
        buttons_row.addWidget(self.add_segment_button)
        buttons_row.addWidget(self.remove_segment_button)
        buttons_row.addWidget(self.segment_preview_button)
        buttons_row.addWidget(self.mark_segment_start_button)
        buttons_row.addWidget(self.mark_segment_end_button)
        buttons_row.addStretch(1)
        table_layout.addLayout(buttons_row)

        detail_group = QGroupBox("선택한 세그먼트", page)
        detail_layout = QFormLayout(detail_group)
        self.segment_name_edit = QLineEdit(detail_group)
        self.segment_start_edit = QLineEdit(detail_group)
        self.segment_end_edit = QLineEdit(detail_group)
        self.segment_upload_checkbox = QCheckBox("업로드 대상", detail_group)
        detail_layout.addRow("이름", self.segment_name_edit)
        detail_layout.addRow("시작 시점", self.segment_start_edit)
        detail_layout.addRow("끝 시점", self.segment_end_edit)
        detail_layout.addRow("업로드", self.segment_upload_checkbox)

        split_row = QHBoxLayout()
        self.split_segments_button = QPushButton("현재 세그먼트 초안으로 분할 실행", page)
        split_row.addWidget(self.split_segments_button)
        split_row.addStretch(1)

        layout.addWidget(table_group, stretch=1)
        layout.addWidget(detail_group)
        layout.addLayout(split_row)

        self.add_segment_button.clicked.connect(self._add_segment_draft)
        self.remove_segment_button.clicked.connect(self._remove_selected_segment_draft)
        self.segment_preview_button.clicked.connect(self._load_selected_draft_preview)
        self.mark_segment_start_button.clicked.connect(lambda: self._apply_preview_time_to_selected_draft("start"))
        self.mark_segment_end_button.clicked.connect(lambda: self._apply_preview_time_to_selected_draft("end"))
        self.segment_table.itemSelectionChanged.connect(self._on_segment_selection_changed)
        self.segment_table.itemChanged.connect(self._on_segment_table_item_changed)
        self.segment_name_edit.textChanged.connect(self._store_selected_segment_draft)
        self.segment_start_edit.textChanged.connect(self._store_selected_segment_draft)
        self.segment_end_edit.textChanged.connect(self._store_selected_segment_draft)
        self.segment_upload_checkbox.stateChanged.connect(self._store_selected_segment_draft)
        self.split_segments_button.clicked.connect(self._split_current_job_segments)
        self._register_busy_controls(
            self.segment_table,
            self.add_segment_button,
            self.remove_segment_button,
            self.segment_preview_button,
            self.mark_segment_start_button,
            self.mark_segment_end_button,
            self.segment_name_edit,
            self.segment_start_edit,
            self.segment_end_edit,
            self.segment_upload_checkbox,
            self.split_segments_button,
        )
        return page

    def _build_metadata_step(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        self.metadata_stage_summary_value = QLabel(page)
        self.metadata_stage_summary_value.setWordWrap(True)
        layout.addWidget(self.metadata_stage_summary_value)

        common_group = QGroupBox("job 공통 메타데이터", page)
        common_layout = QFormLayout(common_group)
        self.title_prefix_edit = QLineEdit(common_group)
        self.tags_edit = QLineEdit(common_group)
        self.tags_edit.setPlaceholderText("태그1, 태그2, 태그3")
        self.description_template_edit = QPlainTextEdit(common_group)
        self.description_template_edit.setPlaceholderText("세그먼트 설명 기본 템플릿")
        self.playlist_edit = QLineEdit(common_group)
        self.privacy_combo = QComboBox(common_group)
        self.privacy_combo.addItem("비공개", "private")
        self.privacy_combo.addItem("일부 공개", "unlisted")
        self.privacy_combo.addItem("공개", "public")
        self.game_edit = QLineEdit(common_group)
        self.preset_edit = QLineEdit(common_group)
        self.characters_edit = QLineEdit(common_group)
        self.build_info_edit = QLineEdit(common_group)
        self.default_category_edit = QLineEdit(common_group)
        defaults_row = QHBoxLayout()
        self.load_defaults_button = QPushButton("기본값 불러오기", common_group)
        self.save_defaults_button = QPushButton("기본값 저장", common_group)
        defaults_row.addWidget(self.load_defaults_button)
        defaults_row.addWidget(self.save_defaults_button)
        defaults_row.addStretch(1)
        common_layout.addRow("제목 말머리", self.title_prefix_edit)
        common_layout.addRow("태그", self.tags_edit)
        common_layout.addRow("설명 기본값", self.description_template_edit)
        common_layout.addRow("재생목록 ID", self.playlist_edit)
        common_layout.addRow("공개 범위", self.privacy_combo)
        common_layout.addRow("게임", self.game_edit)
        common_layout.addRow("프리셋", self.preset_edit)
        common_layout.addRow("캐릭터", self.characters_edit)
        common_layout.addRow("세팅 정보", self.build_info_edit)
        common_layout.addRow("기본 카테고리", self.default_category_edit)
        common_layout.addRow("기본값", defaults_row)

        segment_group = QGroupBox("현재 세그먼트", page)
        segment_layout = QFormLayout(segment_group)
        nav_row = QHBoxLayout()
        self.previous_metadata_segment_button = QPushButton("이전 세그먼트", segment_group)
        self.next_metadata_segment_button = QPushButton("다음 세그먼트", segment_group)
        self.metadata_preview_button = QPushButton("현재 세그먼트 미리보기", segment_group)
        nav_row.addWidget(self.previous_metadata_segment_button)
        nav_row.addWidget(self.next_metadata_segment_button)
        nav_row.addWidget(self.metadata_preview_button)
        nav_row.addStretch(1)
        self.current_metadata_segment_value = QLabel("세그먼트 없음", segment_group)
        self.current_metadata_segment_value.setWordWrap(True)
        self.current_segment_title_edit = QLineEdit(segment_group)
        self.current_segment_thumbnail_time_edit = QLineEdit(segment_group)
        self.current_segment_category_edit = QLineEdit(segment_group)
        self.current_segment_upload_checkbox = QCheckBox("업로드 대상", segment_group)
        self.current_segment_notes_edit = QPlainTextEdit(segment_group)
        self.current_segment_description_edit = QPlainTextEdit(segment_group)
        self.current_segment_chapters_edit = QPlainTextEdit(segment_group)
        self.metadata_chapter_title_edit = QLineEdit(segment_group)
        thumb_row = QHBoxLayout()
        self.apply_thumbnail_time_button = QPushButton("현재 시점 -> 썸네일", segment_group)
        thumb_row.addWidget(self.apply_thumbnail_time_button)
        thumb_row.addStretch(1)
        chapter_row = QHBoxLayout()
        self.add_metadata_chapter_button = QPushButton("현재 시점에 챕터 추가", segment_group)
        chapter_row.addWidget(self.metadata_chapter_title_edit)
        chapter_row.addWidget(self.add_metadata_chapter_button)
        save_row = QHBoxLayout()
        self.save_segment_metadata_button = QPushButton("현재 세그먼트 저장", segment_group)
        self.save_next_segment_metadata_button = QPushButton("저장 후 다음", segment_group)
        save_row.addWidget(self.save_segment_metadata_button)
        save_row.addWidget(self.save_next_segment_metadata_button)
        save_row.addStretch(1)
        segment_layout.addRow("탐색", nav_row)
        segment_layout.addRow("선택된 세그먼트", self.current_metadata_segment_value)
        segment_layout.addRow("수동 제목", self.current_segment_title_edit)
        segment_layout.addRow("썸네일 시점", self.current_segment_thumbnail_time_edit)
        segment_layout.addRow("", thumb_row)
        segment_layout.addRow("카테고리 ID", self.current_segment_category_edit)
        segment_layout.addRow("업로드", self.current_segment_upload_checkbox)
        segment_layout.addRow("메모", self.current_segment_notes_edit)
        segment_layout.addRow("최종 설명", self.current_segment_description_edit)
        segment_layout.addRow("챕터", self.current_segment_chapters_edit)
        segment_layout.addRow("새 챕터", chapter_row)
        segment_layout.addRow("", save_row)

        layout.addWidget(common_group)
        layout.addWidget(segment_group, stretch=1)

        self.load_defaults_button.clicked.connect(self._load_settings_into_ui)
        self.save_defaults_button.clicked.connect(self._save_defaults)
        self.previous_metadata_segment_button.clicked.connect(lambda: self._move_metadata_segment(-1))
        self.next_metadata_segment_button.clicked.connect(lambda: self._move_metadata_segment(1))
        self.metadata_preview_button.clicked.connect(self._load_current_segment_preview)
        self.apply_thumbnail_time_button.clicked.connect(self._apply_preview_time_to_current_segment_thumbnail)
        self.add_metadata_chapter_button.clicked.connect(self._add_preview_chapter_to_current_segment)
        self.save_segment_metadata_button.clicked.connect(self._save_current_segment_metadata)
        self.save_next_segment_metadata_button.clicked.connect(lambda: self._save_current_segment_metadata(advance=True))
        self._register_busy_controls(
            self.title_prefix_edit,
            self.tags_edit,
            self.description_template_edit,
            self.playlist_edit,
            self.privacy_combo,
            self.game_edit,
            self.preset_edit,
            self.characters_edit,
            self.build_info_edit,
            self.default_category_edit,
            self.load_defaults_button,
            self.save_defaults_button,
            self.previous_metadata_segment_button,
            self.next_metadata_segment_button,
            self.metadata_preview_button,
            self.current_segment_title_edit,
            self.current_segment_thumbnail_time_edit,
            self.current_segment_category_edit,
            self.current_segment_upload_checkbox,
            self.current_segment_notes_edit,
            self.current_segment_description_edit,
            self.current_segment_chapters_edit,
            self.metadata_chapter_title_edit,
            self.apply_thumbnail_time_button,
            self.add_metadata_chapter_button,
            self.save_segment_metadata_button,
            self.save_next_segment_metadata_button,
        )
        return page

    def _build_upload_step(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        action_group = QGroupBox("업로드 / 정리", page)
        action_layout = QHBoxLayout(action_group)
        self.auth_button = QPushButton("구글 로그인", action_group)
        self.upload_cleanup_button = QPushButton("준비된 세그먼트 업로드 + 정리", action_group)
        self.open_output_folder_button = QPushButton("출력 폴더 열기", action_group)
        self.copy_metadata_button = QPushButton("선택 메타데이터 복사", action_group)
        action_layout.addWidget(self.auth_button)
        action_layout.addWidget(self.upload_cleanup_button)
        action_layout.addWidget(self.open_output_folder_button)
        action_layout.addWidget(self.copy_metadata_button)
        action_layout.addStretch(1)

        self.upload_stage_summary_value = QLabel(page)
        self.upload_stage_summary_value.setWordWrap(True)

        results_group = QGroupBox("세그먼트 업로드 상태", page)
        results_layout = QHBoxLayout(results_group)
        self.upload_results_list = QListWidget(results_group)
        self.upload_results_list.setAlternatingRowColors(True)
        self.upload_result_detail_value = QLabel(results_group)
        self.upload_result_detail_value.setWordWrap(True)
        self.upload_result_detail_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        results_layout.addWidget(self.upload_results_list, stretch=2)
        results_layout.addWidget(self.upload_result_detail_value, stretch=3)

        layout.addWidget(action_group)
        layout.addWidget(self.upload_stage_summary_value)
        layout.addWidget(results_group, stretch=1)

        self.auth_button.clicked.connect(self._authenticate_google)
        self.upload_cleanup_button.clicked.connect(self._upload_and_cleanup_current_job)
        self.open_output_folder_button.clicked.connect(self._open_output_folder)
        self.copy_metadata_button.clicked.connect(self._copy_selected_clipboard)
        self.upload_results_list.itemSelectionChanged.connect(self._on_upload_result_selection_changed)
        self._register_busy_controls(
            self.auth_button,
            self.upload_cleanup_button,
            self.open_output_folder_button,
            self.copy_metadata_button,
            self.upload_results_list,
        )
        return page

    def _register_busy_controls(self, *controls: QWidget) -> None:
        for control in controls:
            if control not in self._busy_controls:
                self._busy_controls.append(control)

    def _load_settings_into_ui(self) -> None:
        settings = self.data_manager.load()
        self.obs_source_dir_edit.setText(settings["obs_source_dir"])
        self.output_dir_edit.setText(settings["last_output_dir"])
        self.delay_spin.setValue(int(settings["last_delay_ms"]))
        self.title_prefix_edit.setText(settings["title_prefix_template"])
        self.tags_edit.setText(", ".join(settings["tags"]))
        self.description_template_edit.setPlainText(settings["description_template"])
        self.playlist_edit.setText(settings["playlist_id"])
        self.default_category_edit.setText(settings["category_id"])
        if settings["privacy_status"]:
            index = self.privacy_combo.findData(settings["privacy_status"])
            if index >= 0:
                self.privacy_combo.setCurrentIndex(index)
        self._refresh_workflow_shell()

    def _save_defaults(self) -> None:
        settings = self.data_manager.load()
        settings.update(
            {
                "title_prefix_template": self.title_prefix_edit.text().strip(),
                "description_template": self.description_template_edit.toPlainText(),
                "tags": self._parse_tags(),
                "playlist_id": self.playlist_edit.text().strip(),
                "privacy_status": str(self.privacy_combo.currentData()),
                "category_id": self.default_category_edit.text().strip() or "22",
                "obs_source_dir": self.obs_source_dir_edit.text().strip(),
                "last_output_dir": self.output_dir_edit.text().strip(),
                "last_delay_ms": self.delay_spin.value(),
            }
        )
        self.data_manager.save(settings)
        self._append_log("현재 공통 메타데이터 기본값을 저장했습니다.")
        self._refresh_workflow_shell()

    def _restore_latest_job(self) -> None:
        latest_job = self.job_store.load_latest_job()
        if latest_job is None:
            return
        self._set_current_job(latest_job)
        self._go_to_step(self._job_stage_to_step_index(latest_job.current_stage), force=True)
        self._append_log(f"미완료 job을 복원했습니다: {latest_job.job_id}")

    def _refresh_jobs_list(self) -> None:
        self.jobs_list.clear()
        for job in self.job_store.list_jobs():
            label = f"[{job.current_stage}] {job.source_path.name}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, job.job_id)
            self.jobs_list.addItem(item)

    def _load_selected_job_from_list(self, item: QListWidgetItem | None = None) -> None:
        selected_item = item or (self.jobs_list.selectedItems()[0] if self.jobs_list.selectedItems() else None)
        if selected_item is None:
            self._show_error("불러올 job을 선택해 주세요.")
            return
        job_id = str(selected_item.data(Qt.ItemDataRole.UserRole) or "").strip()
        if not job_id:
            self._show_error("job_id를 찾지 못했습니다.")
            return
        self._set_current_job(self.job_store.load_job(job_id))
        self._go_to_step(self._job_stage_to_step_index(self.current_job.current_stage), force=True)

    def _set_current_job(self, job: JobState) -> None:
        self.current_job = job
        self.completed_job_snapshot = None
        self.input_path_edit.setText(str(job.source_path))
        self.output_dir_edit.setText(str(job.output_dir))
        self.delay_spin.setValue(job.delay_ms)
        self.title_prefix_edit.setText(job.title_prefix)
        self.tags_edit.setText(", ".join(job.tags))
        self.description_template_edit.setPlainText(job.description_template)
        self.playlist_edit.setText(job.playlist_id)
        self.default_category_edit.setText(job.category_id)
        self.game_edit.setText(job.game)
        self.preset_edit.setText(job.preset)
        self.characters_edit.setText(job.characters)
        self.build_info_edit.setText(job.build_info)
        privacy_index = self.privacy_combo.findData(job.privacy_status)
        if privacy_index >= 0:
            self.privacy_combo.setCurrentIndex(privacy_index)
        if not job.segment_drafts and not job.segments:
            job.segment_drafts = [self._new_segment_draft(1)]
            self.job_store.save_job(job)
        self._metadata_segment_index = 0
        self._sync_segment_table()
        if self.segment_table.rowCount() > 0:
            self.segment_table.selectRow(0)
        self._refresh_upload_results()
        self._refresh_workflow_shell()

    def _create_job_from_source(self) -> None:
        if not self._ensure_required_runtimes_ready():
            return
        source_path = self._required_path(self.input_path_edit.text(), "작업할 MKV 파일을 먼저 선택해 주세요.")
        if not source_path.exists():
            self._show_error(f"입력 MKV 파일이 존재하지 않습니다: {source_path}")
            return
        output_dir = self._path_or_none(self.output_dir_edit.text()) or (source_path.parent / "exports")
        try:
            title_prefix = render_template(self.title_prefix_edit.text().strip(), source=source_path)
            description_template = render_template(self.description_template_edit.toPlainText(), source=source_path)
        except (ValueError, TemplateRenderError) as exc:
            self._show_error(str(exc))
            return
        segment_drafts = [self._new_segment_draft(1)]
        job = self.job_store.create_job(
            source_path=source_path,
            output_dir=output_dir,
            obs_source_dir=self._path_or_none(self.obs_source_dir_edit.text()),
            title_prefix=title_prefix,
            description_template=description_template,
            tags=self._parse_tags(),
            playlist_id=self.playlist_edit.text().strip(),
            privacy_status=str(self.privacy_combo.currentData()),
            category_id=self.default_category_edit.text().strip() or "22",
            game=self.game_edit.text().strip(),
            preset=self.preset_edit.text().strip(),
            characters=self.characters_edit.text().strip(),
            build_info=self.build_info_edit.text().strip(),
            segment_drafts=segment_drafts,
        )
        job.current_stage = JOB_STAGE_SYNC
        job.delay_ms = self.delay_spin.value()
        self.job_store.save_job(job)
        self.data_manager.pick_recording(source_path)
        self._set_current_job(job)
        self._refresh_jobs_list()
        self._append_log(f"새 job을 생성했습니다: {job.job_id}")
        self._go_to_step(1, force=True)

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
            self._set_selected_source(Path(path_text))

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
        self._set_selected_source(Path(selected))

    def _set_selected_source(self, input_path: Path) -> None:
        self.input_path_edit.setText(str(input_path))
        self.data_manager.pick_recording(input_path)
        if not self.output_dir_edit.text().strip():
            saved_output_dir = self.data_manager.load()["last_output_dir"]
            self.output_dir_edit.setText(saved_output_dir or str(input_path.parent / "exports"))
        self._refresh_recordings()
        self._refresh_workflow_shell()

    def _choose_output_dir(self) -> None:
        start_dir = self.output_dir_edit.text().strip() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "출력 폴더 선택", start_dir)
        if not selected:
            return
        self.output_dir_edit.setText(selected)
        self._refresh_workflow_shell()

    def _new_segment_draft(self, index: int) -> ClipDraft:
        return ClipDraft(clip_id=uuid.uuid4().hex, clip_name=f"segment_{index:02d}", upload_enabled=True)

    def _current_segment_drafts(self) -> list[ClipDraft]:
        if self.current_job is None:
            return []
        return self.current_job.segment_drafts

    def _sync_segment_table(self) -> None:
        drafts = self._current_segment_drafts()
        self._syncing_draft_table = True
        try:
            self.segment_table.setRowCount(len(drafts))
            for row, draft in enumerate(drafts):
                self._set_table_item(row, DRAFT_TABLE_NAME, draft.clip_name)
                self._set_table_item(row, DRAFT_TABLE_START, draft.start_time or "")
                self._set_table_item(row, DRAFT_TABLE_END, draft.end_time or "")
                upload_item = self.segment_table.item(row, DRAFT_TABLE_UPLOAD)
                if upload_item is None:
                    upload_item = QTableWidgetItem()
                    upload_item.setFlags(upload_item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                    self.segment_table.setItem(row, DRAFT_TABLE_UPLOAD, upload_item)
                upload_item.setCheckState(Qt.CheckState.Checked if draft.upload_enabled else Qt.CheckState.Unchecked)
        finally:
            self._syncing_draft_table = False

    def _set_table_item(self, row: int, column: int, text: str) -> None:
        item = self.segment_table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self.segment_table.setItem(row, column, item)
        item.setText(text)

    def _selected_segment_draft_index(self) -> int | None:
        selected_rows = self.segment_table.selectionModel().selectedRows()
        if not selected_rows:
            return None
        return selected_rows[0].row()

    def _selected_segment_draft(self) -> ClipDraft | None:
        row = self._selected_segment_draft_index()
        drafts = self._current_segment_drafts()
        if row is None or row >= len(drafts):
            return None
        return drafts[row]

    def _add_segment_draft(self) -> None:
        if self.current_job is None:
            self._show_error("먼저 job을 생성해 주세요.")
            return
        self._store_selected_segment_draft()
        self.current_job.segment_drafts.append(self._new_segment_draft(len(self.current_job.segment_drafts) + 1))
        self.current_job.current_stage = JOB_STAGE_SEGMENTS
        self.job_store.save_job(self.current_job)
        self._sync_segment_table()
        self.segment_table.selectRow(len(self.current_job.segment_drafts) - 1)
        self._refresh_workflow_shell()

    def _remove_selected_segment_draft(self) -> None:
        if self.current_job is None:
            return
        row = self._selected_segment_draft_index()
        if row is None:
            return
        self.current_job.segment_drafts.pop(row)
        if not self.current_job.segment_drafts:
            self.current_job.segment_drafts.append(self._new_segment_draft(1))
        self.job_store.save_job(self.current_job)
        self._sync_segment_table()
        self.segment_table.selectRow(min(row, len(self.current_job.segment_drafts) - 1))
        self._refresh_workflow_shell()

    def _on_segment_selection_changed(self) -> None:
        self._load_selected_segment_draft()
        self._refresh_workflow_shell()

    def _load_selected_segment_draft(self) -> None:
        draft = self._selected_segment_draft()
        self._syncing_draft_details = True
        try:
            if draft is None:
                self.segment_name_edit.clear()
                self.segment_start_edit.clear()
                self.segment_end_edit.clear()
                self.segment_upload_checkbox.setChecked(False)
                return
            self.segment_name_edit.setText(draft.clip_name)
            self.segment_start_edit.setText(draft.start_time or "")
            self.segment_end_edit.setText(draft.end_time or "")
            self.segment_upload_checkbox.setChecked(draft.upload_enabled)
        finally:
            self._syncing_draft_details = False

    def _store_selected_segment_draft(self) -> None:
        if self._syncing_draft_details or self.current_job is None:
            return
        draft = self._selected_segment_draft()
        row = self._selected_segment_draft_index()
        if draft is None or row is None:
            return
        draft.clip_name = self.segment_name_edit.text().strip()
        draft.start_time = self.segment_start_edit.text().strip() or None
        draft.end_time = self.segment_end_edit.text().strip() or None
        draft.upload_enabled = self.segment_upload_checkbox.isChecked()
        self._syncing_draft_table = True
        try:
            self._set_table_item(row, DRAFT_TABLE_NAME, draft.clip_name)
            self._set_table_item(row, DRAFT_TABLE_START, draft.start_time or "")
            self._set_table_item(row, DRAFT_TABLE_END, draft.end_time or "")
            upload_item = self.segment_table.item(row, DRAFT_TABLE_UPLOAD)
            if upload_item is not None:
                upload_item.setCheckState(Qt.CheckState.Checked if draft.upload_enabled else Qt.CheckState.Unchecked)
        finally:
            self._syncing_draft_table = False
        self.job_store.save_job(self.current_job)
        self._refresh_workflow_shell()

    def _on_segment_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._syncing_draft_table or self.current_job is None:
            return
        row = item.row()
        if row < 0 or row >= len(self.current_job.segment_drafts):
            return
        draft = self.current_job.segment_drafts[row]
        if item.column() == DRAFT_TABLE_NAME:
            draft.clip_name = item.text().strip()
        elif item.column() == DRAFT_TABLE_START:
            draft.start_time = item.text().strip() or None
        elif item.column() == DRAFT_TABLE_END:
            draft.end_time = item.text().strip() or None
        elif item.column() == DRAFT_TABLE_UPLOAD:
            draft.upload_enabled = item.checkState() == Qt.CheckState.Checked
        if row == self._selected_segment_draft_index():
            self._load_selected_segment_draft()
        self.job_store.save_job(self.current_job)
        self._refresh_workflow_shell()

    def _load_source_preview_for_sync(self) -> None:
        if self.current_job is None:
            self._show_error("먼저 job을 생성해 주세요.")
            return
        self._load_preview_media(self.current_job.source_path, label=f"원본: {self.current_job.source_path.name}")

    def _load_synced_preview(self) -> None:
        if self.current_job is None or self.current_job.synced_source_path is None:
            self._show_error("아직 동기화 결과가 없습니다.")
            return
        self._load_preview_media(
            self.current_job.synced_source_path,
            label=f"싱크 결과: {self.current_job.synced_source_path.name}",
        )

    def _load_selected_draft_preview(self) -> None:
        if self.current_job is None:
            self._show_error("먼저 job을 생성해 주세요.")
            return
        draft = self._selected_segment_draft()
        if draft is None:
            self._show_error("미리볼 세그먼트를 선택해 주세요.")
            return
        source_path = self.current_job.synced_source_path or self.current_job.source_path
        self._load_preview_media(
            source_path,
            start_time=draft.start_time,
            label=f"분할 미리보기: {draft.clip_name}",
        )

    def _apply_preview_time_to_selected_draft(self, field_name: str) -> None:
        draft = self._selected_segment_draft()
        if draft is None:
            self._show_error("먼저 세그먼트를 선택해 주세요.")
            return
        timecode = self.preview_host.current_timecode.strip()
        if not timecode:
            self._show_error("아직 미리보기 시점을 읽지 못했습니다.")
            return
        if field_name == "start":
            draft.start_time = timecode
        else:
            draft.end_time = timecode
        self._load_selected_segment_draft()
        self._sync_segment_table()
        if self.current_job is not None:
            self.job_store.save_job(self.current_job)
        self._append_log(f"{draft.clip_name}의 {field_name} 시점에 {timecode}를 적용했습니다.")
        self._refresh_workflow_shell()

    def _apply_sync_for_current_job(self) -> None:
        if self.current_job is None:
            self._show_error("먼저 job을 생성해 주세요.")
            return
        self.current_job.delay_ms = self.delay_spin.value()
        self.current_job.current_stage = JOB_STAGE_SYNC
        self.job_store.save_job(self.current_job)
        sync_output_path = get_job_artifacts_dir(self.current_job.job_id) / "synced.mkv"
        self._start_worker(
            action=WorkerAction.APPLY_SYNC,
            job_state=self.current_job,
            sync_output_path=sync_output_path,
        )

    def _split_current_job_segments(self) -> None:
        if self.current_job is None:
            self._show_error("먼저 job을 생성해 주세요.")
            return
        self._store_selected_segment_draft()
        if not self.current_job.segment_drafts:
            self._show_error("분할할 세그먼트를 하나 이상 추가해 주세요.")
            return
        self.current_job.current_stage = JOB_STAGE_SEGMENTS
        self.job_store.save_job(self.current_job)
        self._start_worker(
            action=WorkerAction.SPLIT_SEGMENTS,
            job_state=self.current_job,
            segment_drafts=list(self.current_job.segment_drafts),
        )

    def _current_segments(self) -> list[SegmentState]:
        if self.current_job is None:
            return []
        return self.current_job.segments

    def _current_metadata_segment(self) -> SegmentState | None:
        segments = self._current_segments()
        if not segments:
            return None
        if self._metadata_segment_index < 0:
            self._metadata_segment_index = 0
        if self._metadata_segment_index >= len(segments):
            self._metadata_segment_index = len(segments) - 1
        return segments[self._metadata_segment_index]

    def _move_metadata_segment(self, offset: int) -> None:
        segments = self._current_segments()
        if not segments:
            return
        self._store_current_metadata_form()
        self._metadata_segment_index = max(0, min(len(segments) - 1, self._metadata_segment_index + offset))
        self._load_current_metadata_segment()
        self._refresh_workflow_shell()

    def _load_current_segment_preview(self) -> None:
        segment = self._current_metadata_segment()
        if segment is None or segment.output_path is None:
            self._show_error("미리볼 세그먼트가 없습니다.")
            return
        self._load_preview_media(segment.output_path, label=f"세그먼트: {segment.output_path.name}")

    def _apply_preview_time_to_current_segment_thumbnail(self) -> None:
        segment = self._current_metadata_segment()
        if segment is None:
            self._show_error("먼저 메타데이터를 작성할 세그먼트를 선택해 주세요.")
            return
        timecode = self.preview_host.current_timecode.strip()
        if not timecode:
            self._show_error("아직 미리보기 시점을 읽지 못했습니다.")
            return
        segment.thumbnail_time = timecode
        self.current_segment_thumbnail_time_edit.setText(timecode)
        self._refresh_workflow_shell()

    def _add_preview_chapter_to_current_segment(self) -> None:
        segment = self._current_metadata_segment()
        if segment is None:
            self._show_error("먼저 메타데이터를 작성할 세그먼트를 선택해 주세요.")
            return
        timecode = self.preview_host.current_timecode.strip()
        if not timecode:
            self._show_error("아직 미리보기 시점을 읽지 못했습니다.")
            return
        chapter_title = self.metadata_chapter_title_edit.text().strip()
        segment.chapters.append(ChapterMarker(timecode=timecode, title=chapter_title))
        self.current_segment_chapters_edit.setPlainText(self._chapters_to_text(segment.chapters))
        self.metadata_chapter_title_edit.clear()
        self._refresh_workflow_shell()

    def _load_current_metadata_segment(self) -> None:
        segment = self._current_metadata_segment()
        if segment is None:
            self.current_metadata_segment_value.setText("세그먼트 없음")
            self.current_segment_title_edit.clear()
            self.current_segment_thumbnail_time_edit.clear()
            self.current_segment_category_edit.clear()
            self.current_segment_upload_checkbox.setChecked(False)
            self.current_segment_notes_edit.clear()
            self.current_segment_description_edit.clear()
            self.current_segment_chapters_edit.clear()
            return
        self.current_metadata_segment_value.setText(
            f"{self._metadata_segment_index + 1}/{len(self._current_segments())} | {segment.clip_name}"
        )
        self.current_segment_title_edit.setText(segment.custom_title)
        self.current_segment_thumbnail_time_edit.setText(segment.thumbnail_time or "")
        self.current_segment_category_edit.setText(segment.category_id or self.default_category_edit.text().strip() or "22")
        self.current_segment_upload_checkbox.setChecked(segment.upload_enabled)
        self.current_segment_notes_edit.setPlainText(segment.custom_notes)
        self.current_segment_description_edit.setPlainText(segment.description_text)
        self.current_segment_chapters_edit.setPlainText(self._chapters_to_text(segment.chapters))

    def _store_common_job_metadata(self) -> None:
        if self.current_job is None:
            return
        self.current_job.title_prefix = self.title_prefix_edit.text().strip()
        self.current_job.tags = self._parse_tags()
        self.current_job.description_template = self.description_template_edit.toPlainText().strip()
        self.current_job.playlist_id = self.playlist_edit.text().strip()
        self.current_job.privacy_status = str(self.privacy_combo.currentData())
        self.current_job.category_id = self.default_category_edit.text().strip() or "22"
        self.current_job.game = self.game_edit.text().strip()
        self.current_job.preset = self.preset_edit.text().strip()
        self.current_job.characters = self.characters_edit.text().strip()
        self.current_job.build_info = self.build_info_edit.text().strip()

    def _store_current_metadata_form(self) -> None:
        if self.current_job is None:
            return
        segment = self._current_metadata_segment()
        if segment is None:
            return
        self._store_common_job_metadata()
        segment.custom_title = self.current_segment_title_edit.text().strip()
        segment.thumbnail_time = self.current_segment_thumbnail_time_edit.text().strip() or None
        segment.category_id = self.current_segment_category_edit.text().strip() or self.current_job.category_id or "22"
        segment.upload_enabled = self.current_segment_upload_checkbox.isChecked()
        segment.custom_notes = self.current_segment_notes_edit.toPlainText().strip()
        segment.description_text = self.current_segment_description_edit.toPlainText().strip()
        segment.chapters = self._parse_chapters(self.current_segment_chapters_edit.toPlainText())
        self.job_store.save_job(self.current_job)

    def _save_current_segment_metadata(self, *, advance: bool = False) -> None:
        if self.current_job is None:
            self._show_error("먼저 job을 생성해 주세요.")
            return
        segment = self._current_metadata_segment()
        if segment is None:
            self._show_error("저장할 세그먼트가 없습니다.")
            return
        self._store_current_metadata_form()
        try:
            self.workflow_runner.save_segment_metadata(
                self.current_job,
                segment,
                log_callback=self._append_log,
            )
        except Exception as exc:
            self._show_error(str(exc))
            return
        self.current_job.current_stage = JOB_STAGE_METADATA
        if self._ready_for_upload():
            self.current_job.current_stage = JOB_STAGE_UPLOAD
        self.job_store.save_job(self.current_job)
        self._append_log(f"{segment.clip_name} 메타데이터를 저장했습니다.")
        if advance:
            self._move_metadata_segment(1)
        else:
            self._load_current_metadata_segment()
        self._refresh_upload_results()
        self._refresh_workflow_shell()

    def _refresh_upload_results(self) -> None:
        if not hasattr(self, "upload_results_list"):
            return
        job = self.current_job or self.completed_job_snapshot
        self._syncing_upload_results = True
        self.upload_results_list.blockSignals(True)
        try:
            self.upload_results_list.clear()
            if job is None:
                return
            for segment in job.segments:
                marker = segment.upload_status
                item = QListWidgetItem(f"[{marker}] {segment.clip_name}")
                item.setData(Qt.ItemDataRole.UserRole, segment.clip_id)
                self.upload_results_list.addItem(item)
        finally:
            self.upload_results_list.blockSignals(False)
            self._syncing_upload_results = False
        if self.upload_results_list.count() > 0:
            self.upload_results_list.setCurrentRow(0)
        self._refresh_upload_result_detail()

    def _selected_upload_segment(self) -> SegmentState | None:
        job = self.current_job or self.completed_job_snapshot
        if job is None:
            return None
        selected_items = self.upload_results_list.selectedItems()
        if not selected_items:
            return None
        clip_id = str(selected_items[0].data(Qt.ItemDataRole.UserRole) or "").strip()
        for segment in job.segments:
            if segment.clip_id == clip_id:
                return segment
        return None

    def _on_upload_result_selection_changed(self) -> None:
        if self._syncing_upload_results:
            return
        self._refresh_upload_result_detail()

    def _refresh_upload_result_detail(self) -> None:
        segment = self._selected_upload_segment()
        if segment is None:
            self.upload_result_detail_value.setText("선택된 세그먼트가 없습니다.")
            return
        lines = [
            f"세그먼트: {segment.clip_name}",
            f"출력 파일: {segment.output_path or '없음'}",
            f"sidecar: {segment.sidecar_path or '없음'}",
            f"업로드 상태: {segment.upload_status}",
        ]
        if segment.upload_url:
            lines.append(f"URL: {segment.upload_url}")
        if segment.upload_error:
            lines.append(f"오류: {segment.upload_error}")
        self.upload_result_detail_value.setText("\n".join(lines))

    def _authenticate_google(self) -> None:
        if not self._ensure_client_secrets_available():
            return
        self._start_worker(action=WorkerAction.AUTHENTICATE)

    def _upload_and_cleanup_current_job(self) -> None:
        if self.current_job is None:
            self._show_error("업로드할 job이 없습니다.")
            return
        if not self._ensure_required_runtimes_ready():
            return
        if not self._ensure_client_secrets_available():
            return
        self._store_current_metadata_form()
        self._start_worker(action=WorkerAction.UPLOAD_AND_CLEANUP, job_state=self.current_job)

    def _copy_selected_clipboard(self) -> None:
        segment = self._selected_upload_segment() or self._current_metadata_segment()
        if segment is None or segment.sidecar_path is None or not segment.sidecar_path.exists():
            self._show_error("복사할 메타데이터가 없습니다.")
            return
        payload = read_sidecar(segment.sidecar_path)
        QApplication.clipboard().setText(build_clipboard_payload(payload))
        self._append_log(f"{segment.clip_name} 메타데이터를 클립보드에 복사했습니다.")

    def _open_output_folder(self) -> None:
        job = self.current_job or self.completed_job_snapshot
        if job is None:
            self._show_error("아직 열 수 있는 출력 폴더가 없습니다.")
            return
        if not job.output_dir.exists():
            self._show_error(f"출력 폴더가 존재하지 않습니다: {job.output_dir}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(job.output_dir)))

    def _start_worker(
        self,
        *,
        action: WorkerAction,
        job_state: JobState | None = None,
        runtime_package: str | None = None,
        sync_output_path: Path | None = None,
        segment_drafts: list[ClipDraft] | None = None,
    ) -> None:
        if self._active_thread is not None:
            self._show_error("이미 다른 작업이 실행 중입니다.")
            return
        thread, worker = create_worker_thread(
            action=action,
            job_state=job_state,
            runtime_package=runtime_package,
            sync_output_path=sync_output_path,
            segment_drafts=segment_drafts,
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
        if stage in {"VALIDATING", "SYNCING", "REMUXING", "THUMBNAIL", "CLEANUP", "INSTALLING_RUNTIME"}:
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
        if self.current_job is not None:
            self.current_job.last_error = message
            self.job_store.save_job(self.current_job)
        self.set_state(AppWorkflowState.ERROR)
        self._append_log(message)
        self._show_error(message)

    def _on_worker_completed(self, payload: object) -> None:
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
            message = f"MPC-BE 설정을 가져왔습니다: {label}" if label else "가져올 MPC-BE 설정을 찾지 못했습니다."
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

        job = payload.get("job")
        if isinstance(job, JobState):
            self.current_job = job

        if action == WorkerAction.APPLY_SYNC.value and self.current_job is not None:
            self.current_job.current_stage = JOB_STAGE_SEGMENTS
            self.job_store.save_job(self.current_job)
            self.set_state(AppWorkflowState.IDLE)
            self._append_log("사운드 싱크 적용을 완료했습니다.")
            self._go_to_step(2, force=True)
        elif action == WorkerAction.SPLIT_SEGMENTS.value and self.current_job is not None:
            self.current_job.current_stage = JOB_STAGE_METADATA
            self.job_store.save_job(self.current_job)
            self._metadata_segment_index = 0
            self.set_state(AppWorkflowState.IDLE)
            self._append_log(f"세그먼트 {len(self.current_job.segments)}개 분할을 완료했습니다.")
            self._go_to_step(3, force=True)
        elif action == WorkerAction.UPLOAD_AND_CLEANUP.value and self.current_job is not None:
            results = payload.get("results", [])
            self._append_log(f"세그먼트 업로드 결과 {len(results)}건을 기록했습니다.")
            if self.current_job.cleanup_status == "done":
                self.completed_job_snapshot = self.current_job
                self.job_store.delete_job(self.current_job.job_id)
                self.current_job = None
                self._append_log("업로드 및 정리가 완료되어 job 상태를 제거했습니다.")
            else:
                self.current_job.current_stage = JOB_STAGE_UPLOAD
                self.job_store.save_job(self.current_job)
                self._append_log("실패한 업로드가 있어 job을 유지합니다. 다시 시도할 수 있습니다.")
            self.set_state(AppWorkflowState.DONE)
        self._refresh_jobs_list()
        self._refresh_upload_results()
        self._refresh_workflow_shell()

    def _on_worker_finished(self) -> None:
        self._refresh_runtime_statuses()
        self._append_log("백그라운드 작업이 종료되었습니다.")

    def _on_thread_finished(self) -> None:
        self._active_worker = None
        self._active_thread = None
        self._refresh_workflow_shell()

    def set_state(self, state: AppWorkflowState) -> None:
        self.state_value.setText(STATE_LABELS.get(state, state.value))
        busy = state in {
            AppWorkflowState.PROCESSING,
            AppWorkflowState.AUTHENTICATING,
            AppWorkflowState.UPLOADING,
        }
        for control in self._busy_controls:
            control.setEnabled(not busy)
        self.preview_play_pause_button.setEnabled(not busy)
        self.preview_refresh_time_button.setEnabled(not busy)
        self.preview_expand_button.setEnabled(True)
        self.cancel_button.setEnabled(busy)
        self._refresh_workflow_shell()

    def _install_runtime_package(self, package_id: str) -> None:
        self._start_worker(action=WorkerAction.INSTALL_RUNTIME, runtime_package=package_id)

    def _import_mpc_be_settings(self) -> None:
        self._start_worker(action=WorkerAction.IMPORT_MPC_BE)

    def _refresh_runtime_statuses(self) -> None:
        statuses = {status.package_id: status for status in self.runtime_installer.list_statuses()}
        self._apply_runtime_status(
            status_text=statuses["losslesscut"].status_text,
            source_label=statuses["losslesscut"].source_label,
            status_label=self.losslesscut_runtime_status_value,
            button=self.install_losslesscut_button,
            default_button_text="LosslessCut 설치하기",
            enable_button=statuses["losslesscut"].installed or bool(statuses["losslesscut"].source_label),
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

    def _load_preview_media(self, media_path: Path, *, start_time: str | None = None, label: str | None = None) -> None:
        if not self._ensure_required_runtimes_ready():
            return
        try:
            self.preview_host.load_media(media_path, start_time=start_time)
            self.preview_media_value.setText(label or media_path.name)
        except Exception as exc:
            self._show_error(str(exc))

    def _toggle_preview_expanded(self) -> None:
        if not self.preview_container.isVisible():
            return
        self._preview_expanded = not self._preview_expanded
        if self._preview_expanded:
            self.content_splitter.setSizes([1400, 200])
            self.preview_expand_button.setText("복귀")
        else:
            self.content_splitter.setSizes([980, 520])
            self.preview_expand_button.setText("확장")

    def _on_preview_position_changed(self, timecode: str) -> None:
        self.preview_time_value.setText(timecode)

    def _on_preview_connection_changed(self, connected: bool) -> None:
        self.preview_connection_value.setText("연결됨" if connected else "연결 안 됨")

    def _on_delay_changed(self, value: int) -> None:
        if self.current_job is None:
            return
        self.current_job.delay_ms = value
        self.job_store.save_job(self.current_job)
        self._refresh_workflow_shell()

    def _ensure_required_runtimes_ready(self) -> bool:
        if self.runtime_installer.is_ready():
            return True
        missing_labels = [
            self.runtime_installer.PACKAGE_LABELS[package_id]
            for package_id in self.runtime_installer.missing_required_package_ids()
        ]
        self._show_error("필수 도구가 아직 준비되지 않았습니다: " + ", ".join(missing_labels))
        return False

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

    def _cancel_active_job(self) -> None:
        if self._active_worker is None:
            return
        self._active_worker.cancel()
        self._append_log("취소를 요청했습니다.")

    def _append_log(self, message: str) -> None:
        if not message:
            return
        self.log_view.appendPlainText(message)
        self._refresh_workflow_shell()

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "오류", message)

    def _parse_tags(self) -> list[str]:
        return [tag.strip() for tag in self.tags_edit.text().split(",") if tag.strip()]

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

    def _current_step_index(self) -> int:
        return self.step_stack.currentIndex()

    def _on_step_selected(self, index: int) -> None:
        if self._syncing_step_selection or index < 0:
            return
        self._go_to_step(index)

    def _go_to_step(self, index: int, *, force: bool = False) -> None:
        if index < 0 or index >= self.step_stack.count():
            return
        if index == self._current_step_index():
            self._refresh_workflow_shell()
            return
        if not force and index > self._current_step_index() and not self._can_navigate_to_step(index):
            self._sync_step_selection()
            return
        self.step_stack.setCurrentIndex(index)
        self._sync_step_selection()
        self._refresh_workflow_shell()

    def _sync_step_selection(self) -> None:
        self._syncing_step_selection = True
        try:
            self.step_list.setCurrentRow(self._current_step_index())
        finally:
            self._syncing_step_selection = False

    def _job_stage_to_step_index(self, stage: str) -> int:
        if stage == JOB_STAGE_SELECT:
            return 0
        if stage == JOB_STAGE_SYNC:
            return 1
        if stage == JOB_STAGE_SEGMENTS:
            return 2
        if stage == JOB_STAGE_METADATA:
            return 3
        return 4

    def _can_navigate_to_step(self, target_index: int) -> bool:
        if target_index >= 1 and not self._validate_source_stage(show_error=True):
            return False
        if target_index >= 2 and not self._validate_sync_stage(show_error=True):
            return False
        if target_index >= 3 and not self._validate_segments_stage(show_error=True):
            return False
        if target_index >= 4 and not self._validate_metadata_stage(show_error=True):
            return False
        return True

    def _validate_source_stage(self, *, show_error: bool) -> bool:
        if self.current_job is None:
            if show_error:
                self._show_error("먼저 작업 대상 파일을 선택하고 job을 생성해 주세요.")
            return False
        return True

    def _validate_sync_stage(self, *, show_error: bool) -> bool:
        if self.current_job is None or self.current_job.synced_source_path is None:
            if show_error:
                self._show_error("사운드 싱크를 먼저 적용해 주세요.")
            return False
        if not self.current_job.synced_source_path.exists():
            if show_error:
                self._show_error(f"동기화 결과 파일이 존재하지 않습니다: {self.current_job.synced_source_path}")
            return False
        return True

    def _validate_segments_stage(self, *, show_error: bool) -> bool:
        if self.current_job is None or not self.current_job.segments:
            if show_error:
                self._show_error("세그먼트 분할을 먼저 완료해 주세요.")
            return False
        return True

    def _ready_for_upload(self) -> bool:
        if self.current_job is None:
            return False
        active_segments = [segment for segment in self.current_job.segments if segment.upload_enabled]
        if not active_segments:
            return bool(self.current_job.segments)
        return all(segment.metadata_ready and segment.sidecar_path and segment.sidecar_path.exists() for segment in active_segments)

    def _validate_metadata_stage(self, *, show_error: bool) -> bool:
        self._store_current_metadata_form()
        if self.current_job is None or not self.current_job.segments:
            if show_error:
                self._show_error("먼저 세그먼트를 분할해 주세요.")
            return False
        for segment in self.current_job.segments:
            if segment.upload_enabled and not segment.metadata_ready:
                if show_error:
                    self._show_error(f"세그먼트 '{segment.clip_name}'의 메타데이터 저장이 아직 완료되지 않았습니다.")
                return False
        return True

    def _refresh_workflow_shell(self) -> None:
        current_index = self._current_step_index()
        self.workflow_heading_value.setText(f"{current_index + 1}. {STEP_TITLES[current_index]}")
        self.workflow_summary_value.setText(self._build_workflow_summary(current_index))
        self._refresh_source_view()
        self._refresh_sync_view()
        self._refresh_segments_view()
        self._refresh_metadata_view()
        self._refresh_upload_view()
        self._refresh_step_list_labels()
        self.previous_step_button.setEnabled(current_index > 0)
        self.next_step_button.setEnabled(current_index < self.step_stack.count() - 1)
        show_preview = current_index in {1, 2, 3} and self.current_job is not None
        self.preview_container.setVisible(show_preview)

    def _refresh_step_list_labels(self) -> None:
        readiness = [
            self.current_job is not None,
            self._validate_sync_stage(show_error=False),
            self._validate_segments_stage(show_error=False),
            self._ready_for_upload(),
            (self.current_job is not None and self.current_job.current_stage == JOB_STAGE_UPLOAD) or self.completed_job_snapshot is not None or self._ready_for_upload(),
        ]
        current_index = self._current_step_index()
        for index, title in enumerate(STEP_TITLES):
            item = self.step_list.item(index)
            if item is None:
                continue
            marker = "→" if index == current_index else ("✓" if readiness[index] else "○")
            item.setText(f"{marker} {index + 1}. {title}")

    def _build_workflow_summary(self, step_index: int) -> str:
        job = self.current_job or self.completed_job_snapshot
        if job is None:
            return "job이 아직 없습니다. 1단계에서 작업 대상 MKV를 선택하고 새 job을 생성해 주세요."
        upload_enabled_count = sum(1 for segment in job.segments if segment.upload_enabled)
        metadata_ready_count = sum(1 for segment in job.segments if segment.metadata_ready)
        uploaded_count = sum(1 for segment in job.segments if segment.upload_status == "uploaded")
        lines = [
            f"job_id: {job.job_id}",
            f"소스: {job.source_path}",
            f"싱크 delay: {job.delay_ms} ms",
            f"세그먼트 초안: {len(job.segment_drafts)}개 | 생성 세그먼트: {len(job.segments)}개",
            f"메타데이터 완료: {metadata_ready_count}개 | 업로드 대상: {upload_enabled_count}개 | 업로드 완료: {uploaded_count}개",
        ]
        if step_index == 4 and job.cleanup_status:
            lines.append(f"정리 상태: {job.cleanup_status}")
        return "\n".join(lines)

    def _refresh_source_view(self) -> None:
        job = self.current_job
        if job is None:
            self.current_job_value.setText("선택된 job 없음")
            return
        self.current_job_value.setText(
            f"{job.job_id}\nstage: {job.current_stage}\nsource: {job.source_path.name}"
        )

    def _refresh_sync_view(self) -> None:
        if self.current_job is None:
            self.sync_summary_value.setText("job이 없습니다.")
            self.synced_source_value.setText("동기화 결과 없음")
            return
        self.sync_summary_value.setText(
            "\n".join(
                [
                    f"원본: {self.current_job.source_path}",
                    f"현재 delay: {self.current_job.delay_ms} ms",
                    "MPC-BE 미리보기로 사운드 싱크를 확인한 뒤 delay 값을 확정합니다.",
                ]
            )
        )
        self.synced_source_value.setText(str(self.current_job.synced_source_path) if self.current_job.synced_source_path else "동기화 결과 없음")

    def _refresh_segments_view(self) -> None:
        if self.current_job is None:
            self.segment_stage_summary_value.setText("job이 없습니다.")
            return
        self.segment_stage_summary_value.setText(
            f"동기화 소스: {self.current_job.synced_source_path or self.current_job.source_path}\n"
            f"세그먼트 초안 {len(self.current_job.segment_drafts)}개, 생성 완료 {len(self.current_job.segments)}개"
        )

    def _refresh_metadata_view(self) -> None:
        if self.current_job is None:
            self.metadata_stage_summary_value.setText("job이 없습니다.")
            self._load_current_metadata_segment()
            return
        ready_count = sum(1 for segment in self.current_job.segments if segment.metadata_ready)
        upload_enabled_count = sum(1 for segment in self.current_job.segments if segment.upload_enabled)
        self.metadata_stage_summary_value.setText(
            f"메타데이터 저장 완료 {ready_count}/{len(self.current_job.segments)}개 | 업로드 대상 {upload_enabled_count}개"
        )
        self._load_current_metadata_segment()

    def _refresh_upload_view(self) -> None:
        job = self.current_job or self.completed_job_snapshot
        if job is None:
            self.upload_stage_summary_value.setText("업로드할 job이 없습니다.")
            self.upload_result_detail_value.setText("선택된 세그먼트가 없습니다.")
            return
        uploaded_count = sum(1 for segment in job.segments if segment.upload_status == "uploaded")
        failed_count = sum(1 for segment in job.segments if segment.upload_status == "failed")
        self.upload_stage_summary_value.setText(
            f"업로드 성공 {uploaded_count}개 | 실패 {failed_count}개 | cleanup 상태: {job.cleanup_status}"
        )
        self._refresh_upload_result_detail()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.preview_host.shutdown()
        super().closeEvent(event)

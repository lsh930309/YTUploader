# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QResizeEvent
from PyQt6.QtWidgets import QWidget

from core.mpc_be import (
    CMD_CONNECT,
    CMD_CURRENTPOSITION,
    CMD_DISCONNECT,
    CMD_NOTIFYSEEK,
    COPYDATASTRUCT,
    MSG,
    MPCBEApiEvent,
    MPCBEController,
    MPCBEError,
    WM_COPYDATA,
    decode_copydata_payload,
)


class MPCBEPreviewHost(QWidget):
    """A native QWidget that hosts an MPC-BE window and exchanges MPC API messages."""

    log = pyqtSignal(str)
    error = pyqtSignal(str)
    connection_changed = pyqtSignal(bool)
    position_changed = pyqtSignal(str)
    api_event = pyqtSignal(object)

    def __init__(self, controller: MPCBEController | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller or MPCBEController()
        self.current_seconds = 0.0
        self.current_timecode = "00:00:00.000"
        self.setMinimumHeight(320)
        self.setStyleSheet("background-color: #000000; border: 1px solid #303030;")
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(500)
        self._poll_timer.timeout.connect(self.request_current_position)

    @property
    def host_hwnd(self) -> int:
        return int(self.winId())

    def load_media(self, media_path: Path, *, start_time: str | None = None) -> None:
        try:
            if self.controller.is_connected:
                self.controller.open_file(media_path, sender_hwnd=self.host_hwnd)
                if start_time:
                    QTimer.singleShot(250, lambda timecode=start_time: self.seek(timecode))
            else:
                self.controller.launch_embedded(
                    self.host_hwnd,
                    media_path=media_path,
                    width=max(self.width(), 1),
                    height=max(self.height(), 1),
                    start_time=start_time,
                )
            self._poll_timer.start()
            self.log.emit(f"내장 미리보기에 {media_path.name} 파일을 불러왔습니다.")
        except Exception as exc:  # pragma: no cover - UI surface
            self.error.emit(str(exc))

    def play_pause(self) -> None:
        self._send_simple_command(self.controller.play_pause)

    def play(self) -> None:
        self._send_simple_command(self.controller.play)

    def pause(self) -> None:
        self._send_simple_command(self.controller.pause)

    def stop(self) -> None:
        self._send_simple_command(self.controller.stop)

    def seek(self, timecode: str) -> None:
        try:
            self.controller.seek(timecode, sender_hwnd=self.host_hwnd)
        except Exception as exc:  # pragma: no cover - UI surface
            self.error.emit(str(exc))

    def request_current_position(self) -> None:
        if not self.controller.is_connected:
            self._poll_timer.stop()
            return
        try:
            self.controller.request_current_position(sender_hwnd=self.host_hwnd)
        except MPCBEError:
            self._poll_timer.stop()
        except Exception as exc:  # pragma: no cover - UI surface
            self.error.emit(str(exc))

    def shutdown(self) -> None:
        self._poll_timer.stop()
        self.controller.shutdown()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.controller.resize_embedded_window(self.width(), self.height())

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.shutdown()
        super().closeEvent(event)

    def nativeEvent(self, event_type, message):  # type: ignore[override]
        if hasattr(event_type, "data"):
            event_name = bytes(event_type.data()).decode()
        elif isinstance(event_type, (bytes, bytearray)):
            event_name = bytes(event_type).decode()
        else:
            event_name = str(event_type)
        if event_name not in {"windows_generic_MSG", "windows_dispatcher_MSG"}:
            return False, 0
        if not hasattr(message, "__int__"):
            return False, 0

        msg = MSG.from_address(int(message))
        if msg.message != WM_COPYDATA:
            return False, 0

        copy_data = COPYDATASTRUCT.from_address(int(msg.lParam))
        event = self.controller.handle_api_message(
            sender_hwnd=int(msg.wParam),
            command=int(copy_data.dwData),
            payload=decode_copydata_payload(copy_data),
            host_hwnd=self.host_hwnd,
            width=max(self.width(), 1),
            height=max(self.height(), 1),
        )
        self._apply_api_event(event)
        return False, 0

    def _apply_api_event(self, event: MPCBEApiEvent) -> None:
        self.api_event.emit(event)
        if event.command == CMD_CONNECT:
            self.connection_changed.emit(True)
            self.log.emit("내장 MPC-BE 미리보기가 연결되었습니다.")
            return

        if event.command in {CMD_CURRENTPOSITION, CMD_NOTIFYSEEK} and event.timecode:
            self.current_seconds = event.position_seconds or 0.0
            self.current_timecode = event.timecode
            self.position_changed.emit(self.current_timecode)
            return

        if event.command == CMD_DISCONNECT:
            self.connection_changed.emit(False)
            self._poll_timer.stop()
            self.log.emit("내장 MPC-BE 미리보기가 연결 해제되었습니다.")

    def _send_simple_command(self, callback) -> None:
        try:
            callback(sender_hwnd=self.host_hwnd)
        except Exception as exc:  # pragma: no cover - UI surface
            self.error.emit(str(exc))

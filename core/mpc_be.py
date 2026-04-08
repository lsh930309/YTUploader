# -*- coding: utf-8 -*-
from __future__ import annotations

import configparser
import ctypes
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import (
    get_mpc_be_ini_path,
    get_mpc_be_runtime_dir,
    get_mpc_be_runtime_executable_path,
    resource_path,
)

try:  # pragma: no cover - exercised on Windows
    import winreg
except ModuleNotFoundError:  # pragma: no cover - non-Windows test environments
    winreg = None

try:  # pragma: no cover - exercised on Windows
    import ctypes.wintypes as wintypes
except ModuleNotFoundError:  # pragma: no cover - non-Windows test environments
    wintypes = None

MPC_BE_REGISTRY_KEY = r"Software\MPC-BE"
MPC_BE_API_SOURCE_URL = "https://github.com/Aleksoid1978/MPC-BE/blob/master/src/apps/mplayerc/mpcapi.h"
MPC_BE_CMDLINE_SOURCE_URL = "https://github.com/Aleksoid1978/MPC-BE/blob/master/src/apps/mplayerc/AppSettings.cpp"
REGISTRY_IMPORT_WHITELIST: dict[str, list[str]] = {
    "Settings": [
        "Language",
        "JumpDistS",
        "JumpDistM",
        "JumpDistL",
        "SnapShotPath",
        "SnapShotExt",
        "SnapShotSubtitles",
        "ThumbRows",
        "ThumbCols",
        "ThumbWidth",
        "ThumbQuality",
        "ThumbLevelPNG",
    ]
}

WM_COPYDATA = 0x004A
WM_CLOSE = 0x0010
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CHILD = 0x40000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_BORDER = 0x00800000
WS_DLGFRAME = 0x00400000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_POPUP = 0x80000000
WS_EX_APPWINDOW = 0x00040000
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_CLIENTEDGE = 0x00000200
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SW_SHOW = 5

CMD_CONNECT = 0x50000000
CMD_STATE = 0x50000001
CMD_PLAYMODE = 0x50000002
CMD_CURRENTPOSITION = 0x50000007
CMD_NOTIFYSEEK = 0x50000008
CMD_DISCONNECT = 0x5000000B
CMD_OPENFILE = 0xA0000000
CMD_STOP = 0xA0000001
CMD_CLOSEFILE = 0xA0000002
CMD_PLAYPAUSE = 0xA0000003
CMD_PLAY = 0xA0000004
CMD_PAUSE = 0xA0000005
CMD_SETPOSITION = 0xA0002000
CMD_GETCURRENTPOSITION = 0xA0003004
CMD_JUMPOFNSECONDS = 0xA0003005

COMMAND_NAMES = {
    CMD_CONNECT: "connect",
    CMD_STATE: "state",
    CMD_PLAYMODE: "playmode",
    CMD_CURRENTPOSITION: "current_position",
    CMD_NOTIFYSEEK: "notify_seek",
    CMD_DISCONNECT: "disconnect",
}


if wintypes is not None:  # pragma: no branch - platform gated
    class COPYDATASTRUCT(ctypes.Structure):
        _fields_ = [
            ("dwData", ctypes.c_size_t),
            ("cbData", wintypes.DWORD),
            ("lpData", wintypes.LPVOID),
        ]


    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt_x", ctypes.c_long),
            ("pt_y", ctypes.c_long),
            ("lPrivate", wintypes.DWORD),
        ]
else:  # pragma: no cover - non-Windows
    COPYDATASTRUCT = Any
    MSG = Any


@dataclass(slots=True)
class MPCBESettingsSource:
    source_type: str
    label: str
    path: Path | None = None


@dataclass(slots=True)
class MPCBEApiEvent:
    command: int
    name: str
    payload: str
    sender_hwnd: int | None = None
    player_hwnd: int | None = None
    position_seconds: float | None = None
    timecode: str | None = None


class MPCBEError(RuntimeError):
    pass


def parse_timecode_to_seconds(value: str | None) -> float | None:
    """Convert a user-facing timecode into seconds."""

    if value is None:
        return None

    text = value.strip()
    if not text:
        return None

    if ":" not in text:
        return float(text)

    parts = [part.strip() for part in text.split(":")]
    if len(parts) > 3:
        raise ValueError(f"지원하지 않는 시점 형식입니다: {value}")

    total = 0.0
    multiplier = 1.0
    for part in reversed(parts):
        total += float(part) * multiplier
        multiplier *= 60.0
    return total


def format_seconds_to_timecode(seconds: float | None) -> str:
    """Render seconds in the HH:MM:SS.mmm format used by the UI and ffmpeg."""

    if seconds is None:
        return ""

    positive_seconds = max(seconds, 0.0)
    hours = int(positive_seconds // 3600)
    minutes = int((positive_seconds % 3600) // 60)
    secs = positive_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def decode_copydata_payload(copy_data_struct: COPYDATASTRUCT) -> str:
    """Decode a WM_COPYDATA payload sent by MPC-BE."""

    if not copy_data_struct.lpData or copy_data_struct.cbData == 0:
        return ""
    char_count = max(int(copy_data_struct.cbData) // 2, 0)
    return ctypes.wstring_at(copy_data_struct.lpData, char_count).rstrip("\x00")


class MPCBEController:
    """Manage the bundled MPC-BE runtime, isolated settings, and host-side API control."""

    def __init__(
        self,
        executable_path: Path | None = None,
        profile_path: Path | None = None,
        runtime_dir: Path | None = None,
        runtime_sources: list[Path] | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir or get_mpc_be_runtime_dir()
        self.executable_path = executable_path or get_mpc_be_runtime_executable_path()
        self.profile_path = profile_path or get_mpc_be_ini_path()
        self.runtime_sources = runtime_sources
        self._process: subprocess.Popen[str] | None = None
        self._player_hwnd: int | None = None
        self._host_hwnd: int | None = None

    @property
    def player_hwnd(self) -> int | None:
        return self._player_hwnd

    @property
    def host_hwnd(self) -> int | None:
        return self._host_hwnd

    @property
    def is_connected(self) -> bool:
        return self._player_hwnd is not None

    def discover_settings_sources(self) -> list[MPCBESettingsSource]:
        candidates = [
            Path.home() / "AppData" / "Roaming" / "MPC-BE" / "mpc-be64.ini",
            Path.home() / "AppData" / "Roaming" / "MPC-BE" / "mpc-be.ini",
        ]

        for source_dir in self._iter_runtime_source_dirs():
            for ini_name in ("mpc-be64.ini", "mpc-be.ini"):
                candidates.append(source_dir / ini_name)

        sources: list[MPCBESettingsSource] = []
        seen: set[Path] = set()
        for candidate in candidates:
            if candidate == self.profile_path or candidate in seen:
                continue
            seen.add(candidate)
            if candidate.exists():
                sources.append(MPCBESettingsSource(source_type="ini", label=str(candidate), path=candidate))

        if winreg is not None and self._registry_settings_exist():
            sources.append(MPCBESettingsSource(source_type="registry", label=MPC_BE_REGISTRY_KEY))
        return sources

    def discover_runtime_sources(self) -> list[Path]:
        return self._iter_runtime_source_dirs()

    def ensure_runtime_installed(self) -> Path:
        """Copy a private MPC-BE runtime into the app data folder and return the executable path."""

        if self.executable_path.exists():
            self._ensure_profile_exists()
            return self.executable_path

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        for source_dir in self._iter_runtime_source_dirs():
            installed = self.install_from_source_dir(source_dir)
            if installed is not None:
                return installed

        raise MPCBEError(
            "MPC-BE 런타임을 찾지 못했습니다. 내장 미리보기를 사용하려면 "
            "bin/mpc-be 또는 bin/mpc-be64.exe를 패키징하거나, 이 PC에 MPC-BE를 설치해야 합니다."
        )

    def install_from_source_dir(self, source_dir: Path) -> Path | None:
        source_executable = source_dir / "mpc-be64.exe"
        if not source_executable.exists():
            return None

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        for source_path in source_dir.rglob("*"):
            if source_path.is_dir():
                continue
            if source_path.suffix.lower() == ".ini":
                continue

            relative_path = source_path.relative_to(source_dir)
            destination = self.runtime_dir / relative_path
            if source_path == source_executable:
                destination = self.executable_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.stat().st_size == source_path.stat().st_size:
                continue
            shutil.copy2(source_path, destination)

        self._ensure_profile_exists()
        return self.executable_path

    def import_settings(self) -> MPCBESettingsSource | None:
        for source in self.discover_settings_sources():
            if source.source_type == "ini" and source.path is not None:
                self.import_from_ini(source.path)
                return source
            if source.source_type == "registry":
                self.import_from_registry()
                return source
        return None

    def import_from_ini(self, source_path: Path) -> Path:
        self.ensure_runtime_installed()
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, self.profile_path)
        return self.profile_path

    def import_from_registry(self) -> Path:
        if winreg is None:
            raise MPCBEError("현재 환경에서는 Windows 레지스트리에 접근할 수 없습니다.")

        self.ensure_runtime_installed()
        config = configparser.ConfigParser()
        config.optionxform = str

        for section, keys in REGISTRY_IMPORT_WHITELIST.items():
            values = self._read_registry_section(section, keys)
            if values:
                config[section] = values

        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        with self.profile_path.open("w", encoding="utf-8") as handle:
            config.write(handle)
        return self.profile_path

    def launch_preview(self, media_path: Path, *, start_time: str | None = None) -> subprocess.Popen[str]:
        self.ensure_runtime_installed()
        if not media_path.exists():
            raise MPCBEError(f"미리보기 소스 파일이 존재하지 않습니다: {media_path}")

        command = [str(self.executable_path), "/new", "/open", str(media_path)]
        if start_time and start_time.strip():
            command.extend(["/startpos", start_time.strip()])
        return subprocess.Popen(command, cwd=self.executable_path.parent)

    def launch_embedded(
        self,
        host_hwnd: int,
        *,
        media_path: Path | None = None,
        width: int = 1280,
        height: int = 720,
        start_time: str | None = None,
    ) -> subprocess.Popen[str]:
        if not self._is_windows():
            raise MPCBEError("내장 MPC-BE 미리보기는 Windows에서만 지원됩니다.")

        if self._process is not None and self._process.poll() is None:
            self.shutdown()

        executable_path = self.ensure_runtime_installed()
        self._host_hwnd = int(host_hwnd)
        self._player_hwnd = None

        command = [
            str(executable_path),
            "/new",
            "/slave",
            str(int(host_hwnd)),
            "/nofocus",
            "/fixedsize",
            f"{max(int(width), 1)},{max(int(height), 1)}",
        ]
        if media_path is not None:
            if not media_path.exists():
                raise MPCBEError(f"미리보기 소스 파일이 존재하지 않습니다: {media_path}")
            command.extend(["/open", str(media_path)])
        if start_time and start_time.strip():
            command.extend(["/startpos", start_time.strip()])

        self._process = subprocess.Popen(command, cwd=executable_path.parent)
        return self._process

    def handle_api_message(
        self,
        *,
        sender_hwnd: int,
        command: int,
        payload: str,
        host_hwnd: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> MPCBEApiEvent:
        event = MPCBEApiEvent(
            command=command,
            name=COMMAND_NAMES.get(command, f"0x{command:X}"),
            payload=payload,
            sender_hwnd=sender_hwnd,
        )

        if command == CMD_CONNECT:
            self._player_hwnd = int(payload)
            event.player_hwnd = self._player_hwnd
            if host_hwnd is not None:
                self._host_hwnd = int(host_hwnd)
                self.attach_embedded_window(
                    host_hwnd=int(host_hwnd),
                    width=width or 1,
                    height=height or 1,
                )
            return event

        if command in {CMD_CURRENTPOSITION, CMD_NOTIFYSEEK} and payload.strip():
            seconds = float(payload.strip())
            event.position_seconds = seconds
            event.timecode = format_seconds_to_timecode(seconds)
            return event

        if command == CMD_DISCONNECT:
            self._player_hwnd = None
            return event

        return event

    def attach_embedded_window(self, *, host_hwnd: int, width: int, height: int) -> None:
        if not self._is_windows():
            raise MPCBEError("내장 MPC-BE 미리보기는 Windows에서만 지원됩니다.")
        if self._player_hwnd is None:
            return

        user32 = ctypes.windll.user32
        host_handle = wintypes.HWND(int(host_hwnd))
        player_handle = wintypes.HWND(int(self._player_hwnd))

        user32.SetParent(player_handle, host_handle)

        style = self._get_window_long_ptr(player_handle, GWL_STYLE)
        style |= WS_CHILD
        style &= ~(WS_CAPTION | WS_THICKFRAME | WS_BORDER | WS_DLGFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_POPUP)
        self._set_window_long_ptr(player_handle, GWL_STYLE, style)

        exstyle = self._get_window_long_ptr(player_handle, GWL_EXSTYLE)
        exstyle &= ~(WS_EX_APPWINDOW | WS_EX_WINDOWEDGE | WS_EX_CLIENTEDGE)
        self._set_window_long_ptr(player_handle, GWL_EXSTYLE, exstyle)

        user32.SetWindowPos(
            player_handle,
            None,
            0,
            0,
            max(int(width), 1),
            max(int(height), 1),
            SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
        user32.ShowWindow(player_handle, SW_SHOW)

    def resize_embedded_window(self, width: int, height: int) -> None:
        if not self._is_windows() or self._player_hwnd is None:
            return

        ctypes.windll.user32.SetWindowPos(
            wintypes.HWND(int(self._player_hwnd)),
            None,
            0,
            0,
            max(int(width), 1),
            max(int(height), 1),
            SWP_NOZORDER | SWP_NOACTIVATE,
        )

    def open_file(self, media_path: Path, *, sender_hwnd: int | None = None) -> None:
        if not media_path.exists():
            raise MPCBEError(f"미리보기 소스 파일이 존재하지 않습니다: {media_path}")
        self.send_command(CMD_OPENFILE, str(media_path), sender_hwnd=sender_hwnd)

    def play_pause(self, *, sender_hwnd: int | None = None) -> None:
        self.send_command(CMD_PLAYPAUSE, sender_hwnd=sender_hwnd)

    def play(self, *, sender_hwnd: int | None = None) -> None:
        self.send_command(CMD_PLAY, sender_hwnd=sender_hwnd)

    def pause(self, *, sender_hwnd: int | None = None) -> None:
        self.send_command(CMD_PAUSE, sender_hwnd=sender_hwnd)

    def stop(self, *, sender_hwnd: int | None = None) -> None:
        self.send_command(CMD_STOP, sender_hwnd=sender_hwnd)

    def close_file(self, *, sender_hwnd: int | None = None) -> None:
        self.send_command(CMD_CLOSEFILE, sender_hwnd=sender_hwnd)

    def request_current_position(self, *, sender_hwnd: int | None = None) -> None:
        self.send_command(CMD_GETCURRENTPOSITION, sender_hwnd=sender_hwnd)

    def seek(self, timecode: str | float, *, sender_hwnd: int | None = None) -> None:
        seconds = float(timecode) if isinstance(timecode, (float, int)) else parse_timecode_to_seconds(timecode)
        if seconds is None:
            raise MPCBEError("MPC-BE 탐색에는 비어 있지 않은 시점 값이 필요합니다.")
        self.send_command(CMD_SETPOSITION, f"{seconds:.3f}", sender_hwnd=sender_hwnd)

    def jump(self, seconds: int, *, sender_hwnd: int | None = None) -> None:
        self.send_command(CMD_JUMPOFNSECONDS, str(int(seconds)), sender_hwnd=sender_hwnd)

    def send_command(self, command: int, payload: str = "", *, sender_hwnd: int | None = None) -> None:
        if not self._is_windows():
            raise MPCBEError("MPC-BE API 제어는 Windows에서만 지원됩니다.")
        if self._player_hwnd is None:
            raise MPCBEError("아직 MPC-BE가 연결되지 않았습니다.")

        source_hwnd = int(sender_hwnd or self._host_hwnd or 0)
        text = payload or ""
        buffer = ctypes.create_unicode_buffer(text)
        copy_data = COPYDATASTRUCT()
        copy_data.dwData = command
        copy_data.cbData = ctypes.sizeof(buffer)
        copy_data.lpData = ctypes.cast(buffer, wintypes.LPVOID)
        ctypes.windll.user32.SendMessageW(
            wintypes.HWND(int(self._player_hwnd)),
            WM_COPYDATA,
            wintypes.WPARAM(source_hwnd),
            ctypes.byref(copy_data),
        )

    def shutdown(self) -> None:
        if not self._is_windows():
            return

        if self._player_hwnd is not None:
            ctypes.windll.user32.PostMessageW(wintypes.HWND(int(self._player_hwnd)), WM_CLOSE, 0, 0)

        if self._process is not None:
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.terminate()
            finally:
                self._process = None

        self._player_hwnd = None
        self._host_hwnd = None

    def _ensure_profile_exists(self) -> None:
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.profile_path.exists():
            self.profile_path.write_text("; YTUploader MPC-BE profile\n", encoding="utf-8")

    def _iter_runtime_source_dirs(self) -> list[Path]:
        if self.runtime_sources is not None:
            return [path for path in self.runtime_sources if path.exists()]

        candidates = [
            resource_path("bin", "mpc-be"),
            resource_path("bin"),
            Path(__file__).resolve().parent.parent / "bin" / "mpc-be",
            Path(__file__).resolve().parent.parent / "bin",
        ]

        executable_candidates = [
            resource_path("bin", "mpc-be64.exe"),
            Path(__file__).resolve().parent.parent / "bin" / "mpc-be64.exe",
        ]

        for env_name in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            raw_value = os.environ.get(env_name)
            if not raw_value:
                continue
            base_path = Path(raw_value)
            candidates.append(base_path / "MPC-BE x64")
            candidates.append(base_path / "MPC-BE")
            executable_candidates.append(base_path / "MPC-BE x64" / "mpc-be64.exe")
            executable_candidates.append(base_path / "MPC-BE" / "mpc-be64.exe")

        which_path = shutil.which("mpc-be64.exe") or shutil.which("mpc-be64")
        if which_path:
            executable_candidates.append(Path(which_path))

        discovered: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            if candidate in seen or not candidate.exists():
                continue
            if (candidate / "mpc-be64.exe").exists():
                discovered.append(candidate)
                seen.add(candidate)

        for executable_candidate in executable_candidates:
            if not executable_candidate.exists():
                continue
            candidate_dir = executable_candidate.parent
            if candidate_dir in seen:
                continue
            if (candidate_dir / "mpc-be64.exe").exists():
                discovered.append(candidate_dir)
                seen.add(candidate_dir)

        return discovered

    def _registry_settings_exist(self) -> bool:
        if winreg is None:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MPC_BE_REGISTRY_KEY):
                return True
        except OSError:
            return False

    def _read_registry_section(self, section: str, keys: list[str]) -> dict[str, str]:
        if winreg is None:
            return {}

        values: dict[str, str] = {}
        registry_path = fr"{MPC_BE_REGISTRY_KEY}\{section}"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path) as section_key:
                for key_name in keys:
                    try:
                        value, _ = winreg.QueryValueEx(section_key, key_name)
                    except OSError:
                        continue
                    values[key_name] = str(value)
        except OSError:
            return {}
        return values

    @staticmethod
    def _is_windows() -> bool:
        return hasattr(ctypes, "windll") and wintypes is not None

    @staticmethod
    def _get_window_long_ptr(hwnd, index: int) -> int:
        user32 = ctypes.windll.user32
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        return int(user32.GetWindowLongPtrW(hwnd, index))

    @staticmethod
    def _set_window_long_ptr(hwnd, index: int, value: int) -> int:
        user32 = ctypes.windll.user32
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        return int(user32.SetWindowLongPtrW(hwnd, index, value))

from __future__ import annotations

from pathlib import Path

from core.mpc_be import (
    CMD_CONNECT,
    CMD_CURRENTPOSITION,
    MPCBEController,
    format_seconds_to_timecode,
    parse_timecode_to_seconds,
)


def test_import_from_ini_copies_settings_file(tmp_path: Path) -> None:
    source_ini = tmp_path / "mpc-be64.ini"
    source_ini.write_text("[Settings]\nLanguage=kr\n", encoding="utf-8")
    target_ini = tmp_path / "runtime" / "ytuploader-mpc-be.ini"
    executable_path = tmp_path / "ytuploader-mpc-be.exe"
    executable_path.write_text("exe", encoding="utf-8")

    controller = MPCBEController(executable_path=executable_path, profile_path=target_ini)
    copied_path = controller.import_from_ini(source_ini)

    assert copied_path == target_ini
    assert target_ini.read_text(encoding="utf-8") == source_ini.read_text(encoding="utf-8")


def test_timecode_helpers_round_trip() -> None:
    seconds = parse_timecode_to_seconds("01:02:03.250")
    assert seconds == 3723.25
    assert format_seconds_to_timecode(seconds) == "01:02:03.250"


def test_ensure_runtime_installed_copies_and_renames_executable(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "mpc-be64.exe").write_text("exe", encoding="utf-8")
    (source_dir / "mpcresources.kr.dll").write_text("dll", encoding="utf-8")
    (source_dir / "mpc-be64.ini").write_text("[Settings]\nLanguage=kr\n", encoding="utf-8")

    runtime_dir = tmp_path / "runtime"
    controller = MPCBEController(
        runtime_dir=runtime_dir,
        profile_path=runtime_dir / "ytuploader-mpc-be.ini",
        executable_path=runtime_dir / "ytuploader-mpc-be.exe",
        runtime_sources=[source_dir],
    )

    executable_path = controller.ensure_runtime_installed()

    assert executable_path == runtime_dir / "ytuploader-mpc-be.exe"
    assert executable_path.read_text(encoding="utf-8") == "exe"
    assert (runtime_dir / "mpcresources.kr.dll").read_text(encoding="utf-8") == "dll"
    assert (runtime_dir / "ytuploader-mpc-be.ini").exists()
    assert not (runtime_dir / "mpc-be64.ini").exists()


def test_handle_api_message_reports_position_and_connection(tmp_path: Path) -> None:
    controller = MPCBEController(executable_path=tmp_path / "ytuploader-mpc-be.exe")
    controller.attach_embedded_window = lambda **_: None  # type: ignore[method-assign]

    connect_event = controller.handle_api_message(
        sender_hwnd=100,
        command=CMD_CONNECT,
        payload="200",
        host_hwnd=100,
        width=640,
        height=360,
    )
    position_event = controller.handle_api_message(
        sender_hwnd=200,
        command=CMD_CURRENTPOSITION,
        payload="83.125",
    )

    assert connect_event.player_hwnd == 200
    assert controller.player_hwnd == 200
    assert position_event.timecode == "00:01:23.125"

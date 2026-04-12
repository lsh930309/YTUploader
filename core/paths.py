from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

APP_NAME = "YTUploader"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MPC_BE_RUNTIME_EXE = "ytuploader-mpc-be.exe"
MPC_BE_RUNTIME_INI = "ytuploader-mpc-be.ini"
LOSSLESSCUT_RUNTIME_EXE = "LosslessCut.exe"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_project_root() -> Path:
    return PROJECT_ROOT


def get_bundle_root() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return PROJECT_ROOT


def resource_path(*parts: str) -> Path:
    return get_bundle_root().joinpath(*parts)


def get_local_appdata_base() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata)

    if os.name == "nt":
        return Path.home() / "AppData" / "Local"

    return Path.home() / ".local" / "share"


def get_user_data_root() -> Path:
    return get_local_appdata_base() / APP_NAME


def get_settings_path() -> Path:
    return get_user_data_root() / "settings.json"


def get_catalog_db_path() -> Path:
    return get_user_data_root() / "catalog.db"


def get_credentials_dir() -> Path:
    return get_user_data_root() / "credentials"


def get_client_secrets_path() -> Path:
    return get_credentials_dir() / "client_secrets.json"


def get_token_path() -> Path:
    return get_credentials_dir() / "token.json"


def get_temp_root() -> Path:
    return get_user_data_root() / "temp"


def get_logs_dir() -> Path:
    return get_user_data_root() / "logs"


def get_mpc_be_dir() -> Path:
    return get_user_data_root() / "mpc-be"


def get_mpc_be_runtime_dir() -> Path:
    return get_mpc_be_dir() / "runtime"


def get_losslesscut_dir() -> Path:
    return get_user_data_root() / "losslesscut"


def get_losslesscut_runtime_dir() -> Path:
    return get_losslesscut_dir() / "runtime"


def get_losslesscut_config_dir() -> Path:
    return get_losslesscut_dir() / "config"


def get_losslesscut_runtime_executable_path() -> Path:
    return get_losslesscut_runtime_dir() / LOSSLESSCUT_RUNTIME_EXE


def get_tool_runtime_dir() -> Path:
    return get_user_data_root() / "tools"


def get_tool_runtime_path(name: str) -> Path:
    executable_name = name if name.lower().endswith(".exe") else f"{name}.exe"
    return get_tool_runtime_dir() / executable_name


def get_ffmpeg_runtime_dir() -> Path:
    return get_tool_runtime_dir() / "ffmpeg"


def get_ffmpeg_runtime_binary_path(name: str) -> Path:
    executable_name = name if name.lower().endswith(".exe") else f"{name}.exe"
    return get_ffmpeg_runtime_dir() / "bin" / executable_name


def get_mkvtoolnix_runtime_dir() -> Path:
    return get_tool_runtime_dir() / "mkvtoolnix"


def get_mkvmerge_runtime_path() -> Path:
    return get_mkvtoolnix_runtime_dir() / "mkvmerge.exe"


def get_mpc_be_runtime_executable_path() -> Path:
    return get_mpc_be_runtime_dir() / MPC_BE_RUNTIME_EXE


def get_mpc_be_ini_path() -> Path:
    return get_mpc_be_runtime_dir() / MPC_BE_RUNTIME_INI


def get_icon_path() -> Path:
    return resource_path("assets", "app.ico")


def _find_losslesscut_bundled_binary(name: str) -> Path | None:
    executable_name = name if name.lower().endswith(".exe") else f"{name}.exe"
    runtime_dir = get_losslesscut_runtime_dir()
    candidates = [
        runtime_dir / executable_name,
        runtime_dir / "resources" / executable_name,
        runtime_dir / "resources" / "ffmpeg" / executable_name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    if runtime_dir.exists():
        for candidate in runtime_dir.rglob(executable_name):
            if candidate.is_file():
                return candidate
    return None


def binary_path(name: str) -> Path:
    normalized_name = name[:-4] if name.lower().endswith(".exe") else name
    executable_name = f"{normalized_name}.exe"
    candidates = [
        get_tool_runtime_path(executable_name),
        resource_path("bin", executable_name),
        get_project_root() / "bin" / executable_name,
    ]

    if normalized_name == "losslesscut":
        candidates.insert(0, get_losslesscut_runtime_executable_path())
    elif normalized_name in {"ffmpeg", "ffprobe", "ffplay"}:
        candidates.insert(0, get_ffmpeg_runtime_binary_path(normalized_name))
        losslesscut_binary = _find_losslesscut_bundled_binary(normalized_name)
        if losslesscut_binary is not None:
            candidates.insert(1, losslesscut_binary)
    elif normalized_name == "mkvmerge":
        candidates.insert(0, get_mkvmerge_runtime_path())

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def create_job_temp_dir() -> Path:
    timestamp = datetime.now().strftime("job-%Y%m%d-%H%M%S-%f")
    temp_dir = get_temp_root() / timestamp
    temp_dir.mkdir(parents=True, exist_ok=False)
    return temp_dir


def ensure_runtime_dirs() -> dict[str, Path]:
    directories = {
        "root": get_user_data_root(),
        "credentials": get_credentials_dir(),
        "temp": get_temp_root(),
        "logs": get_logs_dir(),
        "tools": get_tool_runtime_dir(),
        "ffmpeg_runtime": get_ffmpeg_runtime_dir(),
        "mkvtoolnix_runtime": get_mkvtoolnix_runtime_dir(),
        "losslesscut": get_losslesscut_dir(),
        "losslesscut_runtime": get_losslesscut_runtime_dir(),
        "losslesscut_config": get_losslesscut_config_dir(),
        "mpc_be": get_mpc_be_dir(),
        "mpc_be_runtime": get_mpc_be_runtime_dir(),
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    return directories

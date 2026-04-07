from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

APP_NAME = "YTUploader"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


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


def get_icon_path() -> Path:
    return resource_path("assets", "app.ico")


def binary_path(name: str) -> Path:
    executable_name = name if name.lower().endswith(".exe") else f"{name}.exe"
    candidates = [
        resource_path("bin", executable_name),
        get_project_root() / "bin" / executable_name,
    ]
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
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    return directories


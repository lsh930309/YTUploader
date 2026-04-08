from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DIST_NAME = "YTUploader"


def build_separator() -> str:
    return ";" if os.name == "nt" else ":"


def build_command() -> list[str]:
    separator = build_separator()
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        DIST_NAME,
        "--add-data",
        f"assets{separator}assets",
        "--hidden-import",
        "google_auth_oauthlib.flow",
        "--hidden-import",
        "googleapiclient.discovery",
    ]

    for executable in ("ffmpeg.exe", "ffprobe.exe", "mkvmerge.exe", "mpc-be64.exe"):
        candidate = PROJECT_ROOT / "bin" / executable
        if candidate.exists():
            command.extend(["--add-binary", f"{candidate}{separator}bin"])
        else:
            print(f"Warning: {candidate} not found and will not be packaged.")

    mpc_be_dir = PROJECT_ROOT / "bin" / "mpc-be"
    if mpc_be_dir.exists():
        command.extend(["--add-data", f"{mpc_be_dir}{separator}bin/mpc-be"])
    else:
        print(f"Warning: {mpc_be_dir} not found and will not be packaged.")

    command.append("main.py")
    return command


def main() -> int:
    command = build_command()
    print("Running:", " ".join(str(part) for part in command))
    completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

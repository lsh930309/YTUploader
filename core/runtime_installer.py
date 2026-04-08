# -*- coding: utf-8 -*-
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .mpc_be import MPCBEController, MPCBEError
from .paths import get_tool_runtime_dir, get_tool_runtime_path, resource_path

LogCallback = Optional[Callable[[str], None]]


@dataclass(slots=True)
class RuntimeSource:
    kind: str
    label: str
    paths: tuple[Path, ...]


@dataclass(slots=True)
class RuntimePackageStatus:
    package_id: str
    label: str
    installed: bool
    status_text: str
    source_kind: str = ""
    source_label: str = ""
    installed_paths: tuple[Path, ...] = ()


class RuntimeInstallError(RuntimeError):
    pass


class AppRuntimeInstaller:
    PACKAGE_IDS = ("ffmpeg", "mkvmerge", "mpc_be")
    PACKAGE_LABELS = {
        "ffmpeg": "FFmpeg / FFprobe",
        "mkvmerge": "MKVMerge",
        "mpc_be": "MPC-BE",
    }

    def __init__(
        self,
        *,
        tools_runtime_dir: Path | None = None,
        mpc_be_controller: MPCBEController | None = None,
    ) -> None:
        self.tools_runtime_dir = tools_runtime_dir or get_tool_runtime_dir()
        self.mpc_be_controller = mpc_be_controller or MPCBEController()

    def list_statuses(self) -> list[RuntimePackageStatus]:
        return [self.get_status(package_id) for package_id in self.PACKAGE_IDS]

    def get_status(self, package_id: str) -> RuntimePackageStatus:
        if package_id == "ffmpeg":
            return self._get_ffmpeg_status()
        if package_id == "mkvmerge":
            return self._get_mkvmerge_status()
        if package_id == "mpc_be":
            return self._get_mpc_be_status()
        raise RuntimeInstallError(f"지원하지 않는 런타임 패키지입니다: {package_id}")

    def install_package(
        self,
        package_id: str,
        *,
        log_callback: LogCallback = None,
    ) -> RuntimePackageStatus:
        if package_id == "ffmpeg":
            self._install_ffmpeg(log_callback=log_callback)
        elif package_id == "mkvmerge":
            self._install_mkvmerge(log_callback=log_callback)
        elif package_id == "mpc_be":
            self._install_mpc_be(log_callback=log_callback)
        else:
            raise RuntimeInstallError(f"지원하지 않는 런타임 패키지입니다: {package_id}")
        return self.get_status(package_id)

    def _get_ffmpeg_status(self) -> RuntimePackageStatus:
        installed_paths = (
            get_tool_runtime_path("ffmpeg"),
            get_tool_runtime_path("ffprobe"),
        )
        installed = all(path.exists() for path in installed_paths)
        source = self._resolve_ffmpeg_source()
        return RuntimePackageStatus(
            package_id="ffmpeg",
            label=self.PACKAGE_LABELS["ffmpeg"],
            installed=installed,
            status_text=self._build_status_text(
                installed=installed,
                source_kind=source.kind if source else "",
                allow_direct_use=True,
            ),
            source_kind=source.kind if source else "",
            source_label=source.label if source else "",
            installed_paths=installed_paths,
        )

    def _get_mkvmerge_status(self) -> RuntimePackageStatus:
        installed_paths = (get_tool_runtime_path("mkvmerge"),)
        installed = installed_paths[0].exists()
        source = self._resolve_single_executable_source("mkvmerge")
        return RuntimePackageStatus(
            package_id="mkvmerge",
            label=self.PACKAGE_LABELS["mkvmerge"],
            installed=installed,
            status_text=self._build_status_text(
                installed=installed,
                source_kind=source.kind if source else "",
                allow_direct_use=True,
            ),
            source_kind=source.kind if source else "",
            source_label=source.label if source else "",
            installed_paths=installed_paths,
        )

    def _get_mpc_be_status(self) -> RuntimePackageStatus:
        installed_paths = (self.mpc_be_controller.executable_path,)
        installed = self.mpc_be_controller.executable_path.exists()
        source_dirs = self.mpc_be_controller.discover_runtime_sources()
        source_label = str(source_dirs[0]) if source_dirs else ""
        return RuntimePackageStatus(
            package_id="mpc_be",
            label=self.PACKAGE_LABELS["mpc_be"],
            installed=installed,
            status_text=self._build_status_text(
                installed=installed,
                source_kind="mpc_be_source" if source_dirs else "",
                allow_direct_use=False,
            ),
            source_kind="mpc_be_source" if source_dirs else "",
            source_label=source_label,
            installed_paths=installed_paths,
        )

    def _install_ffmpeg(self, *, log_callback: LogCallback = None) -> None:
        source = self._resolve_ffmpeg_source()
        if source is None:
            raise RuntimeInstallError(
                "FFmpeg / FFprobe 설치 원본을 찾지 못했습니다. "
                "bin 폴더에 ffmpeg.exe와 ffprobe.exe를 넣거나 시스템 PATH를 확인해 주세요."
            )

        self.tools_runtime_dir.mkdir(parents=True, exist_ok=True)
        destinations = (
            get_tool_runtime_path("ffmpeg"),
            get_tool_runtime_path("ffprobe"),
        )
        self._emit_log(log_callback, f"{self.PACKAGE_LABELS['ffmpeg']} 설치를 시작합니다. 원본: {source.label}")
        for source_path, destination in zip(source.paths, destinations, strict=True):
            self._copy_file(source_path, destination)
            self._emit_log(log_callback, f"설치됨: {destination}")

    def _install_mkvmerge(self, *, log_callback: LogCallback = None) -> None:
        source = self._resolve_single_executable_source("mkvmerge")
        if source is None:
            raise RuntimeInstallError(
                "MKVMerge 설치 원본을 찾지 못했습니다. "
                "bin 폴더에 mkvmerge.exe를 넣거나 시스템 PATH를 확인해 주세요."
            )

        destination = get_tool_runtime_path("mkvmerge")
        self.tools_runtime_dir.mkdir(parents=True, exist_ok=True)
        self._emit_log(log_callback, f"{self.PACKAGE_LABELS['mkvmerge']} 설치를 시작합니다. 원본: {source.label}")
        self._copy_file(source.paths[0], destination)
        self._emit_log(log_callback, f"설치됨: {destination}")

    def _install_mpc_be(self, *, log_callback: LogCallback = None) -> None:
        self._emit_log(log_callback, "MPC-BE 설치를 시작합니다.")
        try:
            installed_path = self.mpc_be_controller.ensure_runtime_installed()
        except MPCBEError as exc:
            raise RuntimeInstallError(str(exc)) from exc
        self._emit_log(log_callback, f"설치됨: {installed_path}")

    def _resolve_ffmpeg_source(self) -> RuntimeSource | None:
        bundle_paths = (
            resource_path("bin", "ffmpeg.exe"),
            resource_path("bin", "ffprobe.exe"),
        )
        if all(path.exists() for path in bundle_paths):
            return RuntimeSource(kind="bundle", label="앱 번들", paths=bundle_paths)

        project_paths = (
            Path(__file__).resolve().parent.parent / "bin" / "ffmpeg.exe",
            Path(__file__).resolve().parent.parent / "bin" / "ffprobe.exe",
        )
        if all(path.exists() for path in project_paths):
            return RuntimeSource(kind="project", label="프로젝트 bin 폴더", paths=project_paths)

        ffmpeg_path = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")
        ffprobe_path = shutil.which("ffprobe.exe") or shutil.which("ffprobe")
        if ffmpeg_path and ffprobe_path:
            return RuntimeSource(
                kind="system",
                label="시스템 PATH",
                paths=(Path(ffmpeg_path), Path(ffprobe_path)),
            )
        return None

    def _resolve_single_executable_source(self, tool_name: str) -> RuntimeSource | None:
        executable_name = f"{tool_name}.exe"
        bundle_path = resource_path("bin", executable_name)
        if bundle_path.exists():
            return RuntimeSource(kind="bundle", label="앱 번들", paths=(bundle_path,))

        project_path = Path(__file__).resolve().parent.parent / "bin" / executable_name
        if project_path.exists():
            return RuntimeSource(kind="project", label="프로젝트 bin 폴더", paths=(project_path,))

        system_path = shutil.which(executable_name) or shutil.which(tool_name)
        if system_path:
            return RuntimeSource(kind="system", label="시스템 PATH", paths=(Path(system_path),))
        return None

    @staticmethod
    def _build_status_text(*, installed: bool, source_kind: str, allow_direct_use: bool) -> str:
        if installed:
            return "설치됨"
        if not source_kind:
            return "설치 원본 없음"
        if allow_direct_use and source_kind in {"bundle", "project"}:
            return "바로 사용 가능"
        return "설치 가능"

    @staticmethod
    def _copy_file(source_path: Path, destination_path: Path) -> None:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.exists() and destination_path.stat().st_size == source_path.stat().st_size:
            return
        shutil.copy2(source_path, destination_path)

    @staticmethod
    def _emit_log(callback: LogCallback, message: str) -> None:
        if callback:
            callback(message)

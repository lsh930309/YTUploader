# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from .losslesscut import LosslessCutController
from .mpc_be import MPCBEController, MPCBEError
from .paths import (
    get_ffmpeg_runtime_binary_path,
    get_ffmpeg_runtime_dir,
    get_mkvmerge_runtime_path,
    get_mkvtoolnix_runtime_dir,
    get_temp_root,
    get_tool_runtime_path,
    resource_path,
)

LogCallback = Optional[Callable[[str], None]]

USER_AGENT = "YTUploader/1.0 (+https://github.com/lsh930309/YTUploader)"
FFMPEG_RELEASE_ARCHIVE_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_RELEASE_CHECKSUM_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256"
MKVTOOLNIX_RELEASES_URL = "https://mkvtoolnix.download/windows/releases/"
MPC_BE_RELEASES_URL = "https://sourceforge.net/projects/mpcbe/files/MPC-BE/Release%20builds/"
LOSSLESSCUT_LATEST_RELEASE_API_URL = "https://api.github.com/repos/mifi/lossless-cut/releases/latest"


@dataclass(slots=True)
class RuntimeSource:
    kind: str
    label: str
    paths: tuple[Path, ...] = ()
    directory: Path | None = None


@dataclass(slots=True)
class RuntimePackageStatus:
    package_id: str
    label: str
    installed: bool
    status_text: str
    source_kind: str = ""
    source_label: str = ""
    installed_paths: tuple[Path, ...] = ()
    version: str = ""


@dataclass(slots=True)
class RemotePackageSpec:
    package_id: str
    label: str
    version: str
    download_url: str
    checksum_url: str | None
    checksum_algorithm: str | None
    checksum_target_name: str | None
    archive_kind: str
    source_label: str


class RuntimeInstallError(RuntimeError):
    pass


class AppRuntimeInstaller:
    PACKAGE_IDS = ("losslesscut", "mkvmerge", "mpc_be")
    OPTIONAL_PACKAGE_IDS = ("ffmpeg",)
    REQUIRED_PACKAGE_IDS = PACKAGE_IDS
    PACKAGE_LABELS = {
        "losslesscut": "LosslessCut",
        "ffmpeg": "FFmpeg / FFprobe",
        "mkvmerge": "MKVMerge",
        "mpc_be": "MPC-BE",
    }
    REMOTE_SOURCE_LABELS = {
        "losslesscut": "GitHub 공식 LosslessCut Windows 배포본",
        "ffmpeg": "ffmpeg.org가 안내하는 gyan.dev Windows 빌드",
        "mkvmerge": "mkvtoolnix.download 공식 Windows 배포본",
        "mpc_be": "SourceForge 공식 MPC-BE 배포본",
    }

    def __init__(
        self,
        *,
        losslesscut_controller: LosslessCutController | None = None,
        mpc_be_controller: MPCBEController | None = None,
    ) -> None:
        self.losslesscut_controller = losslesscut_controller or LosslessCutController()
        self.mpc_be_controller = mpc_be_controller or MPCBEController()

    def list_statuses(self) -> list[RuntimePackageStatus]:
        return [self.get_status(package_id) for package_id in self.PACKAGE_IDS]

    def get_required_statuses(self) -> list[RuntimePackageStatus]:
        return [self.get_status(package_id) for package_id in self.REQUIRED_PACKAGE_IDS]

    def is_ready(self) -> bool:
        return all(status.installed for status in self.get_required_statuses())

    def missing_required_package_ids(self) -> list[str]:
        return [status.package_id for status in self.get_required_statuses() if not status.installed]

    def get_status(self, package_id: str) -> RuntimePackageStatus:
        if package_id == "losslesscut":
            installed_paths = (self.losslesscut_controller.executable_path,)
            installed = installed_paths[0].exists()
            source = self._resolve_losslesscut_source()
            source_label = source.label if source else self.REMOTE_SOURCE_LABELS["losslesscut"]
            source_kind = source.kind if source else "remote"
            return RuntimePackageStatus(
                package_id="losslesscut",
                label=self.PACKAGE_LABELS["losslesscut"],
                installed=installed,
                status_text=self._build_status_text(installed=installed, source_kind=source_kind),
                source_kind=source_kind,
                source_label=source_label,
                installed_paths=installed_paths,
            )

        if package_id == "ffmpeg":
            installed_paths = (
                get_ffmpeg_runtime_binary_path("ffmpeg"),
                get_ffmpeg_runtime_binary_path("ffprobe"),
            )
            installed = all(path.exists() for path in installed_paths)
            source = self._resolve_ffmpeg_source()
            source_label = source.label if source else self.REMOTE_SOURCE_LABELS["ffmpeg"]
            source_kind = source.kind if source else "remote"
            return RuntimePackageStatus(
                package_id="ffmpeg",
                label=self.PACKAGE_LABELS["ffmpeg"],
                installed=installed,
                status_text=self._build_status_text(installed=installed, source_kind=source_kind),
                source_kind=source_kind,
                source_label=source_label,
                installed_paths=installed_paths,
            )

        if package_id == "mkvmerge":
            installed_paths = (get_mkvmerge_runtime_path(),)
            installed = installed_paths[0].exists()
            source = self._resolve_mkvmerge_source()
            source_label = source.label if source else self.REMOTE_SOURCE_LABELS["mkvmerge"]
            source_kind = source.kind if source else "remote"
            return RuntimePackageStatus(
                package_id="mkvmerge",
                label=self.PACKAGE_LABELS["mkvmerge"],
                installed=installed,
                status_text=self._build_status_text(installed=installed, source_kind=source_kind),
                source_kind=source_kind,
                source_label=source_label,
                installed_paths=installed_paths,
            )

        if package_id == "mpc_be":
            installed_paths = (self.mpc_be_controller.executable_path,)
            installed = self.mpc_be_controller.executable_path.exists()
            source_dirs = self.mpc_be_controller.discover_runtime_sources()
            if source_dirs:
                source_kind = "local"
                source_label = str(source_dirs[0])
            else:
                source_kind = "remote"
                source_label = self.REMOTE_SOURCE_LABELS["mpc_be"]
            return RuntimePackageStatus(
                package_id="mpc_be",
                label=self.PACKAGE_LABELS["mpc_be"],
                installed=installed,
                status_text=self._build_status_text(installed=installed, source_kind=source_kind),
                source_kind=source_kind,
                source_label=source_label,
                installed_paths=installed_paths,
            )

        raise RuntimeInstallError(f"지원하지 않는 런타임 패키지입니다: {package_id}")

    def install_package(
        self,
        package_id: str,
        *,
        log_callback: LogCallback = None,
    ) -> RuntimePackageStatus:
        if package_id == "losslesscut":
            self._install_losslesscut(log_callback=log_callback)
        elif package_id == "ffmpeg":
            self._install_ffmpeg(log_callback=log_callback)
        elif package_id == "mkvmerge":
            self._install_mkvmerge(log_callback=log_callback)
        elif package_id == "mpc_be":
            self._install_mpc_be(log_callback=log_callback)
        else:
            raise RuntimeInstallError(f"지원하지 않는 런타임 패키지입니다: {package_id}")
        return self.get_status(package_id)

    def _install_losslesscut(self, *, log_callback: LogCallback = None) -> None:
        source = self._resolve_losslesscut_source()
        remote_error: Exception | None = None

        try:
            spec = self._resolve_remote_losslesscut_spec()
            self._emit_log(log_callback, f"{spec.label} 최신 버전을 공식 웹에서 내려받습니다. ({spec.version})")
            self._install_losslesscut_from_remote(spec, log_callback=log_callback)
            return
        except Exception as exc:
            remote_error = exc
            self._emit_log(log_callback, f"공식 웹 설치에 실패했습니다: {exc}")

        if source is not None:
            self._emit_log(log_callback, f"로컬 원본으로 대체 설치를 진행합니다: {source.label}")
            self._install_losslesscut_from_local(source, log_callback=log_callback)
            return

        raise RuntimeInstallError(
            "LosslessCut 공식 웹 설치에 실패했습니다. 네트워크 상태를 확인하거나 나중에 다시 시도해 주세요."
        ) from remote_error

    def _install_ffmpeg(self, *, log_callback: LogCallback = None) -> None:
        source = self._resolve_ffmpeg_source()
        remote_error: Exception | None = None

        try:
            spec = self._resolve_remote_ffmpeg_spec()
            self._emit_log(log_callback, f"{spec.label} 최신 버전을 공식 웹에서 내려받습니다. ({spec.version})")
            self._install_ffmpeg_from_remote(spec, log_callback=log_callback)
            return
        except Exception as exc:
            remote_error = exc
            self._emit_log(log_callback, f"공식 웹 설치에 실패했습니다: {exc}")

        if source is not None:
            self._emit_log(log_callback, f"로컬 원본으로 대체 설치를 진행합니다: {source.label}")
            self._install_ffmpeg_from_local(source, log_callback=log_callback)
            return

        raise RuntimeInstallError(
            "FFmpeg / FFprobe 공식 웹 설치에 실패했습니다. 네트워크 상태를 확인하거나 나중에 다시 시도해 주세요."
        ) from remote_error

    def _install_mkvmerge(self, *, log_callback: LogCallback = None) -> None:
        source = self._resolve_mkvmerge_source()
        remote_error: Exception | None = None

        try:
            spec = self._resolve_remote_mkvmerge_spec()
            self._emit_log(log_callback, f"{spec.label} 최신 버전을 공식 웹에서 내려받습니다. ({spec.version})")
            self._install_mkvmerge_from_remote(spec, log_callback=log_callback)
            return
        except Exception as exc:
            remote_error = exc
            self._emit_log(log_callback, f"공식 웹 설치에 실패했습니다: {exc}")

        if source is not None:
            self._emit_log(log_callback, f"로컬 원본으로 대체 설치를 진행합니다: {source.label}")
            self._install_mkvmerge_from_local(source, log_callback=log_callback)
            return

        raise RuntimeInstallError(
            "MKVMerge 공식 웹 설치에 실패했습니다. 네트워크 상태를 확인하거나 나중에 다시 시도해 주세요."
        ) from remote_error

    def _install_mpc_be(self, *, log_callback: LogCallback = None) -> None:
        local_sources = self.mpc_be_controller.discover_runtime_sources()
        remote_error: Exception | None = None

        try:
            spec = self._resolve_remote_mpc_be_spec()
            self._emit_log(log_callback, f"{spec.label} 최신 버전을 공식 웹에서 내려받습니다. ({spec.version})")
            self._install_mpc_be_from_remote(spec, log_callback=log_callback)
            return
        except Exception as exc:
            remote_error = exc
            self._emit_log(log_callback, f"공식 웹 설치에 실패했습니다: {exc}")

        if local_sources:
            self._emit_log(log_callback, f"로컬 원본으로 대체 설치를 진행합니다: {local_sources[0]}")
            try:
                self.mpc_be_controller.ensure_runtime_installed()
                self._emit_log(log_callback, f"설치됨: {self.mpc_be_controller.executable_path}")
                return
            except MPCBEError as exc:
                raise RuntimeInstallError(str(exc)) from exc

        raise RuntimeInstallError(
            "MPC-BE 공식 웹 설치에 실패했습니다. 네트워크 상태를 확인하거나 나중에 다시 시도해 주세요."
        ) from remote_error

    def _install_losslesscut_from_remote(self, spec: RemotePackageSpec, *, log_callback: LogCallback = None) -> None:
        with self._temporary_work_dir("losslesscut") as work_dir_text:
            work_dir = Path(work_dir_text)
            archive_path = work_dir / f"LosslessCut-{spec.version}.7z"
            self._download_file(spec.download_url, archive_path, log_callback=log_callback)
            expected = self._read_checksum(spec, log_callback=log_callback)
            if expected is not None and spec.checksum_algorithm is not None:
                self._verify_file_checksum(archive_path, expected, spec.checksum_algorithm)

            extract_dir = work_dir / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                ["tar", "-xf", str(archive_path), "-C", str(extract_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeInstallError(
                    f"LosslessCut 압축 해제에 실패했습니다.\n{completed.stdout}\n{completed.stderr}".strip()
                )

            source_dir = self._find_required_file(extract_dir, "LosslessCut.exe").parent
            installed_path = self.losslesscut_controller.install_from_source_dir(source_dir)
            if installed_path is None:
                raise RuntimeInstallError("압축 해제된 LosslessCut 폴더에서 실행 파일을 찾지 못했습니다.")
            self._emit_log(log_callback, f"설치됨: {installed_path}")

    def _install_ffmpeg_from_remote(self, spec: RemotePackageSpec, *, log_callback: LogCallback = None) -> None:
        with self._temporary_work_dir("ffmpeg") as work_dir_text:
            work_dir = Path(work_dir_text)
            archive_path = work_dir / "ffmpeg-release-essentials.zip"
            self._download_file(spec.download_url, archive_path, log_callback=log_callback)
            expected = self._read_checksum(spec, log_callback=log_callback)
            if expected is None or spec.checksum_algorithm is None:
                raise RuntimeInstallError("FFmpeg 체크섬 정보가 없어 설치를 계속할 수 없습니다.")
            self._verify_file_checksum(archive_path, expected, spec.checksum_algorithm)

            extract_dir = work_dir / "extract"
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(extract_dir)

            ffmpeg_path = self._find_required_file(extract_dir, "ffmpeg.exe")
            ffprobe_path = self._find_required_file(extract_dir, "ffprobe.exe")
            destination_dir = get_ffmpeg_runtime_dir() / "bin"
            destination_dir.mkdir(parents=True, exist_ok=True)
            self._copy_file(ffmpeg_path, destination_dir / "ffmpeg.exe")
            self._copy_file(ffprobe_path, destination_dir / "ffprobe.exe")
            self._emit_log(log_callback, f"설치됨: {destination_dir / 'ffmpeg.exe'}")
            self._emit_log(log_callback, f"설치됨: {destination_dir / 'ffprobe.exe'}")

    def _install_mkvmerge_from_remote(self, spec: RemotePackageSpec, *, log_callback: LogCallback = None) -> None:
        with self._temporary_work_dir("mkvmerge") as work_dir_text:
            work_dir = Path(work_dir_text)
            archive_path = work_dir / f"mkvtoolnix-{spec.version}.zip"
            self._download_file(spec.download_url, archive_path, log_callback=log_callback)
            expected = self._read_checksum(spec, log_callback=log_callback)
            if expected is None or spec.checksum_algorithm is None:
                raise RuntimeInstallError("MKVToolNix 체크섬 정보가 없어 설치를 계속할 수 없습니다.")
            self._verify_file_checksum(archive_path, expected, spec.checksum_algorithm)

            extract_dir = work_dir / "extract"
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(extract_dir)

            package_root = self._find_required_file(extract_dir, "mkvmerge.exe").parent
            self._replace_directory(package_root, get_mkvtoolnix_runtime_dir())
            self._emit_log(log_callback, f"설치됨: {get_mkvmerge_runtime_path()}")

    def _install_mpc_be_from_remote(self, spec: RemotePackageSpec, *, log_callback: LogCallback = None) -> None:
        with self._temporary_work_dir("mpc_be") as work_dir_text:
            work_dir = Path(work_dir_text)
            archive_path = work_dir / f"MPC-BE.{spec.version}.x64.7z"
            self._download_file(spec.download_url, archive_path, log_callback=log_callback)
            expected = self._read_checksum(spec, log_callback=log_callback)
            if expected is None or spec.checksum_algorithm is None:
                raise RuntimeInstallError("MPC-BE 체크섬 정보가 없어 설치를 계속할 수 없습니다.")
            self._verify_file_checksum(archive_path, expected, spec.checksum_algorithm)

            extract_dir = work_dir / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                ["tar", "-xf", str(archive_path), "-C", str(extract_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeInstallError(
                    f"MPC-BE 압축 해제에 실패했습니다.\n{completed.stdout}\n{completed.stderr}".strip()
                )

            source_dir = self._find_required_file(extract_dir, "mpc-be64.exe").parent
            installed_path = self.mpc_be_controller.install_from_source_dir(source_dir)
            if installed_path is None:
                raise RuntimeInstallError("압축 해제된 MPC-BE 폴더에서 실행 파일을 찾지 못했습니다.")
            self._emit_log(log_callback, f"설치됨: {installed_path}")

    def _install_losslesscut_from_local(self, source: RuntimeSource, *, log_callback: LogCallback = None) -> None:
        source_dir = source.directory
        if source_dir is None and source.paths:
            source_dir = source.paths[0].parent
        if source_dir is None:
            raise RuntimeInstallError("로컬 LosslessCut 원본을 찾지 못했습니다.")
        installed_path = self.losslesscut_controller.install_from_source_dir(source_dir)
        if installed_path is None:
            raise RuntimeInstallError("로컬 LosslessCut 원본에서 실행 파일을 찾지 못했습니다.")
        self._emit_log(log_callback, f"설치됨: {installed_path}")

    def _install_ffmpeg_from_local(self, source: RuntimeSource, *, log_callback: LogCallback = None) -> None:
        assert len(source.paths) >= 2
        destination_dir = get_ffmpeg_runtime_dir() / "bin"
        destination_dir.mkdir(parents=True, exist_ok=True)
        self._copy_file(source.paths[0], destination_dir / "ffmpeg.exe")
        self._copy_file(source.paths[1], destination_dir / "ffprobe.exe")
        self._emit_log(log_callback, f"설치됨: {destination_dir / 'ffmpeg.exe'}")
        self._emit_log(log_callback, f"설치됨: {destination_dir / 'ffprobe.exe'}")

    def _install_mkvmerge_from_local(self, source: RuntimeSource, *, log_callback: LogCallback = None) -> None:
        if source.directory is not None:
            self._replace_directory(source.directory, get_mkvtoolnix_runtime_dir())
        elif source.paths:
            destination = get_tool_runtime_path("mkvmerge")
            self._copy_file(source.paths[0], destination)
        else:
            raise RuntimeInstallError("로컬 MKVMerge 원본을 찾지 못했습니다.")
        installed_path = get_mkvmerge_runtime_path() if get_mkvmerge_runtime_path().exists() else get_tool_runtime_path("mkvmerge")
        self._emit_log(log_callback, f"설치됨: {installed_path}")

    def _resolve_losslesscut_source(self) -> RuntimeSource | None:
        bundle_dir = resource_path("bin", "losslesscut")
        if (bundle_dir / "LosslessCut.exe").exists():
            return RuntimeSource(kind="bundle_dir", label="앱 번들", directory=bundle_dir)

        project_dir = Path(__file__).resolve().parent.parent / "bin" / "losslesscut"
        if (project_dir / "LosslessCut.exe").exists():
            return RuntimeSource(kind="project_dir", label="프로젝트 bin 폴더", directory=project_dir)

        bundle_path = resource_path("bin", "LosslessCut.exe")
        if bundle_path.exists():
            return RuntimeSource(kind="bundle", label="앱 번들", paths=(bundle_path,), directory=bundle_path.parent)

        project_path = Path(__file__).resolve().parent.parent / "bin" / "LosslessCut.exe"
        if project_path.exists():
            return RuntimeSource(
                kind="project",
                label="프로젝트 bin 폴더",
                paths=(project_path,),
                directory=project_path.parent,
            )

        system_path = shutil.which("LosslessCut.exe") or shutil.which("LosslessCut")
        if system_path:
            system_dir = Path(system_path).parent
            if (system_dir / "LosslessCut.exe").exists():
                return RuntimeSource(kind="system", label="시스템 PATH", directory=system_dir)
        return None

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
            return RuntimeSource(kind="system", label="시스템 PATH", paths=(Path(ffmpeg_path), Path(ffprobe_path)))
        return None

    def _resolve_mkvmerge_source(self) -> RuntimeSource | None:
        bundle_dir = resource_path("bin", "mkvtoolnix")
        if (bundle_dir / "mkvmerge.exe").exists():
            return RuntimeSource(kind="bundle_dir", label="앱 번들", directory=bundle_dir)

        project_dir = Path(__file__).resolve().parent.parent / "bin" / "mkvtoolnix"
        if (project_dir / "mkvmerge.exe").exists():
            return RuntimeSource(kind="project_dir", label="프로젝트 bin 폴더", directory=project_dir)

        bundle_path = resource_path("bin", "mkvmerge.exe")
        if bundle_path.exists():
            return RuntimeSource(kind="bundle", label="앱 번들", paths=(bundle_path,))

        project_path = Path(__file__).resolve().parent.parent / "bin" / "mkvmerge.exe"
        if project_path.exists():
            return RuntimeSource(kind="project", label="프로젝트 bin 폴더", paths=(project_path,))

        system_path = shutil.which("mkvmerge.exe") or shutil.which("mkvmerge")
        if system_path:
            system_dir = Path(system_path).parent
            if (system_dir / "mkvmerge.exe").exists():
                return RuntimeSource(kind="system", label="시스템 PATH", directory=system_dir)
        return None

    def _resolve_remote_losslesscut_spec(self) -> RemotePackageSpec:
        payload = json.loads(self._download_text(LOSSLESSCUT_LATEST_RELEASE_API_URL))
        assets = payload.get("assets", [])
        asset = next((item for item in assets if item.get("name") == "LosslessCut-win-x64.7z"), None)
        if asset is None or not asset.get("browser_download_url"):
            raise RuntimeInstallError("최신 LosslessCut 릴리스에서 Windows 7z 자산을 찾지 못했습니다.")
        version = str(payload.get("name") or payload.get("tag_name") or "latest").lstrip("v")
        return RemotePackageSpec(
            package_id="losslesscut",
            label=self.PACKAGE_LABELS["losslesscut"],
            version=version,
            download_url=str(asset["browser_download_url"]),
            checksum_url=None,
            checksum_algorithm=None,
            checksum_target_name=None,
            archive_kind="7z",
            source_label=self.REMOTE_SOURCE_LABELS["losslesscut"],
        )

    def _resolve_remote_ffmpeg_spec(self) -> RemotePackageSpec:
        version_text = self._download_text("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.ver").strip()
        version = version_text or "latest"
        return RemotePackageSpec(
            package_id="ffmpeg",
            label=self.PACKAGE_LABELS["ffmpeg"],
            version=version,
            download_url=FFMPEG_RELEASE_ARCHIVE_URL,
            checksum_url=FFMPEG_RELEASE_CHECKSUM_URL,
            checksum_algorithm="sha256",
            checksum_target_name=None,
            archive_kind="zip",
            source_label=self.REMOTE_SOURCE_LABELS["ffmpeg"],
        )

    def _resolve_remote_mkvmerge_spec(self) -> RemotePackageSpec:
        page = self._download_text(MKVTOOLNIX_RELEASES_URL)
        versions = re.findall(r'href="/windows/releases/([0-9][0-9.]*)/"', page)
        version = self._pick_latest_version(versions)
        target_name = f"mkvtoolnix-64-bit-{version}.zip"
        base_url = f"https://mkvtoolnix.download/windows/releases/{version}"
        return RemotePackageSpec(
            package_id="mkvmerge",
            label=self.PACKAGE_LABELS["mkvmerge"],
            version=version,
            download_url=f"{base_url}/{target_name}",
            checksum_url=f"{base_url}/sha256sums.txt",
            checksum_algorithm="sha256",
            checksum_target_name=target_name,
            archive_kind="zip",
            source_label=self.REMOTE_SOURCE_LABELS["mkvmerge"],
        )

    def _resolve_remote_mpc_be_spec(self) -> RemotePackageSpec:
        page = self._download_text(MPC_BE_RELEASES_URL)
        versions = re.findall(r"/MPC-BE/Release%20builds/([0-9][0-9.]*)/", page)
        version = self._pick_latest_version(versions)
        archive_name = f"MPC-BE.{version}.x64.7z"
        checksum_name = f"mpc-be.{version}.checksums.sha"
        archive_url = (
            f"https://sourceforge.net/projects/mpcbe/files/MPC-BE/Release%20builds/{version}/{archive_name}/download"
        )
        checksum_url = (
            f"https://sourceforge.net/projects/mpcbe/files/MPC-BE/Release%20builds/{version}/{checksum_name}/download"
        )
        return RemotePackageSpec(
            package_id="mpc_be",
            label=self.PACKAGE_LABELS["mpc_be"],
            version=version,
            download_url=archive_url,
            checksum_url=checksum_url,
            checksum_algorithm="sha1",
            checksum_target_name=archive_name,
            archive_kind="7z",
            source_label=self.REMOTE_SOURCE_LABELS["mpc_be"],
        )

    def _read_checksum(self, spec: RemotePackageSpec, *, log_callback: LogCallback = None) -> str | None:
        if spec.checksum_url is None:
            return None
        self._emit_log(log_callback, "체크섬 정보를 확인합니다.")
        text = self._download_text(spec.checksum_url)
        return self._parse_checksum_text(
            text,
            algorithm=spec.checksum_algorithm or "",
            target_name=spec.checksum_target_name,
        )

    def _download_text(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8", "replace")
        except URLError as exc:
            raise RuntimeInstallError(f"웹 페이지를 가져오지 못했습니다: {url}") from exc

    def _download_file(self, url: str, destination: Path, *, log_callback: LogCallback = None) -> Path:
        self._emit_log(log_callback, f"다운로드 시작: {url}")
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=120) as response, destination.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
        except URLError as exc:
            raise RuntimeInstallError(f"다운로드에 실패했습니다: {url}") from exc
        self._emit_log(log_callback, f"다운로드 완료: {destination.name}")
        return destination

    @staticmethod
    def _parse_checksum_text(text: str, *, algorithm: str, target_name: str | None) -> str:
        del algorithm
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if target_name is None:
            if len(lines) != 1:
                raise RuntimeInstallError("체크섬 파일 형식을 해석하지 못했습니다.")
            return lines[0].split()[0].strip()

        for line in lines:
            parts = line.split()
            if len(parts) < 2:
                continue
            candidate_name = parts[-1].lstrip("*")
            if candidate_name == target_name:
                return parts[0].strip()

        raise RuntimeInstallError(f"체크섬 파일에서 대상 파일을 찾지 못했습니다: {target_name}")

    @staticmethod
    def _verify_file_checksum(file_path: Path, expected_checksum: str, algorithm: str) -> None:
        digest = hashlib.new(algorithm)
        with file_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        actual = digest.hexdigest().lower()
        expected = expected_checksum.strip().lower()
        if actual != expected:
            raise RuntimeInstallError(
                f"체크섬 검증에 실패했습니다: {file_path.name}\n예상: {expected}\n실제: {actual}"
            )

    @staticmethod
    def _find_required_file(root_dir: Path, filename: str) -> Path:
        for candidate in root_dir.rglob(filename):
            if candidate.is_file():
                return candidate
        raise RuntimeInstallError(f"압축 해제 결과에서 필요한 파일을 찾지 못했습니다: {filename}")

    @staticmethod
    def _replace_directory(source_dir: Path, destination_dir: Path) -> None:
        if destination_dir.exists():
            shutil.rmtree(destination_dir)
        destination_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, destination_dir)

    @staticmethod
    def _copy_file(source_path: Path, destination_path: Path) -> None:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.exists() and destination_path.stat().st_size == source_path.stat().st_size:
            return
        shutil.copy2(source_path, destination_path)

    @staticmethod
    def _pick_latest_version(versions: list[str]) -> str:
        unique_versions = sorted({version for version in versions if version}, key=AppRuntimeInstaller._version_key)
        if not unique_versions:
            raise RuntimeInstallError("최신 버전 정보를 찾지 못했습니다.")
        return unique_versions[-1]

    @staticmethod
    def _version_key(version: str) -> tuple[int, ...]:
        return tuple(int(part) for part in version.split("."))

    @staticmethod
    def _build_status_text(*, installed: bool, source_kind: str) -> str:
        if installed:
            return "설치됨"
        if source_kind == "remote":
            return "공식 웹 설치 가능"
        return "로컬 설치 가능"

    @staticmethod
    def _emit_log(callback: LogCallback, message: str) -> None:
        if callback:
            callback(message)

    @staticmethod
    def _temporary_work_dir(prefix: str):
        base_dir = get_temp_root()
        base_dir.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix=f"{prefix}-", dir=base_dir)

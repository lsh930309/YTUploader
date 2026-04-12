from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .paths import (
    binary_path,
    get_losslesscut_config_dir,
    get_losslesscut_runtime_dir,
    get_losslesscut_runtime_executable_path,
    resource_path,
)

if TYPE_CHECKING:
    from .video_processor import ClipJob

LogCallback = Optional[Callable[[str], None]]

HTTP_API_HOST = "127.0.0.1"


class LosslessCutError(RuntimeError):
    pass


class LosslessCutExportError(LosslessCutError):
    pass


def parse_losslesscut_timecode(value: str | None) -> float | None:
    if value is None:
        return None

    text = value.strip()
    if not text:
        return None

    if ":" not in text:
        return float(text)

    parts = [part.strip() for part in text.split(":")]
    if len(parts) > 3:
        raise LosslessCutError(f"지원하지 않는 시점 형식입니다: {value}")

    total = 0.0
    multiplier = 1.0
    for part in reversed(parts):
        total += float(part) * multiplier
        multiplier *= 60.0
    return total


def build_project_payload(source_path: Path, clips: list[ClipJob], media_duration: float) -> dict[str, object]:
    cut_segments: list[dict[str, object]] = []
    for clip in clips:
        start_seconds = float(parse_losslesscut_timecode(clip.start_time) or 0.0)
        end_seconds = parse_losslesscut_timecode(clip.end_time)
        cut_segments.append(
            {
                "start": start_seconds,
                "end": float(end_seconds if end_seconds is not None else media_duration),
                "name": clip.clip_name,
                "selected": True,
            }
        )
    return {
        "version": 2,
        "mediaFileName": source_path.name,
        "cutSegments": cut_segments,
    }


class LosslessCutController:
    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        executable_path: Path | None = None,
        config_dir: Path | None = None,
        runtime_sources: list[Path] | None = None,
        startup_timeout: float = 30.0,
        export_timeout: float = 600.0,
        project_load_delay: float = 2.0,
    ) -> None:
        self.runtime_dir = runtime_dir or get_losslesscut_runtime_dir()
        self.executable_path = executable_path or get_losslesscut_runtime_executable_path()
        self.config_dir = config_dir or get_losslesscut_config_dir()
        self.runtime_sources = runtime_sources
        self.startup_timeout = startup_timeout
        self.export_timeout = export_timeout
        self.project_load_delay = project_load_delay
        self._process: subprocess.Popen[bytes] | None = None

    def discover_runtime_sources(self) -> list[Path]:
        return self._iter_runtime_source_dirs()

    def ensure_runtime_installed(self) -> Path:
        if self.executable_path.exists():
            return self.executable_path

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        for source_dir in self._iter_runtime_source_dirs():
            installed = self.install_from_source_dir(source_dir)
            if installed is not None:
                return installed

        raise LosslessCutError(
            "LosslessCut 런타임을 찾지 못했습니다. "
            "bin/losslesscut 폴더를 패키징하거나, 먼저 준비 마법사에서 설치를 완료해 주세요."
        )

    def install_from_source_dir(self, source_dir: Path) -> Path | None:
        source_executable = self._find_executable(source_dir)
        if source_executable is None:
            return None

        self._replace_directory(source_executable.parent, self.runtime_dir)
        installed_executable = self._find_executable(self.runtime_dir)
        if installed_executable is None:
            raise LosslessCutError("LosslessCut 런타임 복사 후 실행 파일을 찾지 못했습니다.")

        if installed_executable != self.executable_path:
            self.executable_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(installed_executable), str(self.executable_path))
        return self.executable_path

    def export_clips(
        self,
        *,
        source_path: Path,
        clips: list[ClipJob],
        log_callback: LogCallback = None,
    ) -> list[Path]:
        if not source_path.exists():
            raise LosslessCutExportError(f"LosslessCut에 전달할 소스 파일이 존재하지 않습니다: {source_path}")
        if not clips:
            raise LosslessCutExportError("LosslessCut에 전달할 클립이 없습니다.")

        executable_path = self.ensure_runtime_installed()
        media_duration = self._probe_duration(source_path)
        project_path = self._write_project_file(source_path, clips, media_duration)
        port = self._pick_free_port()
        command = [
            str(executable_path),
            "--http-api",
            str(port),
            "--disable-networking",
            "--config-dir",
            str(self.config_dir),
            "--settings-json",
            json.dumps(self._build_settings_override(), ensure_ascii=True),
        ]
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self._emit_log(log_callback, f"LosslessCut 런타임을 시작합니다: {executable_path}")
        self._process = subprocess.Popen(
            command,
            cwd=executable_path.parent,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            self._wait_for_server(port)
            self._call_action(port, "openFiles", [str(project_path)])
            time.sleep(self.project_load_delay)

            event_result: dict[str, object] = {}
            event_error: list[Exception] = []

            def wait_for_export_complete() -> None:
                try:
                    event_result.update(self._await_event(port, "export-complete"))
                except Exception as exc:  # pragma: no cover - exercised via integration
                    event_error.append(exc)

            waiter = threading.Thread(target=wait_for_export_complete, daemon=True)
            waiter.start()
            self._emit_log(log_callback, "LosslessCut export를 요청합니다.")
            self._call_action(port, "export")
            waiter.join(timeout=self.export_timeout)
            if waiter.is_alive():
                raise LosslessCutExportError("LosslessCut export 완료를 기다리다 시간 초과가 발생했습니다.")
            if event_error:
                raise LosslessCutExportError(str(event_error[0])) from event_error[0]

            raw_paths = event_result.get("paths")
            if not isinstance(raw_paths, list) or not raw_paths:
                raise LosslessCutExportError("LosslessCut export 결과에서 출력 파일 목록을 받지 못했습니다.")

            exported_paths = [Path(str(path)) for path in raw_paths]
            if len(exported_paths) != len(clips):
                raise LosslessCutExportError(
                    f"LosslessCut export 결과 수가 예상과 다릅니다. 예상: {len(clips)}, 실제: {len(exported_paths)}"
                )

            finalized_paths: list[Path] = []
            for clip, exported_path in zip(clips, exported_paths, strict=True):
                final_path = clip.output_mp4
                final_path.parent.mkdir(parents=True, exist_ok=True)
                if exported_path.resolve() != final_path.resolve():
                    if final_path.exists():
                        final_path.unlink()
                    shutil.move(str(exported_path), str(final_path))
                finalized_paths.append(final_path)
                self._emit_log(log_callback, f"LosslessCut export 완료: {final_path.name}")
            return finalized_paths
        finally:
            try:
                project_path.unlink(missing_ok=True)
            finally:
                self.shutdown()

    def shutdown(self) -> None:
        if self._process is None:
            return

        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        self._process = None

    def _write_project_file(self, source_path: Path, clips: list[ClipJob], media_duration: float) -> Path:
        source_dir = source_path.parent
        source_dir.mkdir(parents=True, exist_ok=True)
        project_path = source_dir / f".{source_path.stem}.ytuploader-{uuid.uuid4().hex}.llc"
        payload = build_project_payload(source_path, clips, media_duration)
        try:
            project_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            raise LosslessCutExportError(
                f"LosslessCut 프로젝트 파일을 저장하지 못했습니다: {project_path}"
            ) from exc
        return project_path

    def _wait_for_server(self, port: int) -> None:
        deadline = time.time() + self.startup_timeout
        url = f"http://{HTTP_API_HOST}:{port}/"
        while time.time() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise LosslessCutExportError("LosslessCut가 HTTP API 준비 전에 종료되었습니다.")
            try:
                with urlopen(Request(url, method="GET"), timeout=1):
                    return
            except HTTPError:
                return
            except URLError:
                time.sleep(0.25)
        raise LosslessCutExportError("LosslessCut HTTP API가 준비되지 않았습니다.")

    def _call_action(self, port: int, action: str, payload: object | None = None) -> dict[str, object]:
        url = f"http://{HTTP_API_HOST}:{port}/api/action/{action}"
        return self._request_json(url, payload=payload)

    def _await_event(self, port: int, event_name: str) -> dict[str, object]:
        url = f"http://{HTTP_API_HOST}:{port}/api/await-event/{event_name}"
        return self._request_json(url, payload=None)

    @staticmethod
    def _request_json(url: str, payload: object | None) -> dict[str, object]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=data if data is not None else b"",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8", "replace").strip()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace").strip()
            raise LosslessCutExportError(f"LosslessCut API 요청에 실패했습니다: {exc.code} {body}".strip()) from exc
        except URLError as exc:
            raise LosslessCutExportError(f"LosslessCut API 연결에 실패했습니다: {url}") from exc

        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise LosslessCutExportError(f"LosslessCut API 응답 형식이 올바르지 않습니다: {raw}")
        return parsed

    @staticmethod
    def _pick_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((HTTP_API_HOST, 0))
            sock.listen(1)
            return int(sock.getsockname()[1])

    @staticmethod
    def _build_settings_override() -> dict[str, object]:
        return {
            "autoSaveProjectFile": False,
            "allowMultipleInstances": False,
            "exportConfirmEnabled": False,
            "enableOverwriteOutput": True,
            "outFormatLocked": "mp4",
            "safeOutputFileName": True,
            "simpleMode": True,
        }

    @staticmethod
    def _probe_duration(source_path: Path) -> float:
        ffprobe_executable = binary_path("ffprobe")
        command = [
            str(ffprobe_executable),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source_path),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise LosslessCutExportError(
                "ffprobe 실행 파일을 찾지 못했습니다. LosslessCut 번들 또는 보조 FFmpeg 구성을 확인해 주세요."
            ) from exc
        if completed.returncode != 0:
            raise LosslessCutExportError(
                f"ffprobe로 미디어 길이를 읽지 못했습니다.\n명령: {' '.join(command)}\n{completed.stderr}".strip()
            )
        try:
            duration = float(completed.stdout.strip())
        except ValueError as exc:
            raise LosslessCutExportError(
                f"ffprobe 출력에서 미디어 길이를 해석하지 못했습니다: {completed.stdout!r}"
            ) from exc
        if duration <= 0:
            raise LosslessCutExportError(f"미디어 길이가 올바르지 않습니다: {duration}")
        return duration

    def _iter_runtime_source_dirs(self) -> list[Path]:
        if self.runtime_sources is not None:
            return [path for path in self.runtime_sources if path.exists()]

        candidates = [
            resource_path("bin", "losslesscut"),
            resource_path("bin"),
            Path(__file__).resolve().parent.parent / "bin" / "losslesscut",
            Path(__file__).resolve().parent.parent / "bin",
        ]

        which_path = shutil.which("LosslessCut.exe") or shutil.which("LosslessCut")
        if which_path:
            candidates.append(Path(which_path).parent)

        discovered: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            if candidate in seen or not candidate.exists():
                continue
            if self._find_executable(candidate) is not None:
                discovered.append(candidate)
                seen.add(candidate)
        return discovered

    @staticmethod
    def _find_executable(root_dir: Path) -> Path | None:
        direct_candidate = root_dir / "LosslessCut.exe"
        if direct_candidate.exists():
            return direct_candidate
        for candidate in root_dir.rglob("LosslessCut.exe"):
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _replace_directory(source_dir: Path, destination_dir: Path) -> None:
        if destination_dir.exists():
            shutil.rmtree(destination_dir)
        destination_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, destination_dir)

    @staticmethod
    def _emit_log(callback: LogCallback, message: str) -> None:
        if callback:
            callback(message)

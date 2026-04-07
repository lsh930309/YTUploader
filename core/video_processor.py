from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, Optional

from .paths import binary_path, create_job_temp_dir

StageCallback = Optional[Callable[[str], None]]
LogCallback = Optional[Callable[[str], None]]

VALIDATING = "VALIDATING"
SYNCING = "SYNCING"
REMUXING = "REMUXING"
CLEANUP = "CLEANUP"
DONE = "DONE"


class VideoProcessingError(RuntimeError):
    pass


class VideoValidationError(VideoProcessingError):
    pass


class VideoProcessingCancelled(VideoProcessingError):
    pass


@dataclass
class VideoJob:
    input_mkv: Path
    output_mp4: Path
    delay_ms: int = 0
    start_time: str | None = None
    end_time: str | None = None
    temp_dir: Path | None = None


def parse_timecode(value: str | None) -> float | None:
    if value is None:
        return None

    text = value.strip()
    if not text:
        return None

    if text.replace(".", "", 1).isdigit():
        return float(text)

    parts = text.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return (int(minutes) * 60) + float(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return (int(hours) * 3600) + (int(minutes) * 60) + float(seconds)

    raise VideoValidationError(f"Invalid timecode: {value}")


def validate_job(job: VideoJob) -> None:
    errors: list[str] = []

    if not Path(job.input_mkv).exists():
        errors.append(f"Input MKV does not exist: {job.input_mkv}")

    if Path(job.output_mp4).suffix.lower() != ".mp4":
        errors.append("Output file must use the .mp4 extension.")

    try:
        start_seconds = parse_timecode(job.start_time)
        end_seconds = parse_timecode(job.end_time)
    except ValueError as exc:
        raise VideoValidationError(str(exc)) from exc

    if start_seconds is not None and end_seconds is not None and end_seconds <= start_seconds:
        errors.append("End time must be greater than start time.")

    if errors:
        raise VideoValidationError(" ".join(errors))


def build_sync_command(job: VideoJob, temp_mkv: Path, mkvmerge_executable: Path | None = None) -> list[str]:
    executable = mkvmerge_executable or binary_path("mkvmerge")
    return [
        str(executable),
        "-o",
        str(temp_mkv),
        "--sync",
        f"1:{int(job.delay_ms)}",
        str(job.input_mkv),
    ]


def build_remux_command(job: VideoJob, temp_mkv: Path, ffmpeg_executable: Path | None = None) -> list[str]:
    executable = ffmpeg_executable or binary_path("ffmpeg")
    command = [str(executable), "-y"]
    if job.start_time and job.start_time.strip():
        command.extend(["-ss", job.start_time.strip()])
    if job.end_time and job.end_time.strip():
        command.extend(["-to", job.end_time.strip()])
    command.extend(["-i", str(temp_mkv), "-c", "copy", str(job.output_mp4)])
    return command


class VideoProcessor:
    def __init__(self, ffmpeg_executable: Path | None = None, mkvmerge_executable: Path | None = None) -> None:
        self.ffmpeg_executable = ffmpeg_executable or binary_path("ffmpeg")
        self.mkvmerge_executable = mkvmerge_executable or binary_path("mkvmerge")
        self._current_process: subprocess.Popen[str] | None = None
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True
        if self._current_process and self._current_process.poll() is None:
            self._current_process.terminate()

    def process_video(
        self,
        job: VideoJob,
        *,
        stage_callback: StageCallback = None,
        log_callback: LogCallback = None,
    ) -> Path:
        validate_job(job)
        temp_dir = job.temp_dir or create_job_temp_dir()
        temp_mkv = temp_dir / "synced.mkv"
        output_path = Path(job.output_mp4)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._emit_stage(stage_callback, VALIDATING)
            self._emit_log(log_callback, f"Validated job for {job.input_mkv}")
            self._ensure_not_cancelled()

            self._emit_stage(stage_callback, SYNCING)
            self._emit_log(log_callback, "Running mkvmerge for audio sync correction.")
            sync_command = build_sync_command(job, temp_mkv, mkvmerge_executable=self.mkvmerge_executable)
            self._run_command(sync_command, log_callback=log_callback)
            self._ensure_not_cancelled()

            self._emit_stage(stage_callback, REMUXING)
            self._emit_log(log_callback, "Running ffmpeg remux step.")
            remux_command = build_remux_command(job, temp_mkv, ffmpeg_executable=self.ffmpeg_executable)
            self._run_command(remux_command, log_callback=log_callback)
            self._ensure_not_cancelled()

            self._emit_stage(stage_callback, CLEANUP)
            shutil.rmtree(temp_dir, ignore_errors=True)
            self._emit_log(log_callback, f"Removed temp directory {temp_dir}")
            self._emit_stage(stage_callback, DONE)
            self._emit_log(log_callback, f"Finished processing {output_path}")
            return output_path
        except VideoProcessingCancelled:
            self._emit_log(log_callback, f"Processing cancelled. Temp directory kept at {temp_dir}")
            raise
        except Exception:
            self._emit_log(log_callback, f"Processing failed. Temp directory kept at {temp_dir}")
            raise
        finally:
            self._current_process = None
            self._cancel_requested = False

    def _run_command(self, command: list[str], *, log_callback: LogCallback = None) -> None:
        output_queue: Queue[str] = Queue()
        captured_output: list[str] = []

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._current_process = process

        def drain_stdout() -> None:
            assert process.stdout is not None
            for line in iter(process.stdout.readline, ""):
                output_queue.put(line)
            process.stdout.close()

        reader_thread = threading.Thread(target=drain_stdout, daemon=True)
        reader_thread.start()

        while True:
            if self._cancel_requested:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise VideoProcessingCancelled("Video processing was cancelled by the user.")

            self._flush_output_queue(output_queue, captured_output, log_callback)

            if process.poll() is not None:
                break

            time.sleep(0.1)

        reader_thread.join(timeout=1)
        self._flush_output_queue(output_queue, captured_output, log_callback)
        self._current_process = None

        if process.returncode != 0:
            joined_output = "".join(captured_output).strip()
            raise VideoProcessingError(
                f"Command failed with exit code {process.returncode}: {' '.join(command)}\n{joined_output}"
            )

    @staticmethod
    def _flush_output_queue(
        output_queue: Queue[str],
        captured_output: list[str],
        log_callback: LogCallback,
    ) -> None:
        while True:
            try:
                line = output_queue.get_nowait()
            except Empty:
                break
            captured_output.append(line)
            if log_callback:
                log_callback(line.rstrip())

    @staticmethod
    def _emit_stage(callback: StageCallback, stage: str) -> None:
        if callback:
            callback(stage)

    @staticmethod
    def _emit_log(callback: LogCallback, message: str) -> None:
        if callback:
            callback(message)

    def _ensure_not_cancelled(self) -> None:
        if self._cancel_requested:
            raise VideoProcessingCancelled("Video processing was cancelled by the user.")

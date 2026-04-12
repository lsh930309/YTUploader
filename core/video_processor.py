# -*- coding: utf-8 -*-
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, Optional

from .losslesscut import LosslessCutController
from .models import ChapterMarker, ClipDraft
from .paths import binary_path, create_job_temp_dir

StageCallback = Optional[Callable[[str], None]]
LogCallback = Optional[Callable[[str], None]]

VALIDATING = "VALIDATING"
SYNCING = "SYNCING"
REMUXING = "REMUXING"
THUMBNAIL = "THUMBNAIL"
EXPORTING = "EXPORTING"
CLEANUP = "CLEANUP"
DONE = "DONE"


class VideoProcessingError(RuntimeError):
    pass


class VideoValidationError(VideoProcessingError):
    pass


class VideoProcessingCancelled(VideoProcessingError):
    pass


@dataclass(slots=True)
class VideoJob:
    input_mkv: Path
    output_mp4: Path
    delay_ms: int = 0
    start_time: str | None = None
    end_time: str | None = None
    temp_dir: Path | None = None


@dataclass(slots=True)
class ClipJob:
    clip_id: str
    clip_name: str
    output_mp4: Path
    start_time: str | None = None
    end_time: str | None = None
    thumbnail_time: str | None = None
    custom_title: str = ""
    custom_notes: str = ""
    upload_enabled: bool = False
    chapters: list[ChapterMarker] = field(default_factory=list)


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

    raise VideoValidationError(f"잘못된 시점 형식입니다: {value}")


def validate_job(job: VideoJob) -> None:
    errors: list[str] = []

    if not Path(job.input_mkv).exists():
        errors.append(f"입력 MKV 파일이 존재하지 않습니다: {job.input_mkv}")

    if Path(job.output_mp4).suffix.lower() != ".mp4":
        errors.append("출력 파일은 .mp4 확장자를 사용해야 합니다.")

    try:
        start_seconds = parse_timecode(job.start_time)
        end_seconds = parse_timecode(job.end_time)
    except ValueError as exc:
        raise VideoValidationError(str(exc)) from exc

    if start_seconds is not None and end_seconds is not None and end_seconds <= start_seconds:
        errors.append("끝 시점은 시작 시점보다 뒤여야 합니다.")

    if errors:
        raise VideoValidationError(" ".join(errors))


def validate_clip_job(clip: ClipJob) -> None:
    errors: list[str] = []

    if Path(clip.output_mp4).suffix.lower() != ".mp4":
        errors.append("클립 출력 파일은 .mp4 확장자를 사용해야 합니다.")

    try:
        start_seconds = parse_timecode(clip.start_time)
        end_seconds = parse_timecode(clip.end_time)
        thumbnail_seconds = parse_timecode(clip.thumbnail_time)
    except ValueError as exc:
        raise VideoValidationError(str(exc)) from exc

    if start_seconds is not None and end_seconds is not None and end_seconds <= start_seconds:
        errors.append(f"클립 '{clip.clip_name}'의 끝 시점은 시작 시점보다 뒤여야 합니다.")

    if thumbnail_seconds is not None and start_seconds is not None and thumbnail_seconds < start_seconds:
        errors.append(f"클립 '{clip.clip_name}'의 썸네일 시점은 클립 범위 안에 있어야 합니다.")

    if (
        thumbnail_seconds is not None
        and end_seconds is not None
        and thumbnail_seconds > end_seconds
    ):
        errors.append(f"클립 '{clip.clip_name}'의 썸네일 시점은 클립 범위 안에 있어야 합니다.")

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


def build_remux_command(
    job: VideoJob | ClipJob,
    temp_mkv: Path,
    ffmpeg_executable: Path | None = None,
) -> list[str]:
    executable = ffmpeg_executable or binary_path("ffmpeg")
    command = [str(executable), "-y"]
    if job.start_time and job.start_time.strip():
        command.extend(["-ss", job.start_time.strip()])
    if job.end_time and job.end_time.strip():
        command.extend(["-to", job.end_time.strip()])
    command.extend(["-i", str(temp_mkv), "-c", "copy", str(job.output_mp4)])
    return command


def build_thumbnail_command(
    source_mkv: Path,
    thumbnail_time: str,
    output_path: Path,
    ffmpeg_executable: Path | None = None,
) -> list[str]:
    executable = ffmpeg_executable or binary_path("ffmpeg")
    return [
        str(executable),
        "-y",
        "-ss",
        thumbnail_time.strip(),
        "-i",
        str(source_mkv),
        "-frames:v",
        "1",
        str(output_path),
    ]


class VideoProcessor:
    def __init__(
        self,
        ffmpeg_executable: Path | None = None,
        mkvmerge_executable: Path | None = None,
        losslesscut_controller: LosslessCutController | None = None,
    ) -> None:
        self.ffmpeg_executable = ffmpeg_executable or binary_path("ffmpeg")
        self.mkvmerge_executable = mkvmerge_executable or binary_path("mkvmerge")
        self.losslesscut_controller = losslesscut_controller or LosslessCutController()
        self._current_process: subprocess.Popen[str] | None = None
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True
        self.losslesscut_controller.shutdown()
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
        clip = ClipJob(
            clip_id="single",
            clip_name=Path(job.output_mp4).stem,
            output_mp4=Path(job.output_mp4),
            start_time=job.start_time,
            end_time=job.end_time,
        )
        results = self.process_clips(
            input_mkv=job.input_mkv,
            delay_ms=job.delay_ms,
            clips=[clip],
            temp_dir=job.temp_dir,
            stage_callback=stage_callback,
            log_callback=log_callback,
        )
        return results[0]

    def process_clips(
        self,
        *,
        input_mkv: Path,
        delay_ms: int,
        clips: list[ClipJob],
        temp_dir: Path | None = None,
        stage_callback: StageCallback = None,
        log_callback: LogCallback = None,
    ) -> list[Path]:
        if not input_mkv.exists():
            raise VideoValidationError(f"입력 MKV 파일이 존재하지 않습니다: {input_mkv}")
        if not clips:
            raise VideoValidationError("클립은 하나 이상 정의해야 합니다.")

        for clip in clips:
            validate_clip_job(clip)

        temp_dir = temp_dir or create_job_temp_dir()
        temp_mkv = temp_dir / "synced.mkv"

        try:
            synced_path = self.apply_audio_sync(
                input_mkv=input_mkv,
                delay_ms=delay_ms,
                output_path=temp_mkv,
                stage_callback=stage_callback,
                log_callback=log_callback,
            )
            rendered_paths = self.split_clips(
                source_mkv=synced_path,
                clips=clips,
                stage_callback=stage_callback,
                log_callback=log_callback,
            )

            self._emit_stage(stage_callback, CLEANUP)
            shutil.rmtree(temp_dir, ignore_errors=True)
            self._emit_log(log_callback, f"임시 폴더를 정리했습니다: {temp_dir}")
            self._emit_stage(stage_callback, DONE)
            self._emit_log(log_callback, f"클립 {len(rendered_paths)}개 처리를 마쳤습니다.")
            return rendered_paths
        except VideoProcessingCancelled:
            self._emit_log(log_callback, f"처리가 취소되어 임시 폴더를 유지합니다: {temp_dir}")
            raise
        except Exception:
            self._emit_log(log_callback, f"처리에 실패하여 임시 폴더를 유지합니다: {temp_dir}")
            raise
        finally:
            self._current_process = None
            self._cancel_requested = False

    def apply_audio_sync(
        self,
        *,
        input_mkv: Path,
        delay_ms: int,
        output_path: Path,
        stage_callback: StageCallback = None,
        log_callback: LogCallback = None,
    ) -> Path:
        if not input_mkv.exists():
            raise VideoValidationError(f"입력 MKV 파일이 존재하지 않습니다: {input_mkv}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._emit_stage(stage_callback, VALIDATING)
        self._emit_log(log_callback, f"소스 검증 완료: {input_mkv}")
        self._ensure_not_cancelled()
        self._emit_stage(stage_callback, SYNCING)
        self._emit_log(log_callback, "오디오 싱크 보정을 위해 mkvmerge를 실행합니다.")
        sync_job = VideoJob(input_mkv=input_mkv, output_mp4=output_path.with_suffix(".mp4"), delay_ms=delay_ms)
        sync_command = build_sync_command(sync_job, output_path, mkvmerge_executable=self.mkvmerge_executable)
        self._run_command(sync_command, log_callback=log_callback)
        self._ensure_not_cancelled()
        self._emit_log(log_callback, f"오디오 싱크 적용 완료: {output_path.name}")
        return output_path

    def split_clips(
        self,
        *,
        source_mkv: Path,
        clips: list[ClipJob],
        stage_callback: StageCallback = None,
        log_callback: LogCallback = None,
    ) -> list[Path]:
        if not source_mkv.exists():
            raise VideoValidationError(f"세그먼트 분할 소스가 존재하지 않습니다: {source_mkv}")
        if not clips:
            raise VideoValidationError("클립은 하나 이상 정의해야 합니다.")
        for clip in clips:
            validate_clip_job(clip)
        self._emit_stage(stage_callback, REMUXING)
        for clip in clips:
            clip.output_mp4.parent.mkdir(parents=True, exist_ok=True)
            self._emit_log(log_callback, f"클립 export 준비: {clip.clip_name} -> {clip.output_mp4.name}")
        rendered_paths = self.losslesscut_controller.export_clips(
            source_path=source_mkv,
            clips=clips,
            log_callback=log_callback,
        )
        self._ensure_not_cancelled()
        return rendered_paths

    def capture_thumbnail(
        self,
        *,
        source_mkv: Path,
        thumbnail_time: str,
        output_path: Path,
        stage_callback: StageCallback = None,
        log_callback: LogCallback = None,
    ) -> Path:
        if not source_mkv.exists():
            raise VideoValidationError(f"입력 MKV 파일이 존재하지 않습니다: {source_mkv}")
        if not thumbnail_time.strip():
            raise VideoValidationError("썸네일 시점이 필요합니다.")

        parse_timecode(thumbnail_time)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._emit_stage(stage_callback, THUMBNAIL)
        command = build_thumbnail_command(
            source_mkv,
            thumbnail_time,
            output_path,
            ffmpeg_executable=self.ffmpeg_executable,
        )
        self._run_command(command, log_callback=log_callback)
        self._emit_log(log_callback, f"썸네일을 추출했습니다: {output_path.name}")
        return output_path

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
                raise VideoProcessingCancelled("사용자 요청으로 영상 처리가 취소되었습니다.")

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
                f"명령 실행이 실패했습니다. 종료 코드: {process.returncode}\n"
                f"명령: {' '.join(command)}\n{joined_output}"
            )

    @staticmethod
    def clip_jobs_from_drafts(
        input_mkv: Path,
        output_dir: Path,
        clip_drafts: list[ClipDraft],
    ) -> list[ClipJob]:
        jobs: list[ClipJob] = []
        source_stem = input_mkv.stem
        for index, clip in enumerate(clip_drafts, start=1):
            clip_name = clip.clip_name.strip() or f"clip_{index:02d}"
            safe_name = clip_name.replace(" ", "_")
            output_path = output_dir / f"{source_stem}_{safe_name}.mp4"
            jobs.append(
                ClipJob(
                    clip_id=clip.clip_id,
                    clip_name=clip_name,
                    output_mp4=output_path,
                    start_time=clip.start_time,
                    end_time=clip.end_time,
                    thumbnail_time=clip.thumbnail_time,
                    custom_title=clip.custom_title,
                    custom_notes=clip.custom_notes,
                    upload_enabled=clip.upload_enabled,
                    chapters=list(clip.chapters),
                )
            )
        return jobs

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
            raise VideoProcessingCancelled("사용자 요청으로 영상 처리가 취소되었습니다.")

from __future__ import annotations

import json
from pathlib import Path

from core.job_store import JobStore
from core.models import ClipDraft, SegmentState


def test_job_store_round_trips_job_state(tmp_path: Path) -> None:
    source_path = tmp_path / "source.mkv"
    source_path.write_text("source", encoding="utf-8")
    output_dir = tmp_path / "output"

    store = JobStore(tmp_path / "jobs")
    job = store.create_job(
        source_path=source_path,
        output_dir=output_dir,
        title_prefix="[녹화]",
        description_template="desc",
        segment_drafts=[ClipDraft(clip_id="clip-1", clip_name="segment_01", upload_enabled=True)],
    )

    loaded = store.load_job(job.job_id)

    assert loaded.job_id == job.job_id
    assert loaded.source_path == source_path
    assert loaded.output_dir == output_dir
    assert len(loaded.segment_drafts) == 1
    assert loaded.segment_drafts[0].clip_name == "segment_01"


def test_job_store_hydrates_segment_metadata_from_sidecar(tmp_path: Path) -> None:
    source_path = tmp_path / "source.mkv"
    source_path.write_text("source", encoding="utf-8")
    output_dir = tmp_path / "output"
    video_path = output_dir / "segment.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_text("video", encoding="utf-8")
    sidecar_path = video_path.with_suffix(".json")
    sidecar_path.write_text(
        json.dumps(
            {
                "job_id": "job-1",
                "clip": {
                    "clip_id": "clip-1",
                    "clip_name": "segment_01",
                    "custom_title": "manual title",
                    "custom_notes": "memo",
                    "thumbnail_time": "00:00:05.000",
                    "upload_enabled": True,
                    "chapters": [{"timecode": "00:00:01.000", "title": "intro"}],
                },
                "metadata": {
                    "description": "final description",
                    "category_id": "20",
                },
                "outputs": {
                    "video_path": str(video_path),
                    "thumbnail_path": "",
                },
                "upload": {
                    "status": "failed",
                    "video_id": "",
                    "url": "",
                    "error_message": "boom",
                },
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )

    jobs_dir = tmp_path / "jobs"
    store = JobStore(jobs_dir)
    job = store.create_job(source_path=source_path, output_dir=output_dir)
    job.segments = [
        SegmentState(
            clip_id="clip-1",
            clip_name="segment_01",
            output_path=video_path,
            sidecar_path=sidecar_path,
        )
    ]
    store.save_job(job)

    loaded = store.load_job(job.job_id)

    assert loaded.segments[0].custom_title == "manual title"
    assert loaded.segments[0].description_text == "final description"
    assert loaded.segments[0].category_id == "20"
    assert loaded.segments[0].upload_status == "failed"
    assert loaded.segments[0].upload_error == "boom"

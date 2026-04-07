from __future__ import annotations

from pathlib import Path

from core.youtube_uploader import UploadJob, YouTubeUploader, build_video_insert_body


class FakeStatus:
    def __init__(self, progress_value: float) -> None:
        self._progress_value = progress_value

    def progress(self) -> float:
        return self._progress_value


class FakeRequest:
    def __init__(self) -> None:
        self._calls = 0

    def next_chunk(self):
        self._calls += 1
        if self._calls == 1:
            return FakeStatus(0.5), None
        return FakeStatus(1.0), {"id": "abc123"}


class FakeExecute:
    def __init__(self, sink: list[str], label: str) -> None:
        self.sink = sink
        self.label = label

    def execute(self) -> None:
        self.sink.append(self.label)


class FakeVideosApi:
    def __init__(self, sink: dict) -> None:
        self.sink = sink

    def insert(self, *, part, body, media_body):
        self.sink["insert"] = {"part": part, "body": body, "media_body": media_body}
        return FakeRequest()


class FakeThumbnailsApi:
    def __init__(self, sink: list[str]) -> None:
        self.sink = sink

    def set(self, *, videoId, media_body):
        self.sink.append(f"thumbnail:{videoId}")
        return FakeExecute(self.sink, "thumbnail:execute")


class FakePlaylistItemsApi:
    def __init__(self, sink: list[str]) -> None:
        self.sink = sink

    def insert(self, *, part, body):
        self.sink.append(f"playlist:{body['snippet']['playlistId']}")
        return FakeExecute(self.sink, "playlist:execute")


class FakeService:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.video_insert: dict = {}

    def videos(self) -> FakeVideosApi:
        return FakeVideosApi(self.video_insert)

    def thumbnails(self) -> FakeThumbnailsApi:
        return FakeThumbnailsApi(self.calls)

    def playlistItems(self) -> FakePlaylistItemsApi:
        return FakePlaylistItemsApi(self.calls)


def test_build_video_insert_body_contains_metadata(tmp_path: Path) -> None:
    job = UploadJob(
        video_path=tmp_path / "video.mp4",
        title="Sample",
        description="Description",
        tags=["one", "two"],
        privacy_status="private",
        category_id="22",
    )
    body = build_video_insert_body(job)
    assert body["snippet"]["title"] == "Sample"
    assert body["status"]["privacyStatus"] == "private"


def test_upload_video_uses_resumable_flow(monkeypatch, tmp_path: Path) -> None:
    video_path = tmp_path / "video.mp4"
    thumbnail_path = tmp_path / "thumb.png"
    video_path.write_text("video", encoding="utf-8")
    thumbnail_path.write_text("thumb", encoding="utf-8")

    uploader = YouTubeUploader(
        client_secrets_path=tmp_path / "client_secrets.json",
        token_path=tmp_path / "token.json",
    )
    fake_service = FakeService()
    progress_updates: list[int | None] = []

    monkeypatch.setattr(uploader, "_ensure_google_api_available", lambda: None)
    monkeypatch.setattr(uploader, "ensure_credentials", lambda interactive=True, log_callback=None: object())
    monkeypatch.setattr(uploader, "build_service", lambda credentials: fake_service)
    monkeypatch.setattr("core.youtube_uploader.MediaFileUpload", lambda *args, **kwargs: {"args": args, "kwargs": kwargs})

    result = uploader.upload_video(
        UploadJob(
            video_path=video_path,
            title="Video",
            description="Desc",
            tags=["tag"],
            playlist_id="playlist-123",
            privacy_status="private",
            thumbnail_path=thumbnail_path,
        ),
        stage_callback=lambda stage: None,
        progress_callback=progress_updates.append,
        log_callback=lambda message: None,
    )

    assert result == {"video_id": "abc123", "url": "https://youtu.be/abc123"}
    assert progress_updates == [0, 50, 100, 100]
    assert fake_service.video_insert["insert"]["part"] == "snippet,status"
    assert "thumbnail:abc123" in fake_service.calls
    assert "playlist:playlist-123" in fake_service.calls


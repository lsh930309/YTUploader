# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .paths import get_client_secrets_path, get_token_path

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
except ModuleNotFoundError as exc:  # pragma: no cover - exercised in environments without Google deps
    GOOGLE_IMPORT_ERROR = exc
    Request = None
    Credentials = None
    InstalledAppFlow = None
    MediaFileUpload = None
    build = None
    HttpError = Exception
else:
    GOOGLE_IMPORT_ERROR = None

StageCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[Optional[int]], None]]
LogCallback = Optional[Callable[[str], None]]

AUTHENTICATING = "AUTHENTICATING"
UPLOADING = "UPLOADING"
DONE = "DONE"

DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]


class YouTubeUploadError(RuntimeError):
    pass


class YouTubeCredentialError(YouTubeUploadError):
    pass


class YouTubeUploadCancelled(YouTubeUploadError):
    pass


@dataclass
class UploadJob:
    video_path: Path
    title: str
    description: str
    tags: list[str]
    playlist_id: str = ""
    privacy_status: str = "private"
    thumbnail_path: Path | None = None
    category_id: str = "22"
    made_for_kids: bool = False


def build_video_insert_body(job: UploadJob) -> dict[str, Any]:
    return {
        "snippet": {
            "title": job.title,
            "description": job.description,
            "tags": job.tags,
            "categoryId": job.category_id,
        },
        "status": {
            "privacyStatus": job.privacy_status,
            "selfDeclaredMadeForKids": job.made_for_kids,
        },
    }


class YouTubeUploader:
    def __init__(
        self,
        *,
        client_secrets_path: Path | None = None,
        token_path: Path | None = None,
        scopes: list[str] | None = None,
    ) -> None:
        self.client_secrets_path = client_secrets_path or get_client_secrets_path()
        self.token_path = token_path or get_token_path()
        self.scopes = scopes or list(DEFAULT_SCOPES)
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def credential_setup_message(self) -> str:
        return (
            "client_secrets.json 파일이 없습니다. "
            f"구글 OAuth 데스크톱 앱 자격 증명을 {self.client_secrets_path.parent} 폴더에 넣어 주세요."
        )

    def ensure_credentials(
        self,
        *,
        interactive: bool = True,
        log_callback: LogCallback = None,
    ):
        self._ensure_google_api_available()
        credentials = None

        if self.token_path.exists():
            credentials = Credentials.from_authorized_user_file(str(self.token_path), self.scopes)

        if credentials and credentials.expired and credentials.refresh_token:
            self._emit_log(log_callback, "저장된 유튜브 토큰을 새로고침합니다.")
            credentials.refresh(Request())
            self._save_credentials(credentials)
            return credentials

        if credentials and credentials.valid:
            return credentials

        if not interactive:
            raise YouTubeCredentialError("새 유튜브 자격 증명을 만들려면 대화형 OAuth 인증이 필요합니다.")

        if not self.client_secrets_path.exists():
            raise YouTubeCredentialError(self.credential_setup_message())

        self._emit_log(log_callback, "OAuth 브라우저 인증을 시작합니다.")
        flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secrets_path), self.scopes)
        credentials = flow.run_local_server(port=0, open_browser=True)
        self._save_credentials(credentials)
        return credentials

    def build_service(self, credentials):
        self._ensure_google_api_available()
        return build("youtube", "v3", credentials=credentials, cache_discovery=False)

    def upload_video(
        self,
        job: UploadJob,
        *,
        interactive: bool = True,
        stage_callback: StageCallback = None,
        progress_callback: ProgressCallback = None,
        log_callback: LogCallback = None,
    ) -> dict[str, str]:
        self._cancel_requested = False
        try:
            video_path = Path(job.video_path)
            if not video_path.exists():
                raise YouTubeUploadError(f"업로드할 영상 파일이 존재하지 않습니다: {video_path}")

            self._emit_stage(stage_callback, AUTHENTICATING)
            credentials = self.ensure_credentials(interactive=interactive, log_callback=log_callback)
            service = self.build_service(credentials)
            self._ensure_not_cancelled()

            self._emit_stage(stage_callback, UPLOADING)
            self._emit_log(log_callback, f"유튜브에 업로드 중: {video_path.name}")
            if progress_callback:
                progress_callback(0)

            media_body = MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True)
            request = service.videos().insert(
                part="snippet,status",
                body=build_video_insert_body(job),
                media_body=media_body,
            )

            response = None
            try:
                while response is None:
                    self._ensure_not_cancelled()
                    status, response = request.next_chunk()
                    if status and progress_callback:
                        progress_callback(int(status.progress() * 100))
            except HttpError as exc:
                raise YouTubeUploadError(f"유튜브 업로드에 실패했습니다: {exc}") from exc

            video_id = response.get("id") if response else None
            if not video_id:
                raise YouTubeUploadError("업로드는 끝났지만 유튜브 영상 ID를 돌려받지 못했습니다.")

            if job.thumbnail_path:
                self.set_thumbnail(service, video_id, Path(job.thumbnail_path), log_callback=log_callback)

            if job.playlist_id:
                self.add_to_playlist(service, video_id, job.playlist_id, log_callback=log_callback)

            if progress_callback:
                progress_callback(100)
            self._emit_stage(stage_callback, DONE)
            return {"video_id": video_id, "url": f"https://youtu.be/{video_id}"}
        finally:
            self._cancel_requested = False

    def set_thumbnail(self, service, video_id: str, thumbnail_path: Path, *, log_callback: LogCallback = None) -> None:
        if not thumbnail_path.exists():
            raise YouTubeUploadError(f"썸네일 파일이 존재하지 않습니다: {thumbnail_path}")

        self._emit_log(log_callback, f"썸네일 업로드 중: {thumbnail_path.name}")
        media_body = MediaFileUpload(str(thumbnail_path), resumable=False)
        service.thumbnails().set(videoId=video_id, media_body=media_body).execute()

    def add_to_playlist(self, service, video_id: str, playlist_id: str, *, log_callback: LogCallback = None) -> None:
        self._emit_log(log_callback, f"재생목록에 추가 중: {playlist_id}")
        body = {
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        }
        service.playlistItems().insert(part="snippet", body=body).execute()

    def _save_credentials(self, credentials) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(credentials.to_json(), encoding="utf-8")

    def _ensure_google_api_available(self) -> None:
        if GOOGLE_IMPORT_ERROR is not None:
            raise YouTubeUploadError(
                "Google API 의존성이 없습니다. 유튜브 업로드 전에 프로젝트 요구 사항을 먼저 설치해 주세요."
            ) from GOOGLE_IMPORT_ERROR

    def _ensure_not_cancelled(self) -> None:
        if self._cancel_requested:
            raise YouTubeUploadCancelled("사용자 요청으로 유튜브 업로드가 취소되었습니다.")

    @staticmethod
    def _emit_stage(callback: StageCallback, stage: str) -> None:
        if callback:
            callback(stage)

    @staticmethod
    def _emit_log(callback: LogCallback, message: str) -> None:
        if callback:
            callback(message)

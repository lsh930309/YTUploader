# YTUploader

Windows 11 전용 데스크톱 앱으로, OBS로 녹화한 MKV 파일의 오디오 싱크를 보정하고 원하는 구간을 빠르게 MP4로 remux한 뒤 YouTube에 업로드하는 개인용 자동화 도구입니다.

## Features

- `mkvmerge`로 오디오 싱크 딜레이 보정
- `ffmpeg -c copy` 기반 무재인코딩 trim/remux
- PyQt6 GUI 기반 단일 작업 워크플로
- JSON 템플릿 저장
- Google OAuth 2.0 기반 YouTube 업로드
- PyInstaller `onedir` + Inno Setup 배포 준비

## Project Layout

```text
.
├── main.py
├── core/
├── ui/
├── tests/
├── assets/
├── bin/
├── credentials/
├── build_script.py
├── installer.iss
└── PLAN.MD
```

## Requirements

- Windows 11
- Python 3.11+
- `ffmpeg.exe`, `ffprobe.exe`, `mkvmerge.exe`
- Google Cloud에서 발급한 YouTube Data API OAuth Desktop client credentials

## Quick Start

1. Python 3.11 가상환경을 만든다.
2. 의존성을 설치한다.
3. `bin/` 폴더에 `ffmpeg.exe`, `ffprobe.exe`, `mkvmerge.exe`를 넣는다.
4. 첫 실행 전 `%LOCALAPPDATA%\\YTUploader\\credentials\\client_secrets.json` 위치에 Google OAuth 클라이언트 파일을 넣는다.
5. 앱을 실행한다.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
python main.py
```

## Runtime Data

앱은 설치 디렉터리에 쓰지 않고 아래 위치를 사용합니다.

- `%LOCALAPPDATA%\\YTUploader\\settings.json`
- `%LOCALAPPDATA%\\YTUploader\\credentials\\client_secrets.json`
- `%LOCALAPPDATA%\\YTUploader\\credentials\\token.json`
- `%LOCALAPPDATA%\\YTUploader\\temp\\`
- `%LOCALAPPDATA%\\YTUploader\\logs\\`

## Testing

```bash
pytest
```

현재 테스트는 경로 해석, 템플릿 렌더링, 비디오 명령 구성, 업로드 요청 구성을 중심으로 작성되어 있습니다.

## Packaging

PyInstaller `onedir` 빌드:

```bash
python build_script.py
```

이후 Inno Setup에서 `installer.iss`를 컴파일해 설치 프로그램을 생성합니다.

## Notes

- 이 저장소에는 Google OAuth 시크릿과 비디오 바이너리를 포함하지 않습니다.
- 현재 구현은 단일 사용자, 단일 계정, 단일 작업 처리 흐름을 기준으로 합니다.
- 실제 YouTube 업로드 smoke test는 Windows 11 환경에서 별도로 수행해야 합니다.

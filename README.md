# YTUploader

OBS 녹화본을 빠르게 불러와 여러 개의 MP4 클립으로 나누고, 게임 영상 업로드에 필요한 메타데이터와 썸네일을 준비한 뒤 선택적으로 YouTube 업로드까지 진행하는 Windows 11 전용 데스크톱 앱입니다.

## Features

- OBS 녹화 폴더 저장 및 최근 MKV 빠른 가져오기
- `mkvmerge` 기반 오디오 싱크 딜레이 보정
- `LosslessCut` 기반 다중 클립 export
- 클립별 제목, 챕터, 메모, 업로드 여부 관리
- 클립별 썸네일 PNG 추출
- 클립별 JSON 사이드카 및 클립보드 payload 생성
- `MPC-BE` 설정 import 및 미리보기 실행 지원
- 선택적 YouTube 업로드

## Project Layout

```text
.
├── main.py
├── core/
├── ui/
├── tests/
├── assets/
├── bin/
├── build_script.py
├── installer.iss
└── PLAN.MD
```

## Requirements

- Windows 11
- Python 3.11+
- `LosslessCut`, `mkvmerge`, `MPC-BE` 런타임
- 썸네일 추출용 `ffmpeg` / `ffprobe`는 앱 번들 또는 LosslessCut 번들 내부 보조 도구를 사용
- Google Cloud에서 발급한 YouTube Data API OAuth Desktop client credentials

## Quick Start

1. Python 3.11 가상환경을 만든다.
2. 의존성을 설치한다.
3. `bin/` 폴더에 필요한 런타임 번들을 넣는다.
4. 필요하면 `%LOCALAPPDATA%\\YTUploader\\credentials\\client_secrets.json` 위치에 Google OAuth 클라이언트 파일을 넣는다.
5. 앱을 실행하고 필수 도구 준비 마법사를 완료한다.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
python main.py
```

## Bundled Binaries

`bin/` 기준으로 아래 런타임 번들을 준비합니다.

- `losslesscut/` 디렉터리 또는 `LosslessCut.exe` 포함 번들
- `mkvmerge.exe`
- `mpc-be64.exe`
- 또는 `mpc-be/` 디렉터리 전체
- 선택: `ffmpeg.exe`, `ffprobe.exe`

## Runtime Data

앱은 설치 디렉터리에 쓰지 않고 아래 위치를 사용합니다.

- `%LOCALAPPDATA%\\YTUploader\\settings.json`
- `%LOCALAPPDATA%\\YTUploader\\catalog.db`
- `%LOCALAPPDATA%\\YTUploader\\credentials\\client_secrets.json`
- `%LOCALAPPDATA%\\YTUploader\\credentials\\token.json`
- `%LOCALAPPDATA%\\YTUploader\\temp\\`
- `%LOCALAPPDATA%\\YTUploader\\logs\\`
- `%LOCALAPPDATA%\\YTUploader\\losslesscut\\`
- `%LOCALAPPDATA%\\YTUploader\\mpc-be\\mpc-be64.ini`

## Testing

```bash
pytest
```

## Packaging

PyInstaller `onedir` 빌드:

```bash
python build_script.py
```

이후 Inno Setup에서 `installer.iss`를 컴파일해 설치 프로그램을 생성합니다.

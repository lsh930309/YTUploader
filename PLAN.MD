# Project: Game Video Preparation Tool (Windows 11)
**Target OS:** Windows 11  
**Target Audience:** Single User (Personal Workflow Automation)  
**Architecture Style:** Desktop Workflow Application

## 1. Project Overview
이 프로젝트는 OBS로 녹화한 MKV 파일을 빠르게 불러와 오디오 싱크를 조정하고, 하나의 원본에서 여러 개의 MP4 클립을 분할 생성한 뒤, 게임 영상 업로드에 필요한 제목/설명/태그/챕터/썸네일을 정리하고 필요 시 YouTube까지 업로드할 수 있게 돕는 Windows 전용 데스크톱 앱입니다.

핵심 가치는 "영상 편집기"가 아니라 "업로드 준비 워크플로 도구"에 있습니다.

## 2. Product Direction
- 사용 방식은 `수동 1건 처리`
- 입력은 `OBS MKV` 단일 파일
- 출력은 `1 MKV -> N MP4`
- 결과물은 클립별로 독립
- 제목 말머리는 템플릿으로 자동 적용
- 실제 제목 본문은 사용자가 클립별로 직접 입력
- `MPC-BE`는 미리보기/탐색 지원용 런타임으로 사용

## 3. Core Workflow
1. 설정에 저장된 OBS 녹화 폴더에서 최근 MKV를 빠르게 선택
2. 필요하면 직접 MKV 파일을 수동 선택
3. `MPC-BE`로 원본을 열어 탐색
4. 오디오 싱크 보정값 입력
5. 컷 구간을 하나 이상 정의
6. 각 구간을 독립된 클립으로 관리
7. 클립별 썸네일 시점 지정
8. 클립별 챕터와 메모 작성
9. 게임/프리셋/캐릭터/세팅/태그 같은 공통 메타데이터 입력
10. 클립별 MP4, 썸네일 PNG, 사이드카 JSON, 클립보드용 텍스트 생성
11. 원하는 클립만 선택적으로 YouTube 업로드

## 4. Runtime Strategy
### Bundled Tools
- `ffmpeg.exe`
- `ffprobe.exe`
- `mkvmerge.exe`
- `mpc-be64.exe` 또는 `bin/mpc-be/` 배포본

### External Tools Policy
- `LosslessCut` GUI는 내장하지 않음
- `MKVToolNix GUI`는 내장하지 않음
- 핵심 기능은 `ffmpeg + mkvmerge`로 직접 구현

## 5. Data Model
### Settings
- `obs_source_dir`: 기본 OBS 녹화 폴더
- `recent_source_files`: 최근 불러온 MKV 목록
- `last_output_dir`
- `default_thumbnail_dir`
- `last_delay_ms`
- `title_prefix_template`
- `description_template`
- `tags`
- `playlist_id`
- `privacy_status`
- `category_id`

### Job Model
- `JobDraft`
  - 입력 MKV 경로
  - OBS 소스 폴더
  - 오디오 딜레이
  - 게임/프리셋/캐릭터/세팅/태그
  - 제목 말머리
  - 설명 템플릿
  - 클립 목록

- `ClipDraft`
  - `clip_name`
  - `start_time`
  - `end_time`
  - `thumbnail_time`
  - `custom_title`
  - `custom_notes`
  - `upload_enabled`
  - `chapters`

- `ClipExport`
  - `video_path`
  - `thumbnail_path`
  - `metadata_sidecar_path`
  - `clipboard_payload`
  - 선택적 `youtube_upload_payload`

### Catalog Storage
메타데이터용 게임 프로필과 프리셋은 기본적으로 SQLite에 저장합니다.

- `game_profiles`
- `presets`

## 6. Module Layout
```text
project_root/
├── main.py
├── core/
│   ├── catalog_store.py
│   ├── data_manager.py
│   ├── metadata_exporter.py
│   ├── models.py
│   ├── mpc_be.py
│   ├── video_processor.py
│   ├── workflow.py
│   └── youtube_uploader.py
├── ui/
│   ├── main_window.py
│   └── worker_threads.py
├── tests/
├── assets/
├── bin/
├── build_script.py
└── installer.iss
```

## 7. Implementation Notes
### Video Processing
- 오디오 싱크 보정은 `mkvmerge --sync`
- 컷 분할은 `ffmpeg -c copy`
- 썸네일 추출은 `ffmpeg -frames:v 1`
- 싱크 보정은 원본당 1회 수행 후, 보정된 임시 MKV에서 여러 클립을 렌더링

### Metadata Export
- 각 클립마다 JSON 사이드카 생성
- 사이드카에는 원본 경로, 클립 범위, 썸네일 시점, 메타데이터, 출력 파일 경로 포함
- 클립보드 payload는 제목 + 설명 기준으로 생성

### MPC-BE Settings Import
- 앱 전용 프로필(`mpc-be64.ini`)을 별도 사용
- 기존 `MPC-BE` 설정이 INI 기반이면 그대로 복제
- 레지스트리 기반이면 미리보기 관련 핵심 설정만 앱 전용 INI로 export
- 기존 사용자 설정 원본은 직접 수정하지 않음

## 8. Non-goals
- 자동 폴더 감시
- 다건 배치 작업
- 범용 타임라인 편집기 수준 UI
- `LosslessCut`/`MKVToolNix GUI` 내장
- 완전 자동 제목 생성

## 9. Verification Targets
- OBS 녹화 폴더에서 최근 MKV를 바로 볼 수 있어야 함
- 한 MKV에서 여러 MP4 클립 생성 가능해야 함
- 클립별 제목/챕터/썸네일/업로드 여부를 독립적으로 관리해야 함
- 클립별 JSON 사이드카가 생성되어야 함
- 선택한 클립만 YouTube에 업로드 가능해야 함
- `MPC-BE` 설정 import는 원본 설정을 손상시키지 않아야 함

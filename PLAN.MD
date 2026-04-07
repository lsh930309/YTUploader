# Project: Automated Video Editor & YouTube Uploader (Windows 11)
**Target OS:** Windows 11
**Target Audience:** Single User (Personal Automation)
**Architecture Style:** Desktop GUI Application

## 1. Project Overview
이 프로젝트는 OBS로 녹화한 영상(MKV)의 오디오 싱크를 조정하고, 원하는 구간을 잘라 MP4로 변환(Remux)한 뒤, 사용자의 개인 데이터(템플릿)와 결합하여 YouTube에 자동으로 업로드하는 Windows 11 전용 데스크톱 앱입니다.

## 2. Technology Stack
| 분류 | 기술/라이브러리 | 선택 이유 및 원리 |
| :--- | :--- | :--- |
| **Language** | Python 3.11+ | 빠른 프로토타이핑 및 풍부한 API/CLI 래퍼 라이브러리 지원 |
| **GUI Framework** | `PyQt6` 또는 `CustomTkinter` | Windows 11 환경에 어울리는 모던한 UI 구성 및 비동기 처리(QThread 등) 용이 |
| **Video Processing** | `ffmpeg` (CLI), `mkvmerge` (CLI) | 백엔드 엔진. `-c copy` 옵션으로 재인코딩 없이 스트림 메타데이터만 조작하여 매우 빠른 처리 속도 확보 |
| **Data Storage** | `SQLite3` 또는 `JSON` | 사용자의 개인 데이터(자주 쓰는 태그, 설명 템플릿, 재생목록 ID 등) 로컬 저장 |
| **YouTube API** | `google-api-python-client`, `google-auth-oauthlib` | OAuth 2.0 인증 및 YouTube Data API v3를 통한 영상 업로드 |
| **Packaging** | `PyInstaller` (onedir) + `Inno Setup` | `onefile` 방식은 실행 시 임시 폴더에 압축을 푸는 시간(특히 ffmpeg 포함 시)이 오래 걸림. `onedir`로 빌드 후 Inno Setup으로 설치 파일을 만들어 실행 속도와 배포 편의성 극대화 |

## 3. Directory Structure
```text
project_root/
│
├── main.py                 # 앱 엔트리 포인트 (GUI 초기화)
├── core/
│   ├── video_processor.py  # ffmpeg, mkvmerge 제어 모듈
│   ├── youtube_uploader.py # YouTube API 업로드 로직
│   └── data_manager.py     # 로컬 메타데이터(JSON/SQLite) 관리
│
├── ui/
│   ├── main_window.py      # 메인 레이아웃 및 UI 컴포넌트
│   └── worker_threads.py   # GUI 멈춤 방지를 위한 백그라운드 스레드
│
├── assets/                 # 아이콘, 기본 썸네일 등
├── bin/                    # 외부 의존성 실행 파일 (ffmpeg.exe, mkvmerge.exe)
├── credentials/            # YouTube API client_secrets.json 및 token.pickle 저장소
│
├── build_script.py         # PyInstaller 빌드 자동화 스크립트
└── installer.iss           # Inno Setup 컴파일 스크립트
```

## 4. Phase-by-Phase Implementation Plan

### Phase 1: Environment & Dependency Management
* **목표:** `ffmpeg.exe`와 `mkvmerge.exe`를 앱 내부에 내장하고, 실행 시 올바른 경로를 찾도록 구성.
* **구현 지침:**
    * 에이전트는 `sys._MEIPASS` (PyInstaller 런타임 임시 경로)를 확인하는 유틸리티 함수를 작성하여, 개발 환경과 빌드 환경 모두에서 `bin/` 폴더 내의 실행 파일을 정상적으로 호출할 수 있게 해야 함.
    * *이유:* 사용자가 별도로 환경 변수(PATH)를 설정할 필요 없이 앱 설치만으로 즉시 작동하도록 독립성(Isolation)을 보장하기 위함.

### Phase 2: Metadata & Personal Data Management (Data DB)
* **목표:** 업로드에 필요한 정보를 매번 타이핑하지 않도록 템플릿화.
* **구현 지침:**
    * `data_manager.py`에 로컬 JSON 또는 SQLite를 읽고 쓰는 로직 구현.
    * 저장 항목: 기본 제목 포맷 (예: `[분류] {날짜} 녹화본`), 기본 설명 템플릿, 타임라인(챕터) 텍스트 구조, 고정 태그 리스트.
    * UI에서 이 템플릿을 불러와 현재 영상에 맞게 부분 수정만 할 수 있도록 연동.

### Phase 3: Video Processing Engine
* **목표:** `mkvmerge`로 싱크 조정 후, `ffmpeg`로 자르기 및 MP4 변환.
* **구현 지침:**
    * `subprocess.run()` 또는 `subprocess.Popen()`을 사용.
    * **명령어 파이프라인:**
        1.  `mkvmerge -o "{temp_mkv}" --sync 1:{delay_ms} "{input_mkv}"`
        2.  `ffmpeg -ss {start_time} -to {end_time} -i "{temp_mkv}" -c copy "{final_mp4}"`
    * *핵심 원리:* 이 과정은 무거운 I/O 작업이므로 **반드시 GUI 스레드와 분리된 별도의 Thread(예: QThread)에서 실행**해야 함. 그렇지 않으면 작업 중 앱이 응답 없음(Freezing) 상태에 빠짐. 진행 상태(Progress)를 UI로 emit 할 것.

### Phase 4: YouTube API Integration & Upload
* **목표:** OAuth 2.0 인증 처리 및 메타데이터, 썸네일, 영상 업로드.
* **구현 지침:**
    * 최초 실행 시 브라우저를 띄워 구글 로그인 승인을 받고, `token.pickle`로 토큰을 로컬에 저장하여 이후 자동 로그인 처리.
    * Resumable Upload(이어올리기) 방식을 사용하여 대용량 파일 업로드 시 네트워크 단절에 대비할 것 (`googleapiclient.http.MediaFileUpload` 사용 시 `resumable=True` 옵션).
    * 썸네일 이미지 파일이 있을 경우 영상 업로드 완료 직후 `thumbnails().set` API를 호출하여 등록.

### Phase 5: PyInstaller (onedir) & Inno Setup
* **목표:** 사용자 친화적인 `.exe` 설치 파일 생성.
* **구현 지침:**
    * `build_script.py` 작성: `PyInstaller --noconfirm --onedir --windowed --add-data "bin;bin" main.py` 실행.
    * `installer.iss` (Inno Setup) 작성: PyInstaller가 생성한 `dist/main/` 폴더 내부의 모든 파일을 `{app}` 디렉토리로 복사하고, 바탕화면에 바로가기를 생성하는 스크립트 작성.

## 5. Instructions for AI Agent
1.  **Do not skip error handling:** `subprocess` 실행 시 `stderr`를 캡처하여 예외 처리를 명확히 할 것.
2.  **Modularity:** 한 파일에 모든 코드를 넣지 말고, 섹션 3의 Directory Structure에 따라 모듈을 분리하여 작성할 것.
3.  **Step-by-step Execution:** 한 번에 전체 코드를 생성하지 말고, Phase 1 & 3 (핵심 기능) -> Phase 2 (데이터) -> Phase 4 (업로드) -> UI 연결 순서로 단계별 구현 및 확인을 요청받을 것.
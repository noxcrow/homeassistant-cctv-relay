# CCTV Relay

Home Assistant에서 Synology Surveillance Station의 Action Rule 이벤트를 수신하고, 지정한 카메라의 녹화 영상을 Telegram으로 전송하는 사용자 지정 통합입니다.

## 준비 사항

- Home Assistant
- Telegram Bot 통합과 `notify.*` 엔티티
- 접근 가능한 Synology DSM
- Surveillance Station 녹화 조회 권한이 있는 DSM 계정
- 사용할 Surveillance Station 카메라 2대
- 누락 이벤트 복구를 사용할 경우 Action Rule History 조회 권한

## 설치

`custom_components/cctv_relay` 폴더를 Home Assistant 설정 디렉터리의 같은 경로에 복사한 뒤 Home Assistant를 재시작합니다.

재시작 후 **설정 → 기기 및 서비스 → 통합 추가 → CCTV Relay**에서 다음 항목을 설정합니다.

- Synology DSM 주소
- SSL 인증서 검증 여부
- DSM 사용자 이름과 비밀번호
- Surveillance Station에서 조회된 목록의 카메라 1
- Surveillance Station에서 조회된 목록의 카메라 2
- Telegram Notify 엔티티
- 이벤트 전·후 영상 포함 시간
- Action Rule History 복구 여부와 확인 주기

카메라 1/2는 위치를 의미하지 않으며 DSM에서 자동 조회된 Surveillance Station 카메라 목록에서 원하는 두 대를 선택합니다.

## Action Rule

새 설치에서는 다음 Action Rule 이름을 권장합니다.

- `TG_CAMERA1_MOTION`
- `TG_CAMERA2_MOTION`
- `TG_CAMERA1_LOST`
- `TG_CAMERA1_RESTORED`
- `TG_CAMERA2_LOST`
- `TG_CAMERA2_RESTORED`

통합 등록 후 Home Assistant에 표시되는 웹훅 URL을 각 Action Rule에서 호출하도록 설정합니다.

웹훅 형식:

`/api/webhook/<webhook_id>?camera=<camera1|camera2>&event_type=<event_type>`

지원 이벤트 유형:

- `motion`
- `lost`
- `restored`
- `test`

기존 설치와의 호환을 위해 `front`/`back` 웹훅 값과 `TG_FRONT_*`/`TG_BACK_*` 규칙명도 계속 인식합니다.

## 보안

웹훅 ID, DSM 계정 정보, Telegram 토큰 등 인증정보를 공개 저장소나 로그에 기록하지 마십시오.

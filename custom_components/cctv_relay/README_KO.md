# CCTV Relay

Home Assistant에서 Synology Surveillance Station의 Action Rule 이벤트를 수신하고, 선택한 카메라의 녹화 영상을 Telegram으로 전송하는 사용자 지정 통합입니다.

## 준비 사항

- Home Assistant
- Telegram Bot 통합과 `notify.*` 엔티티
- 접근 가능한 Synology DSM
- Surveillance Station 카메라 및 녹화 조회 권한이 있는 DSM 계정
- 사용할 Surveillance Station 카메라 1대 이상
- 누락 이벤트 복구를 사용할 경우 Action Rule History 조회 권한

Home Assistant의 Synology DSM 통합은 필요하지 않습니다. CCTV Relay가 Surveillance Station WebAPI에 직접 연결합니다.

DSM 연결은 HTTPS만 허용합니다. 사설 IP, 로컬 호스트 및 Tailscale 내부 주소는 HTTPS 암호화를 유지한 채 인증서 검증만 자동 우회하며, 외부 주소는 유효한 TLS 인증서를 반드시 검증합니다.

## 설치 및 설정

`custom_components/cctv_relay` 폴더를 Home Assistant 설정 디렉터리의 같은 경로에 복사한 뒤 Home Assistant를 재시작합니다.

**설정 → 기기 및 서비스 → 통합 추가 → CCTV Relay**에서 DSM 연결정보를 입력하면 Surveillance Station 카메라 목록을 자동 조회합니다. 사용할 카메라를 1대 이상 다중 선택할 수 있으며 카메라 수에 고정 제한은 없습니다.

Telegram 알림에는 DSM에 설정된 실제 카메라명이 표시됩니다.

## Action Rule

카메라 식별자는 Surveillance Station의 실제 Camera ID를 사용합니다. Camera ID가 `7`인 경우 권장 규칙명과 웹훅은 다음과 같습니다.
Surveillance Station Action Rule의 HTTP Method는 **PUT**으로 설정합니다. CCTV Relay는 GET/POST 웹훅 요청을 허용하지 않습니다.

- `TG_CAMERA_7_MOTION` → `?camera=7&event_type=motion`
- `TG_CAMERA_7_LOST` → `?camera=7&event_type=lost`
- `TG_CAMERA_7_RESTORED` → `?camera=7&event_type=restored`

선택한 모든 카메라에 동일한 형식으로 적용합니다.

지원 이벤트 유형은 `motion`, `lost`, `restored`, `test`입니다.

## 영상 제한

감지 전 시간은 0~15초, 감지 후 시간은 1~30초의 직접 숫자 입력이며 합계는 최대 30초입니다. 최종 Telegram 전송 영상은 45MB를 넘지 않도록 제한됩니다.

## 보안

웹훅 ID, DSM 계정 정보, Telegram 토큰 등 인증정보를 공개 저장소나 로그에 기록하지 마십시오.

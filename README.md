# [KR] CCTV Relay

<p align="center">
  <img src="custom_components/cctv_relay/brand/icon.png" alt="[KR] CCTV Relay icon" width="128" height="128">
</p>

Synology Surveillance Station의 Action Rule 이벤트를 Home Assistant에서 받아 CCTV 영상을 Telegram으로 전달하는 커스텀 통합입니다.

## 주요 기능

- Synology Surveillance Station Action Rule 웹훅 수신
- 사용자가 지정한 Surveillance Station 카메라 ID 기반 이벤트 처리
- 움직임 이벤트 발생 시 전후 구간 영상을 자동 추출하여 Telegram 전송
- 카메라 연결 끊김/복구 알림 전송
- Action Rule History를 이용한 누락 이벤트 복구
- 중복 이벤트 방지 및 실패 시 자동 재시도
- 같은 카메라에서 짧은 시간 안에 반복되는 모션 트리거를 하나의 이벤트로 통합

## 요구 사항

- Home Assistant
- HACS
- Synology Surveillance Station
- Home Assistant의 **Synology DSM 통합은 필요하지 않음** (CCTV Relay가 DSM Surveillance Station API에 직접 연결)
- Telegram Bot 통합
- Home Assistant에서 Synology NAS에 접근 가능한 네트워크 환경
- Synology Surveillance Station에서 녹화 조회 및 Action Rule History 조회 권한이 있는 계정

## HACS 설치

1. Home Assistant에서 HACS를 엽니다.
2. 우측 상단 메뉴에서 **Custom repositories**를 선택합니다.
3. Repository에 아래 주소를 입력합니다.

   `https://github.com/noxcrow/homeassistant-cctv-relay`

4. Category는 **Integration**을 선택합니다.
5. 목록에서 **[KR] CCTV Relay**를 설치합니다.
6. Home Assistant를 재시작합니다.

## 수동 설치

HACS를 사용하지 않는 경우에도 직접 설치할 수 있습니다.

1. GitHub 저장소에서 최신 소스 코드를 다운로드합니다.
2. 저장소의 `custom_components/cctv_relay` 폴더를 Home Assistant 설정 디렉터리의 `custom_components/cctv_relay` 경로로 복사합니다.
3. Home Assistant를 재시작합니다.
4. **설정 → 기기 및 서비스 → 통합 추가**에서 **[KR] CCTV Relay**를 검색해 설정합니다.

업데이트 시에도 최신 `custom_components/cctv_relay` 폴더로 기존 파일을 교체한 후 Home Assistant를 재시작하면 됩니다.

## 통합 설정

Home Assistant 재시작 후:

1. **설정 → 기기 및 서비스 → 통합 추가**로 이동합니다.
2. **[KR] CCTV Relay**를 검색합니다.
3. 다음 항목을 입력합니다.

- Synology DSM 주소
- Synology 사용자 이름 / 비밀번호
- Surveillance Station에서 조회된 카메라 목록에서 사용할 카메라 1대 이상 선택
- Telegram Notify 엔티티
- 감지 전 영상 시간: 직접 숫자 입력 (0~15초)
- 감지 후 영상 시간: 직접 숫자 입력 (1~30초)
- Action Rule History 복구 사용 여부
- History 확인 주기

DSM 연결정보를 입력하면 Surveillance Station의 카메라 목록을 자동으로 조회합니다. 표시되는 카메라명과 ID를 확인해 사용할 카메라를 1대 이상 선택할 수 있으며, 카메라 수에 고정 제한은 없습니다. Telegram 알림에는 Surveillance Station에 설정된 실제 카메라명이 표시됩니다.

감지 전/후 영상 시간의 합계는 최대 30초입니다. Telegram Bot의 일반 비디오 업로드 한계(50MB)에 여유를 두기 위해 CCTV Relay는 최종 영상 파일을 45MB로 제한하며, 카메라 비트레이트가 높아 30초 미만 영상이 45MB를 넘는 경우에도 전송하지 않고 재시도/오류 처리합니다.

## Surveillance Station 설정

CCTV Relay를 처음 등록하면 Home Assistant 알림에 선택한 카메라별 웹훅 주소가 **1회 표시**됩니다. 업데이트, Home Assistant 재시작, 통합 재로드 또는 재구성만으로는 이 안내가 다시 생성되지 않습니다. 이 주소를 Synology Surveillance Station의 **Action Rule**에 등록합니다.

### 1. Action Rule 생성

1. Surveillance Station을 엽니다.
2. **Action Rule** 메뉴로 이동합니다.
3. **추가(Add)** 를 선택하여 새 규칙을 생성합니다.
4. 규칙 유형은 카메라 이벤트가 발생할 때 동작하는 이벤트 기반 규칙으로 설정합니다.
5. 이벤트 소스에서 대상 카메라를 선택하고 필요한 이벤트를 지정합니다.
6. 동작(Action)에서 **HTTP/Webhook 요청 전송** 항목을 선택합니다. 메뉴 명칭은 Surveillance Station 버전에 따라 `HTTP Request`, `Webhook` 등으로 표시될 수 있습니다.
7. HTTP Method는 반드시 **PUT**으로 설정합니다. CCTV Relay는 GET/POST 웹훅 요청을 허용하지 않습니다.
8. URL에는 Home Assistant에서 표시된 해당 카메라/이벤트의 전체 웹훅 주소를 입력합니다.
9. 규칙을 저장하고 활성화합니다.

### 2. 카메라별 권장 규칙

카메라 식별자는 통합 설정에서 확인한 **Surveillance Station 실제 Camera ID**를 사용합니다. 예를 들어 Camera ID가 `7`인 경우 다음과 같이 구성합니다.

| 용도 | 권장 Action Rule 이름 | 이벤트 | Webhook query |
| --- | --- | --- | --- |
| 움직임 감지 | `TG_CAMERA_7_MOTION` | 카메라 움직임 감지 | `?camera=7&event_type=motion` |
| 연결 끊김 | `TG_CAMERA_7_LOST` | 카메라 연결 끊김/오프라인 | `?camera=7&event_type=lost` |
| 연결 복구 | `TG_CAMERA_7_RESTORED` | 카메라 재연결/온라인 복구 | `?camera=7&event_type=restored` |

카메라가 여러 대라면 각 Camera ID에 대해 동일한 형식으로 규칙을 생성합니다. 예를 들어 ID `12` 카메라는 `TG_CAMERA_12_MOTION`, `TG_CAMERA_12_LOST`, `TG_CAMERA_12_RESTORED` 형식을 사용합니다.

지원 이벤트 유형은 `motion`, `lost`, `restored`입니다.

### 3. Webhook URL

통합이 표시하는 전체 URL을 그대로 사용하는 것을 권장합니다. URL 형식은 다음과 같습니다.

`http(s)://<Home Assistant 내부주소>/api/webhook/<webhook_id>?camera=<camera_id>&event_type=<event_type>`

예:

`http(s)://<Home Assistant 내부주소>/api/webhook/<webhook_id>?camera=7&event_type=motion`

웹훅은 **로컬 요청만 허용**하므로 Surveillance Station에서 Home Assistant 내부 주소로 직접 접근할 수 있어야 합니다. 웹훅 ID는 인증에 사용되는 비밀값이므로 외부에 공개하지 마십시오.

### 4. Action Rule History 복구

통합 설정에서 **Action Rule History 복구**를 활성화하면 웹훅이 일시적으로 누락되더라도 Surveillance Station의 Action Rule 실행 이력을 조회해 이벤트를 복구할 수 있습니다.

History 복구를 사용하려면 DSM 계정에 Action Rule History 조회 권한이 있어야 하며, 권장 규칙명인 `TG_CAMERA_<Camera ID>_MOTION`, `TG_CAMERA_<Camera ID>_LOST`, `TG_CAMERA_<Camera ID>_RESTORED` 형식을 유지해야 합니다.

### 5. 설정 확인

설정 후 실제 카메라 이벤트를 발생시켜 다음을 확인합니다.

- Surveillance Station Action Rule 실행 이력에 규칙이 정상 실행되는지
- HTTP 요청이 실패하지 않는지
- Home Assistant CCTV Relay 진단 센서에서 대기/실패 이벤트가 증가하지 않는지
- `motion` 이벤트에서 Telegram 영상이 정상 전송되는지
- `lost`/`restored` 이벤트에서 Telegram 상태 알림이 정상 전송되는지

## Telegram 설정

Home Assistant의 Telegram Bot 통합으로 생성된 Notify 엔티티를 CCTV Relay 설정에서 선택합니다.

움직임 이벤트가 발생하면 해당 Telegram 대상에 영상과 캡션이 전송됩니다.

## 업데이트

HACS에서 CCTV Relay 업데이트가 표시되면 업데이트 후 Home Assistant를 재시작합니다.

설정값은 Home Assistant Config Entry에 저장되므로 일반적인 업데이트에서는 다시 설정할 필요가 없습니다.

## 문제 해결

### 영상이 전송되지 않는 경우

- Synology Surveillance Station에서 해당 시간대 녹화가 존재하는지 확인합니다.
- 선택한 카메라가 Surveillance Station에서 현재 조회 가능한지 확인합니다.
- Synology 계정에 녹화 재생/조회 권한이 있는지 확인합니다.
- Home Assistant에서 DSM 주소로 접근 가능한지 확인합니다.
- Telegram Notify 엔티티가 정상 동작하는지 확인합니다.

### SSL 오류가 발생하는 경우

CCTV Relay는 DSM 연결에 HTTPS를 강제합니다. 사설 IP, 단일 호스트명, `localhost`, `.local`/`.lan`/`.home`/`.home.arpa`/`.internal` 호스트, Tailscale/CGNAT `100.64.0.0/10`, IPv6 ULA는 내부 주소로 판단하여 **HTTPS 암호화는 유지하고 인증서 체인 및 호스트명 검증만 자동 우회**합니다.

공인 IP 또는 외부 도메인은 인증서 검증을 강제하므로 DSM 인증서의 호스트명, 만료일, 인증서 체인이 정상이어야 합니다. 외부 주소의 인증서 검증 우회는 지원하지 않습니다.

### 이벤트가 누락되는 경우

Action Rule History 복구 기능이 활성화되어 있는지 확인하고, Synology 계정에 Action Rule History 조회 권한이 있는지 확인합니다.

### 중복 알림이 발생하는 경우

Synology Action Rule에서 동일 이벤트에 대해 웹훅 액션이 여러 개 등록되어 있지 않은지 확인합니다. 또한 이전 자동화나 별도 Telegram 전송 자동화가 남아 있지 않은지 확인합니다.

### 주요 보안 및 운영 고지

- CCTV Relay 웹훅은 **로컬 요청만 허용**하며 HTTP Method는 **PUT만 허용**합니다. 웹훅 ID는 인증 비밀값으로 취급하고 외부에 공개하지 마십시오.
- 활성 이벤트 큐는 최대 **1,000건**으로 제한되며, 초과 요청은 거부됩니다. 동일 호출자의 웹훅은 **60초당 120회**로 제한됩니다.
- 이벤트 처리는 최대 **20회**까지만 재시도하고 이후 `failed` 상태로 격리합니다. `failed` 건수는 CCTV Relay 진단 센서에서 확인할 수 있습니다. 오래된 `sent`/`failed` 이벤트는 Action Rule History 사용 여부와 관계없이 30일 후 정리됩니다.
- 내부 주소는 자체 서명 인증서 호환성을 위해 인증서 체인/호스트명 검증을 우회하지만 HTTPS 암호화 자체는 유지합니다. 신뢰할 수 없는 LAN에서는 내부 TLS 검증 우회 환경을 사용하지 마십시오.
- Synology 영상 다운로드/ZIP 압축 해제는 256MB 안전 한도를 적용하고, Telegram 전송 영상은 45MB로 제한합니다.

## 보안 주의사항

- 웹훅 주소와 웹훅 ID를 공개하지 마십시오.
- DSM 관리자 계정보다는 필요한 최소 권한만 가진 전용 계정 사용을 권장합니다.
- Synology 계정 비밀번호나 Telegram 관련 토큰을 GitHub 또는 공개 문서에 기록하지 마십시오.
- Home Assistant와 DSM 간 통신은 HTTPS만 사용합니다. 내부 주소는 인증서 검증만 우회하며 평문 HTTP로 전환하지 않습니다.

## 라이선스 및 문의

문제 보고 및 기능 문의는 GitHub Issues를 이용해 주세요.

`https://github.com/noxcrow/homeassistant-cctv-relay/issues`

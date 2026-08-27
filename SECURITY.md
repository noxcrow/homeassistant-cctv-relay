# Security Policy

이 저장소에는 운영 자격정보를 저장하지 않습니다.

금지 항목:
- DSM/Home Assistant 사용자 비밀번호
- API token, SynoToken, session ID, webhook secret
- `secrets.yaml`, `.storage` 내용
- TLS/SSH private key 및 실제 인증서 비밀정보
- 자격증명이 포함된 RTSP/HTTP URL

민감정보가 커밋된 경우 해당 자격정보를 즉시 폐기/회전한 뒤 Git 이력 정리를 수행해야 합니다.

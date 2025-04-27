# SSE 알림 서비스

FastAPI와 Redis를 사용한 Server-Sent Events(SSE) 기반 실시간 알림 시스템입니다.

### 기술 구성
- **백엔드**: FastAPI (Python)
- **프론트엔드**: HTML, JavaScript, Bootstrap
- **메시지 브로커**: Redis (PubSub 기능)
- **실시간 통신**: Server-Sent Events (SSE)

### 작동 방식
1. 사용자는 웹 페이지에서 자신의 ID를 입력하고 연결
2. 서버는 사용자별 고유 채널을 생성하고 SSE 연결 유지
3. Redis PubSub이 사용자별 채널에 메시지를 발행
4. 서버는 Redis에서 받은 메시지를 SSE 채널을 통해 클라이언트로 전송
5. 클라이언트는 이 메시지를 받아 브라우저 알림과 화면 내 알림 표시

### 기술적 특징
- 단방향 통신 채널로 서버에서 클라이언트로 실시간 데이터 전송
- Redis를 사용한 메시지 분배 시스템
- 영구 연결을 유지하면서 이벤트 스트림 관리
- JSON 형식으로 구조화된 메시지 전송


## 기능

- 사용자별 실시간 알림 수신 (Redis 채널: `channel:{user_id}`)
- 브라우저 푸시 알림 지원
- 개별 또는 다중 사용자에게 알림 전송 가능
- 사용자별 알림 히스토리 저장 및 조회

## 설치 및 실행

### 요구사항

- Python 3.9+
- Redis 서버

### 환경 설정

1. 패키지 설치

```bash
# uv 패키지 매니저 설치 (없을 경우)
pip install uv

# 의존성 설치
uv pip install -e .
```

2. Redis 연결 설정

`.env` 파일에서 Redis 연결 정보를 설정합니다:

```
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=
```

### 실행

```bash
uvicorn app.main:app --reload
또는 
uv run python app/main.py
```

서버가 실행되면 http://localhost:8000 으로 접속할 수 있습니다.

## API 엔드포인트

- `GET /` - 메인 페이지 (HTML)
- `GET /events/{user_id}` - SSE 이벤트 스트림 엔드포인트
- `POST /notify/{user_id}` - 특정 사용자에게 알림 전송
- `POST /broadcast` - 여러 사용자에게 알림 전송

### 알림 전송 예제

**단일 사용자에게 알림 전송:**

```bash
curl -X POST http://localhost:8000/notify/user123 \
  -H "Content-Type: application/json" \
  -d '{"title": "새 메시지", "message": "안녕하세요! 새 메시지가 도착했습니다."}'
```

**여러 사용자에게 알림 전송:**

```bash
curl -X POST http://localhost:8000/broadcast \
  -H "Content-Type: application/json" \
  -d '{"users": ["user1", "user2", "user3"], "title": "공지사항", "message": "서비스가 업데이트 되었습니다."}'
```

## 사용자 ID 관리

사용자 ID는 클라이언트 측에서 제공합니다. 실제 운영 환경에서는 인증 시스템과 연동하여 사용자 식별 및 권한 관리가 필요합니다.

## 고도화 제안 포인트

### 사용자 인증/권한 시스템
- 로그인 기능으로 안전한 사용자 식별
- 권한 기반 알림 그룹 설정

### 알림 영속성
- 데이터베이스에 알림 저장 (MongoDB, PostgreSQL)
- 읽음/안읽음 상태 관리
- 알림 이력 조회 기능

### 알림 템플릿 시스템
- 다양한 알림 유형 템플릿화
- 커스텀 알림 디자인 지원

### 알림 우선순위 및 카테고리
- 중요도에 따른 알림 분류
- 카테고리별 필터링 기능

### 성능 최적화
- Redis 클러스터링 도입
- 대규모 알림 처리를 위한 큐 시스템 (RabbitMQ, Kafka)

### 관리자 대시보드
- 알림 모니터링 및 관리 인터페이스
- 사용자별/그룹별 알림 발송 및 통계

### 다중 채널 지원
- 브라우저 알림 외 이메일, SMS, 푸시 알림 등 추가
- 사용자별 알림 채널 선호도 설정

### 오프라인 지원
- Service Worker를 통한 오프라인 알림 큐
- 재연결 시 알림 동기화

### 클라이언트 라이브러리
- 다양한 프레임워크용 클라이언트 SDK 개발
- React, Vue, Angular 컴포넌트 제공

### 로깅 및 분석
- 알림 전송/수신 로깅
- 통계 및 분석 도구 통합
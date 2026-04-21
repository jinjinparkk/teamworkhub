# TeamWorkHub

Gmail → Google Drive + Obsidian Markdown 자동 동기화 서비스. GCP Cloud Run 위에서 동작하며 Claude Haiku 4.5로 이메일을 요약·분석합니다.

```
Cloud Scheduler  ──POST /sync────▶  Cloud Run
Cloud Scheduler  ──POST /daily───▶  Cloud Run
Cloud Scheduler  ──POST /weekly──▶  Cloud Run
Cloud Scheduler  ──POST /monthly─▶  Cloud Run
                                         │
                                  Gmail API (OAuth)
                                  list messages by label
                                         │
                                  Claude Haiku 4.5 (optional)
                                  요약 · 담당자 · 우선순위 · 카테고리 분석
                                         │
                                  Drive API + Local Obsidian vault
                                  .md 파일 upsert (멱등성 보장)
```

> **MVP scope:** Drive + 로컬 Obsidian 출력.  Git push · Gmail watch / Pub/Sub는 Phase 2.

---

## 기능 요약

| 엔드포인트 | 출력 파일 | 설명 |
|---|---|---|
| `POST /sync` | `twh_{msgId}.md` | 개별 메일 → Drive 개별 노트 |
| `POST /daily` | `YYYY-MM-DD.md` | 야간(18:00~08:59) 메일 → 일간 다이제스트 |
| `POST /weekly` | `YYYY-WNN.md` | 주간(월~금) 메일 → 주간 리포트 |
| `POST /monthly` | `YYYY-MM.md` | 월간 메일 → 월간 리포트 |
| `POST /dashboard` | `Dashboard.md` | Dataview 쿼리 기반 대시보드 |
| `GET /health` | — | 헬스체크 (Cloud Run liveness probe) |

### Obsidian 연동 기능

- **Claude 분석**: 요약 불릿 · 담당자 · 우선순위(🔴긴급/🟡보통/🟢낮음) · 카테고리(📊보고/✅승인요청/📢공지/📅미팅/📧일반) 자동 태깅
- **담당자 추출**: 이름+직함 정규식 우선, Claude 추론 fallback
- **스레드 감지**: RE:/FW: 정규화 → 같은 주제 메일 간 `[[날짜#섹션]]` 위키링크
- **체크박스**: `- [ ] 처리 완료` — Obsidian Tasks 플러그인으로 미처리 항목 추적
- **Dataview 인라인 필드**: `담당자::` `우선순위::` `카테고리::` — 노트 간 집계 쿼리 가능
- **담당자 페이지**: `/daily` 실행 시 `{이름}.md` 자동 생성 (담당 메일 Dataview 조회)

---

## Quick start (로컬)

```bash
# 1. 의존성 설치
pip install -r requirements-dev.txt

# 2. 환경변수 복사
cp .env.example .env

# 3. OAuth 토큰 발급 (최초 1회)
#    a. Cloud Console → APIs & Services → Credentials
#       → OAuth 2.0 Client ID → Desktop app → JSON 다운로드
#       → scripts/client_secret.json 으로 저장
#    b. 토큰 발급 (브라우저 인증):
py -3 scripts/get_token.py
#    → 출력된 CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN 을 .env 에 입력
#    c. .env 에 DRIVE_OUTPUT_FOLDER_ID 도 입력 (Drive 폴더 URL 마지막 세그먼트)

# 4. 서버 실행 (.env 자동 로드)
py -3 -m src
# or: uvicorn src.app:app --reload --port 8080

# 5. 헬스체크
curl http://localhost:8080/health
# {"status":"ok","service":"teamworkhub"}

# 6. 개별 메일 동기화 (Gmail → Drive)
curl -X POST http://localhost:8080/sync
# {"status":"ok","run_id":"a1b2c3d4","processed":3,"skipped":0,"errors":0}

# 7. 일간 다이제스트 (야간 메일 → YYYY-MM-DD.md)
curl -X POST http://localhost:8080/daily
# {"status":"ok","run_id":"b2c3d4e5","date":"2026-04-02","email_count":5}

# 8. 주간 리포트 (이번 주 메일 → YYYY-WNN.md)
curl -X POST http://localhost:8080/weekly
# {"status":"ok","run_id":"c3d4e5f6","week":"2026-W14","email_count":23}

# 9. 월간 리포트 (이번 달 메일 → YYYY-MM.md)
curl -X POST http://localhost:8080/monthly
# {"status":"ok","run_id":"d4e5f6g7","month":"2026-04","email_count":87}

# 10. 대시보드 생성 (Dataview 쿼리 기반 Dashboard.md)
curl -X POST http://localhost:8080/dashboard
# {"status":"ok","run_id":"e5f6g7h8"}
```

---

## Environment variables

### 필수

| Variable | Description |
|---|---|
| `DRIVE_OUTPUT_FOLDER_ID` | Drive 폴더 ID (`/sync` 출력 기본 폴더) |
| `GOOGLE_OAUTH_CLIENT_ID` | OAuth 2.0 클라이언트 ID (Desktop app) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | OAuth 2.0 클라이언트 시크릿 |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | 장기 리프레시 토큰 — 프로덕션에서는 Secret Manager 사용 |

### 선택 — 기본 동작

| Variable | Default | Description |
|---|---|---|
| `GMAIL_LABEL_ID` | `INBOX` | 동기화할 Gmail 라벨 ID |
| `MAX_MESSAGES_PER_RUN` | `50` | 1회 실행당 최대 메시지 수 |
| `ANTHROPIC_API_KEY` | `""` | Anthropic API 키 (비어있으면 요약 비활성화) |
| `TIMEZONE` | `UTC` | 노트 타임스탬프·다이제스트 기준 시간대 (예: `Asia/Seoul`) |
| `PORT` | `8080` | HTTP 리슨 포트 (Cloud Run 자동 주입) |
| `LOG_FORMAT` | `json` | `json` (프로덕션) / `pretty` (로컬 개발) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

### 선택 — 로컬 Obsidian 출력

| Variable | Default | Description |
|---|---|---|
| `LOCAL_OUTPUT_DIR` | `""` | 개별 노트 저장 경로 (Obsidian vault 하위 폴더) |
| `LOCAL_DAILY_OUTPUT_DIR` | `""` | 일간 노트 폴더; 없으면 `LOCAL_OUTPUT_DIR` fallback |
| `LOCAL_WEEKLY_OUTPUT_DIR` | `""` | 주간 리포트 폴더; 없으면 `LOCAL_DAILY_OUTPUT_DIR` fallback |
| `LOCAL_MONTHLY_OUTPUT_DIR` | `""` | 월간 리포트 폴더; 없으면 `LOCAL_WEEKLY_OUTPUT_DIR` fallback |
| `LOCAL_DASHBOARD_DIR` | `""` | Dashboard.md · 담당자 페이지 저장 폴더 |

### 선택 — Drive 출력 폴더 (엔드포인트별)

| Variable | Default | Description |
|---|---|---|
| `DAILY_OUTPUT_FOLDER_ID` | `""` | 일간 노트용 Drive 폴더; 없으면 `DRIVE_OUTPUT_FOLDER_ID` fallback |
| `WEEKLY_OUTPUT_FOLDER_ID` | `""` | 주간 리포트용 Drive 폴더 |
| `MONTHLY_OUTPUT_FOLDER_ID` | `""` | 월간 리포트용 Drive 폴더 |

### 선택 — 멀티 계정

| Variable | Default | Description |
|---|---|---|
| `GMAIL_ACCOUNTS_JSON` | `""` | `[{"email":"...", "refresh_token":"..."}]` 형태 JSON 배열 |

---

### Secret Manager (프로덕션)

`GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN`, `ANTHROPIC_API_KEY`는 Cloud Run 환경변수에 평문으로 저장하지 마세요.

```bash
# 시크릿 생성 (최초 1회)
gcloud secrets create twh-oauth-client-id     --replication-policy=automatic
gcloud secrets create twh-oauth-client-secret --replication-policy=automatic
gcloud secrets create twh-oauth-refresh-token --replication-policy=automatic
gcloud secrets create twh-anthropic-api-key    --replication-policy=automatic

# 값 업로드
echo -n "YOUR_CLIENT_ID"     | gcloud secrets versions add twh-oauth-client-id     --data-file=-
echo -n "YOUR_CLIENT_SECRET" | gcloud secrets versions add twh-oauth-client-secret  --data-file=-
echo -n "YOUR_REFRESH_TOKEN" | gcloud secrets versions add twh-oauth-refresh-token  --data-file=-
echo -n "YOUR_ANTHROPIC_KEY" | gcloud secrets versions add twh-anthropic-api-key    --data-file=-
```

Cloud Run은 배포 시 `--update-secrets` 옵션으로 시크릿을 환경변수로 주입합니다.

---

## OAuth 스코프

| Scope | 이유 |
|---|---|
| `https://www.googleapis.com/auth/gmail.readonly` | 메시지 목록 조회·읽기 전용 (발송·삭제 불가) |
| `https://www.googleapis.com/auth/drive.file` | 이 앱이 생성한 파일만 수정 가능 (최소 권한) |

---

## 멱등성 규칙

같은 `messageId`로 `/sync`를 두 번 실행해도 노트가 중복되거나 손상되지 않습니다.

`.md` 파일이 **커밋 마커**: Drive에 `twh_{messageId}.md`가 이미 존재하면 메시지 전체를 스킵합니다.

| 시나리오 | 동작 |
|---|---|
| 처음 보는 `messageId` | 첨부 업로드 → `.md` 작성 → upsert (생성) |
| `.md` 이미 Drive에 존재 | **전체 스킵** — API 쓰기 0회 |
| 첨부파일 이미 Drive에 존재 | 기존 파일 반환 (0 bytes 기록) |
| 새 `messageId`, 동일 제목 | 새 `.md` 생성 (파일명은 제목이 아닌 `messageId` 기반) |

Drive 파일명 패턴:
- 노트: `twh_{sanitised_messageId}.md`
- 첨부파일: `{messageId}_{sanitised_filename}`

---

## 응답 형식

모든 엔드포인트는 항상 **HTTP 200** 반환. `status` 필드로 성공 여부 확인.

```jsonc
// POST /sync
{
  "status":    "ok",        // "ok" | "skipped" | "partial" | "error"
  "run_id":    "a1b2c3d4",  // 8자리 상관관계 ID (로그에도 동일하게 기록)
  "processed": 3,
  "skipped":   7,
  "errors":    0,
  "note":      "..."        // status != "ok" 일 때만 존재
}

// POST /daily
{
  "status":      "ok",
  "run_id":      "b2c3d4e5",
  "date":        "2026-04-02",
  "email_count": 5
}

// POST /weekly
{
  "status":      "ok",
  "run_id":      "c3d4e5f6",
  "week":        "2026-W14",
  "email_count": 23
}

// POST /monthly
{
  "status":      "ok",
  "run_id":      "d4e5f6g7",
  "month":       "2026-04",
  "email_count": 87
}

// POST /dashboard
{
  "status":  "ok",
  "run_id":  "e5f6g7h8"
}
```

| `status` | 의미 |
|---|---|
| `ok` | 정상 완료 (메시지 0건도 ok) |
| `skipped` | 필수 환경변수 미설정 — 실행 안 됨 |
| `partial` | 일부 성공·일부 실패 (`/sync` 전용) |
| `error` | 인증 실패 또는 전체 실패 |

---

## 프로젝트 구조

```
teamworkhub/
├── src/
│   ├── __init__.py
│   ├── __main__.py           # uvicorn 진입점
│   ├── app.py                # FastAPI 앱 — 모든 엔드포인트 + _collect_messages 헬퍼
│   ├── auth.py               # OAuth2 리프레시 토큰 기반 credentials 빌더
│   ├── config.py             # 환경변수 → Config 데이터클래스, validate_for_sync()
│   ├── logging_cfg.py        # 구조화 JSON 로깅
│   ├── gmail_client.py       # 메시지 목록·조회·첨부 다운로드 — Gmail API
│   ├── drive_client.py       # 파일 업로드·upsert — Drive API (멱등성)
│   ├── md_writer.py          # 개별 메시지 Obsidian .md 작성
│   ├── summarizer.py         # Claude Haiku 4.5 분석 — AnalysisResult 반환
│   ├── assignee.py           # 담당자 추출 (정규식 → Claude fallback)
│   ├── daily_writer.py       # 일간 다이제스트 작성 (YYYY-MM-DD.md)
│   ├── weekly_writer.py      # 주간 리포트 작성 (YYYY-WNN.md)
│   ├── monthly_writer.py     # 월간 리포트 작성 (YYYY-MM.md)
│   └── dashboard_writer.py   # Dashboard.md + 담당자 페이지 작성
├── tests/
│   ├── test_app.py
│   ├── test_auth.py
│   ├── test_config.py
│   ├── test_drive_client.py
│   ├── test_gmail_client.py
│   ├── test_logging_cfg.py
│   ├── test_md_writer.py
│   ├── test_daily_writer.py
│   ├── test_weekly_writer.py
│   ├── test_monthly_writer.py
│   ├── test_dashboard_writer.py
│   ├── test_assignee.py
│   └── test_sync_integration.py
├── scripts/
│   ├── get_token.py          # OAuth 토큰 발급 헬퍼 (브라우저 인증)
│   └── client_secret.json    # Desktop OAuth 클라이언트 JSON — gitignore 처리
├── .env.example
├── .dockerignore
├── .gitignore
├── Dockerfile
├── requirements.txt
└── requirements-dev.txt
```

---

## 테스트

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
# 354 passed
```

실제 API 호출 없음 — 모든 Google API 클라이언트·Claude는 mock 처리됩니다.

---

## Obsidian 플러그인 설정

Dashboard와 주간/월간 리포트를 완전히 활용하려면 아래 플러그인이 필요합니다.

| 플러그인 | 용도 |
|---|---|
| **Dataview** | 담당자·카테고리·우선순위 집계 쿼리, 대시보드 |
| **Tasks** | 일간→주간→월간 미처리 항목 실시간 동기화 |

설치: Settings → Community plugins → 검색 → Install → Enable

### Obsidian 폴더 구조 (권장)

```
Obsidian Vault/
├── TeamWorkHub/            # /sync 개별 메일 노트
├── TeamWorkHub_Daily/      # /daily 일간 다이제스트
├── TeamWorkHub_Weekly/     # /weekly 주간 리포트
├── TeamWorkHub_Monthly/    # /monthly 월간 리포트
└── TeamWorkHub_Dashboard/  # /dashboard + 담당자 페이지
    ├── Dashboard.md
    ├── 박은진.md
    └── ...
```

---

## Deploy to Cloud Run

### 1. API 활성화

```bash
gcloud services enable \
  run.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com
```

### 2. 서비스 계정 & IAM

```bash
export PROJECT_ID=$(gcloud config get-value project)
export SA=twh-runner

gcloud iam service-accounts create $SA --display-name="TeamWorkHub runner"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/logging.logWriter"
```

### 3. 빌드 & 푸시

```bash
export REGION=asia-northeast3
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-images/teamworkhub:latest"

# Artifact Registry 저장소 생성 (최초 1회)
gcloud artifacts repositories create cloud-run-images \
  --repository-format=docker --location=$REGION

gcloud builds submit --tag $IMAGE
```

### 4. 배포

```bash
gcloud run deploy teamworkhub \
  --image $IMAGE \
  --platform managed \
  --region $REGION \
  --service-account "${SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --no-allow-unauthenticated \
  --memory 512Mi \
  --timeout 300 \
  --set-env-vars "GMAIL_LABEL_ID=INBOX,DRIVE_OUTPUT_FOLDER_ID=YOUR_FOLDER_ID,TIMEZONE=Asia/Seoul" \
  --update-secrets "GOOGLE_OAUTH_CLIENT_ID=twh-oauth-client-id:latest" \
  --update-secrets "GOOGLE_OAUTH_CLIENT_SECRET=twh-oauth-client-secret:latest" \
  --update-secrets "GOOGLE_OAUTH_REFRESH_TOKEN=twh-oauth-refresh-token:latest" \
  --update-secrets "ANTHROPIC_API_KEY=twh-anthropic-api-key:latest"
```

### 5. Cloud Scheduler 잡 등록

```bash
export SERVICE_URL=$(gcloud run services describe teamworkhub \
  --region $REGION --format="value(status.url)")

# Scheduler → Cloud Run 호출 권한 부여
gcloud run services add-iam-policy-binding teamworkhub \
  --region $REGION \
  --member="serviceAccount:${SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

# 잡 1: 개별 메일 동기화 — 평일 08:00 KST
gcloud scheduler jobs create http teamworkhub-sync \
  --location $REGION \
  --schedule "0 8 * * 1-5" \
  --time-zone "Asia/Seoul" \
  --uri "${SERVICE_URL}/sync" \
  --http-method POST \
  --oidc-service-account-email "${SA}@${PROJECT_ID}.iam.gserviceaccount.com"

# 잡 2: 일간 다이제스트 — 평일 09:00 KST
gcloud scheduler jobs create http teamworkhub-daily \
  --location $REGION \
  --schedule "0 9 * * 1-5" \
  --time-zone "Asia/Seoul" \
  --uri "${SERVICE_URL}/daily" \
  --http-method POST \
  --oidc-service-account-email "${SA}@${PROJECT_ID}.iam.gserviceaccount.com"

# 잡 3: 주간 리포트 — 매주 금요일 18:00 KST
gcloud scheduler jobs create http teamworkhub-weekly \
  --location $REGION \
  --schedule "0 18 * * 5" \
  --time-zone "Asia/Seoul" \
  --uri "${SERVICE_URL}/weekly" \
  --http-method POST \
  --oidc-service-account-email "${SA}@${PROJECT_ID}.iam.gserviceaccount.com"

# 잡 4: 월간 리포트 — 매월 마지막 날 17:00 KST
gcloud scheduler jobs create http teamworkhub-monthly \
  --location $REGION \
  --schedule "0 17 28-31 * *" \
  --time-zone "Asia/Seoul" \
  --uri "${SERVICE_URL}/monthly" \
  --http-method POST \
  --oidc-service-account-email "${SA}@${PROJECT_ID}.iam.gserviceaccount.com"
```

> 월간 잡은 28~31일에만 실행되므로 실질적으로 매월 마지막 며칠에 여러 번 실행됩니다. `/monthly`는 항상 현재 달 전체를 수집하므로 멱등성이 보장됩니다.

---

## Phase 2 (MVP 외)

명시적으로 **구현 범위 외** 기능:

- Gmail watch API + Pub/Sub push → Cloud Run (실시간 트리거)
- 액션 아이템 자동 추출 (이메일 → 세분화된 할 일 목록)
- 긴급 메일 즉시 알림 (Telegram / Discord)
- 뉴스레터·광고 자동 필터링
- Git push Obsidian vault 동기화

스텁 훅은 `src/app.py`와 `src/gmail_client.py`의 `# Phase 2` 주석으로 표시됩니다.

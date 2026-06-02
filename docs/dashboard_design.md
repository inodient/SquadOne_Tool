# SquadOne 셀러용 대시보드 — 설계 & 운영 문서

뉴스 트렌드 → 상품군 추천 파이프라인의 산출물을 **실사용 셀러**에게 보여주는 읽기 전용 웹 대시보드.

- 백엔드: 기존 FastAPI(`rest_mcp_server/app.py`)에 조회 전용 라우터 `/v1/view/*` 추가.
- 프론트: `frontend/` — Vite + React + TypeScript + Recharts.
- 데이터: Postgres `newstrend` 스키마(이미 적재된 산출물만 **읽기**). 파이프라인 실행/Qdrant/Ollama와 무관.

---

## 1. 페이지 구성

| 경로 | 페이지 | 내용 | 소스 테이블 |
|---|---|---|---|
| `/` | **이번 주 추천**(Home) | KPI 4종 + 추천 상품군 카드 + 주목 트렌드 | `product_candidates`, `trend_timeseries`, `weekly_keyword_freq` |
| `/trends` | **트렌드 익스플로러** | 생명주기 보드(4단계) + 연관 키워드 군집 버블 | `trend_timeseries`, `trend_groups` |
| `/trend/:keyword` | **트렌드 상세**(추천 사슬) | 주간 요약·핵심문장 + z-score 라인 + 언론사 도넛 + 근거 뉴스 | `trend_timeseries`, `keysentence`, `z_score_keywords`, `weekly_keywords`, `trend_contexts` |
| `/compare` | **주차 비교** | 이전 주 대비 추천 신규/유지/탈락 | `product_candidates` |

상단 헤더의 **주차 셀렉터**가 전역 상태(`src/week.tsx`, localStorage 보존)로 모든 페이지에 적용된다.
미선택 시 최신 주차로 폴백.

---

## 2. REST 조회 API (`/v1/view/*`)

`rest_mcp_server/views.py` (APIRouter). 인증은 기존과 동일 — env `REST_MCP_API_KEY` 설정 시에만 `X-API-Key` 강제.

| 엔드포인트 | repository 함수 | 비고 |
|---|---|---|
| `GET /v1/view/weeks` | `read_distinct_weeks` | 주차 목록 + 최신 |
| `GET /v1/view/summary?week=` | `read_status_counts`, `read_keyword_count`, `read_product_candidates` | KPI |
| `GET /v1/view/recommendations?week=` | `read_product_candidates`, `read_reports` | 상품 + LLM 리포트 |
| `GET /v1/view/trends?week=&status=` | `read_trends` | 생명주기 필터 |
| `GET /v1/view/groups?week=` | `read_trend_groups` | 군집 버블 |
| `GET /v1/view/trend?keyword=&week=` | `read_trend_detail` | 시계열+근거+핵심문장 |
| `GET /v1/view/zscore-series?keyword=&limit=` | `read_zscore_series` | z-score 추이 |
| `GET /v1/view/sources?keyword=&week=` | `read_source_distribution` | 언론사 분포 |

`week` 미지정 → 최신 주차 폴백. 조회 함수는 모두 `db/repository.py` 하단 "대시보드 조회" 섹션에 위치.

CORS: `app.py`가 env `DASHBOARD_ORIGINS`(콤마구분, 기본 `http://localhost:5180,http://127.0.0.1:5180`) 허용.
(5173은 동일 장비의 SquadOne_AI가 점유 중 → 대시보드는 5180 사용.)

---

## 3. 프론트 구조

```
frontend/
  src/
    api/client.ts     # fetch 래퍼(VITE_API_BASE, VITE_API_KEY)
    api/types.ts      # 응답 타입(views.py 와 1:1)
    week.tsx          # 전역 주차 컨텍스트
    hooks.ts          # useAsync
    components/ui.tsx # StatusBadge / Loading / Empty / ErrorBox / 팔레트
    pages/Home.tsx           # P1
    pages/TrendExplorer.tsx  # P2
    pages/Detail.tsx         # P3
    pages/WeekCompare.tsx    # P4
    App.tsx           # 헤더 + 라우팅 + 주차 셀렉터
  .env                # VITE_API_BASE / VITE_API_KEY
```

Recharts 매핑: 생명주기=카드 그리드, 군집=ScatterChart(x=cohesion, y=group_score, z=멤버수),
z-score=LineChart, 언론사=PieChart(도넛).

---

## 4. 기동 방법

### 백엔드 (REST)
```bash
# 의존성(최초 1회): psycopg[binary] / psycopg_pool / fastapi / uvicorn
# 8000/8001은 SquadOne_AI 점유 → 8010 사용.
uvicorn rest_mcp_server.app:app --reload --port 8010
# 점검
curl localhost:8010/v1/view/weeks
curl 'localhost:8010/v1/view/summary'
```

### 프론트 (대시보드)
```bash
cd frontend
npm install        # 최초 1회
npm run dev        # http://localhost:5180
```
`frontend/.env`의 `VITE_API_BASE`가 REST 서버를 가리키도록 한다(기본 `http://localhost:8010`).
서버에 `REST_MCP_API_KEY`를 설정했다면 `VITE_API_KEY`에도 동일 값을 넣는다.

---

## 5. 데이터 전제 & 온디맨드 생성

대시보드는 **적재된 결과를 조회**하되, 비어 있는 주차는 화면에서 직접 생성할 수 있다.

- ✅ `weekly_keywords`, `weekly_keyword_freq`, `z_score_keywords` — 1~4단계 산출물(537주차 적재).
- `trend_timeseries`, `trend_groups`, `trend_contexts`, `keysentence`, `product_candidates`, `reports`
  — 6~7단계 산출물. 주차별로 **생성 버튼**(아래)으로 채운다.

### 생성 버튼 (Home, 상품 0건일 때 노출)
- `POST /v1/view/generate {week}` → 백그라운드 스레드로 `run_trend_extractor`(6) → `run_product_extractor`(7)를
  **단일 주차**(`base_start_week=base_end_week=week`)로 실행. `GET /v1/view/generate-status?week=`로 폴링.
- 외부 의존성: **Qdrant**(벡터검색)와 **Ollama**(LLM). 둘 다 `.env`의 원격 주소(`QDRANT_HOST`, `OLLAMA_BASE_URL`)를 사용.
  - Qdrant URL은 `db.config.qdrant_url()`(=`.env`)이 단일 출처. config의 `qdrant_url`은 env 미설정 시 폴백.
- 성능: 단일 주차 DB 읽기는 `read_zscore/read_weekly_freq`의 `start_week/end_week` 스코핑으로 해당 주차만 읽는다
  (9.7M행 → ~1.8만행, ~2초). cold-start 기준 1개 주차 생성 **~85초**(모델 워밍 후).

> 참고: 단일 주차 생성은 직전 이력(seen_history)이 비어 모든 트렌드가 `Emerging`으로 분류된다(설계상 특성).
> 생명주기(Active/Fading) 구분이 필요하면 연속 주차를 범위로 생성해야 한다.

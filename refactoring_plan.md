# SquadOne 뉴스트렌드 파이프라인 고도화 계획 (refactoring_plan)

> 최종 갱신: 2026-06-01
> 골자: **① Qdrant 벡터DB 활용 · ② 불필요 단계 삭제 · ③ PostgreSQL DB화 · ④ MCP Server 제공**
> 기준 코드: 분기 A (`SquadOne_Tool`) — 백업 `SquadOne_NewsTrend_Backup`은 참고용

---

## 0. 확정된 의사결정

| # | 항목 | 결정 |
|---|------|------|
| 1 | 5·6 벡터검색 | **통합** (2단계 중복 검색 제거 + evidence 일원화) |
| 2 | LLM 호출 표준 | **`llm_factory`로 일원화** |
| 3 | Qdrant payload 계약 | **`doc_id/source/title/body/date` 스키마 고정** |
| 4 | MCP 도구 경계 | **처음엔 파이프라인 통째 노출**, 이후 단계별 노출은 계획 후 반영 |
| 5 | 4단계 `_group` | **삭제** (소비처 없음 확인) |
| 6 | 2단계 frequency_matrix | **SQL 증분**, dense 피벗 파일 폐기 |
| 7 | DB | 기존 도커 **PostgreSQL 16** (`squadone-postgres`) 활용 |

---

## 1. 현행 → 목표 아키텍처

### 현행
- 입력: 뉴스 **엑셀**(`data/news/*.xlsx`)
- 단계 간 전달: **CSV/JSON 파일 경로**
- 산출물: `data/output/` 84개 파일 (~2.5GB), 대용량 CSV 다수
- LLM: `llm_factory`(일부) — 일관성 점검 필요
- 노출: 로컬 FastMCP + REST(FastAPI) 부분 구현

### 목표
```
[0 수집·적재]  뉴스 → 임베딩 → Qdrant (별도 구현)
      │
[1 keyword]    Qdrant 본문 → Kiwi 명사 → 주차 빈도 ──┐
[2 frequency]  SQL 증분 집계 (파일 없음)            │  PostgreSQL
[3 base]       TF-IDF + rolling (Python) → DB long  │  (squadone)
[4 z-score]    EWM z-score → DB long (_group 삭제)  ──┘
      │
[5 keysentence] Qdrant 벡터검색 + LLM 요약 ──┐ 통합 검색
[6 trend]       생애주기 + Qdrant 근거 + 그룹 ┘ evidence_doc_ids 일원화
[7 product]     2단계 LLM → 상품 후보 → DB(jsonb)
      │
[MCP Server]   파이프라인 통째 노출 (run_news_trend)
```

핵심 전환: **파일 경로 계약 → DB 테이블/record 계약**, **엑셀 → Qdrant**, **CSV 누적 → 증분 upsert**.

---

## 2. 인프라 현황 (기 가동 중)

| 서비스 | 컨테이너 | 이미지 | 포트 | 비고 |
|--------|----------|--------|------|------|
| PostgreSQL | `squadone-postgres` | postgres:16-alpine | 5432 | DB `squadone`, user `squadone`, 비밀번호 env |
| Qdrant | `qdrant-main` | qdrant/qdrant | 6333/6334 | 컬렉션 `news_10y_ko_v1` |

- compose 정의·관리: **`SquadOne_AI/infra/docker-compose.yml`** (공유 인프라)
- 마이그레이션 마운트: `infra/migrations → /docker-entrypoint-initdb.d`
- **본 프로젝트의 테이블은 공유 DB `squadone` 내 전용 스키마 `newstrend`에 격리**한다.

> ⚠️ 확인 필요: 접속 자격증명(`POSTGRES_PASSWORD`) 전달 방식 → 본 프로젝트는 `DATABASE_URL` 환경변수로 주입 (기존 `GEMINI_API_KEY` 등과 동일 패턴).

---

## 3. PostgreSQL 스키마 설계 (`newstrend` 스키마)

### 3.1 원천·파생 테이블

```sql
CREATE SCHEMA IF NOT EXISTS newstrend;

-- [1] 원천 fact (유일하게 반드시 저장)
CREATE TABLE newstrend.weekly_keywords (
    week        text    NOT NULL,          -- 'YYYY-Www'
    keyword     text    NOT NULL,
    source      text    NOT NULL,          -- 언론사
    count       integer NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword, source)
);
CREATE INDEX ON newstrend.weekly_keywords (week);
CREATE INDEX ON newstrend.weekly_keywords (keyword);

-- [2] frequency_matrix: 저장하지 않음.
--     필요 시 집계 뷰(주차×키워드 합계, 희소). dense 피벗은 3단계에서 메모리로만.
CREATE MATERIALIZED VIEW newstrend.mv_weekly_keyword_freq AS
SELECT week, keyword, sum(count)::int AS count
FROM newstrend.weekly_keywords
GROUP BY week, keyword;
CREATE UNIQUE INDEX ON newstrend.mv_weekly_keyword_freq (week, keyword);

-- [3] base_calculation (long)
CREATE TABLE newstrend.base_calculation (
    week       text   NOT NULL,
    keyword    text   NOT NULL,
    tfidf      double precision,
    base_mean  double precision,
    base_std   double precision,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword)
);

-- [4] z_score (long). 고z는 뷰로.
CREATE TABLE newstrend.z_score_keywords (
    week       text   NOT NULL,
    keyword    text   NOT NULL,
    z_score    double precision NOT NULL,
    sources    text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword)
);
CREATE VIEW newstrend.v_z_score_high AS
SELECT * FROM newstrend.z_score_keywords WHERE z_score >= 2.0;
```

### 3.2 벡터·LLM 산출 테이블

```sql
-- [5] keysentence (벡터검색 + LLM 요약 결과)
CREATE TABLE newstrend.keysentence (
    week             text NOT NULL,
    keyword          text NOT NULL,
    query_text       text,              -- 6단계 검색 시드
    key_sentence     text,
    evidence_doc_ids text[],            -- Qdrant doc_id (이제 채워짐)
    evidence_count   integer,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword)
);

-- [6] trend 메인 + 보조
CREATE TABLE newstrend.trend_timeseries (
    week            text NOT NULL,
    keyword         text NOT NULL,
    trend_slot_id   text,
    group_id        text,
    group_score     double precision,
    status          text,              -- Emerging/Active/Fading/Archived
    status_reason   text,
    z_score         double precision,
    count           integer,
    weekly_summary  text,
    evidence_doc_ids text[],
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword)
);
CREATE TABLE newstrend.trend_contexts (
    week    text NOT NULL,
    keyword text NOT NULL,
    doc_id  text NOT NULL,
    score   double precision,
    snippet text,
    PRIMARY KEY (week, keyword, doc_id)
);
CREATE TABLE newstrend.trend_groups (
    week        text NOT NULL,
    group_id    text NOT NULL,
    members     jsonb,
    group_score double precision,
    cohesion    double precision,
    PRIMARY KEY (week, group_id)
);
-- dashboard_timeseries → 뷰로 대체
CREATE VIEW newstrend.v_trend_dashboard AS
SELECT week, keyword, trend_slot_id, group_id, group_score,
       status, z_score AS trend_strength, weekly_summary, evidence_doc_ids
FROM newstrend.trend_timeseries;

-- [7] product
CREATE TABLE newstrend.product_candidates (
    week             text NOT NULL,
    rank             integer NOT NULL,
    product_name     text,
    selection_reason text,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, rank)
);
-- LLM 자유서술 리포트는 범용 jsonb 테이블
CREATE TABLE newstrend.reports (
    step       text NOT NULL,   -- 'product_context' 등
    week       text NOT NULL,
    payload    jsonb NOT NULL,
    meta       jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (step, week)
);

-- 파이프라인 실행 추적 (MCP)
CREATE TABLE newstrend.pipeline_runs (
    run_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    params     jsonb,
    status     text,            -- running/success/failed
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
```

### 3.3 증분(upsert) 패턴
```sql
INSERT INTO newstrend.weekly_keywords (week, keyword, source, count)
VALUES %s
ON CONFLICT (week, keyword, source)
DO UPDATE SET count = EXCLUDED.count, updated_at = now();
-- 적재 후: REFRESH MATERIALIZED VIEW CONCURRENTLY newstrend.mv_weekly_keyword_freq;
```
→ 비용은 **신규 주차 행수**에 비례 (전량 재계산 아님).

---

## 4. 단계별 로직 수정 상세

### 0단계 · 뉴스 수집 → Qdrant (별도 구현, 계약만 본 계획에서 고정)
**Qdrant payload 계약 (고정):**
```json
{
  "doc_id":  "string(고유)",
  "source":  "언론사",
  "title":   "제목",
  "body":    "본문",
  "date":    "YYYY-MM-DD"
}
```
- 벡터: named vector `body`(필수), `title`(선택) — 기존 `paraphrase-multilingual-mpnet-base-v2`, `normalize_embeddings=True`
- 1·5·6단계가 이 계약에 의존 → **변경 시 공동 영향**.

### 1단계 · keyword_extractor 🟥
- **변경:** 입력을 엑셀 → **Qdrant**. 주차 범위의 포인트를 scroll로 가져와 `body` 추출.
- 유지: Kiwi(`sbg`) 명사 추출 → 불용어 → ISO 주차 → `(week, keyword, source)` 빈도.
- **출력:** `weekly_keywords` 테이블 **upsert**.
- 수정 파일: `steps/keyword_extractor.py` (입력부), `steps/common.py`(엑셀 헬퍼 제거), config(`paths.news_dir` 제거, `vector_db` 추가).

### 2단계 · frequency_matrix 🟥
- **변경:** `pd.pivot` + CSV 저장 → **SQL 집계/뷰**. `run_frequency_matrix`는 `REFRESH MATERIALIZED VIEW` 호출로 축소(또는 제거하고 3단계가 직접 집계 사용).
- **출력:** `mv_weekly_keyword_freq` (저장 파일 없음).
- 수정 파일: `steps/frequency_matrix.py` (대폭 축소 또는 흡수).

### 3단계 · base_calculation 🟢(로직)/🟥(IO)
- 유지: TF-IDF + 장기 rolling mean/std. **행렬은 메모리에서 임시 피벗**(scipy sparse 권장).
- **변경:** wide CSV → **`base_calculation` long 테이블** upsert.
- 수정 파일: `steps/base_calculation.py` (입력=뷰, 출력=DB).

### 4단계 · z_score_filtering 🗑+🟥
- 유지: rolling + EWM → z-score, 고z(≥2.0).
- **삭제:** `_build_keyword_group_map`, `_keyword_similarity`, `_normalize_keyword_for_grouping`, config `group_similarity_threshold`, `z_score_keywords_group*` 출력 및 요약 키.
- **변경:** CSV → `z_score_keywords` 테이블, 고z는 `v_z_score_high` 뷰.
- 수정 파일: `steps/z_score_filtering.py`, `config/pipeline_config.json`, `tool_news_trend.py`, `rest_mcp_server/app.py`.

### 5+6단계 · keysentence + trend 통합 🟥 (검색 일원화)
- **통합 결정 반영:** 벡터검색을 **한 번만** 수행.
  - 흐름: 고z 키워드 → (선택)LLM 요약으로 query 보강 → **Qdrant 단일 검색** → 근거 문서(doc_id 포함) → 생애주기 + 그룹핑.
  - 5단계의 LLM 요약은 "검색 시드"가 아니라 **검색 결과 요약/맥락화** 역할로 재배치하거나, 검색은 키워드+source로 1회 수행 후 결과를 5·6이 공유.
- **이득:** `evidence_doc_ids` 자동 채움, 중복 Qdrant 호출 제거, 엑셀 의존 완전 제거.
- **재사용:** `embed_query_for_news`, `_query_qdrant_contexts`(6) + `_collect_week_points`(5, 죽은코드) 통합.
- **출력:** `keysentence`, `trend_timeseries`, `trend_contexts`, `trend_groups` 테이블, dashboard는 뷰.
- 수정 파일: `steps/keysentence_extractor.py`(엑셀→벡터, 또는 6에 흡수), `steps/trend_extractor.py`(검색 공유), `steps/qdrant_embed.py`(공유 검색 헬퍼 추출 권장).

### 7단계 · product_extractor 🟢(로직)/🟥(IO)
- 유지: 2단계 LLM(맥락분석 → 상품 후보, 제약 IP/물류/일반명사).
- **변경:** CSV → `product_candidates` 테이블 + `reports`(jsonb, context).
- 수정 파일: `steps/product_extractor.py`.

### 공통 · LLM 일원화 🟥
- 모든 LLM 호출을 **`steps/llm_factory.get_llm(role)`** 경유로 통일 (genai 직접호출 금지).
- 역할(role)·모델·provider는 환경변수/config로 (`LLM_PROVIDER`, `GEMINI_API_KEY`, `OLLAMA_*`).
- 점검: 분기 A는 대부분 준수 — 잔여 직접호출/하드코딩 폴백 정리.

---

## 5. 데이터 액세스 계층 (신규)

```
db/
  __init__.py
  engine.py        # psycopg3 connection pool, DATABASE_URL
  repository.py    # upsert_weekly(), read_freq(), write_base(), write_zscore(),
                   # write_keysentence(), write_trend(), write_product(), ...
  migrations/      # 0001_init.sql, 0002_..., (또는 infra/migrations 공유)
  migrate.py       # 마이그레이션 러너 (psql 적용 or yoyo)
```
- **드라이버:** `psycopg[binary]>=3` (대량 적재 `COPY`/`execute_values`).
- `steps/common.py`의 `read_csv/write_csv/write_dataframe_json_export`는 **repository 호출로 대체**(점진적: 어댑터 두고 단계별 전환).
- 접속: `DATABASE_URL=postgresql://squadone:***@localhost:5432/squadone`.
- requirements.txt 추가: `psycopg[binary]` (필요 시 `sqlalchemy`).

---

## 6. MCP Server 제공

- **1차: 파이프라인 통째 노출** — 기존 `tool_news_trend.py`의 `run_news_trend` 도구 유지하되 **반환을 파일경로 → `run_id` + DB 조회 메타**로 변경.
- 실행 추적: `pipeline_runs`에 run 기록, 도구는 `run_id`/status/요약 반환.
- 조회 도구(읽기): `get_trend_report(week)`, `get_product_candidates(week)` 등은 **DB 뷰 조회**로 가볍게 (2차에서 단계별 노출과 함께 확장).
- REST(`rest_mcp_server/app.py`)도 동일 계약으로 정렬, `X-API-Key` 유지.
- **2차(추후): 단계별 노출** — 각 step을 개별 MCP 도구로. 계획 별도 수립.

---

## 7. 마이그레이션 / 백필 전략

1. **스키마 생성**: `0001_init.sql` 적용 (`infra/migrations` 또는 본 repo `db/migrations`).
2. **기존 산출물 적재(1회)**: 현재 `data/output/*.csv` → 해당 테이블 백필 스크립트(`scripts/backfill_csv_to_pg.py`). 검증용으로만, 이후 파이프라인이 권위.
3. **병행 운영(safety)**: 전환 초기엔 DB 적재 + 기존 파일 출력 **동시 유지** → 결과 일치 확인 후 파일 출력 제거.
4. **컷오버**: 파일 경로 의존 제거, `data/output` 정리, `.gitignore` 유지.

---

## 8. 실행 단계 (Phase)

| Phase | 내용 | 산출물 | 의존 |
|-------|------|--------|------|
| **P0 인프라·계약** | DB 스키마/마이그레이션, `db/` 액세스 계층, `DATABASE_URL` 설정, Qdrant payload 계약 문서, psycopg 추가 | `0001_init.sql`, `db/*`, 계약 문서 | 도커 가동(완료) |
| **P1 정형 DB화(1~4)** | 1=Qdrant입력, 2=SQL증분, 3·4=DB long, **_group 삭제** | 테이블 적재 동작 | P0 |
| **P2 벡터 통합(5+6)** | 검색 일원화, evidence_doc_ids, 엑셀 제거 | trend 테이블 | P1, 0단계 컬렉션 |
| **P3 상품(7)** | product DB(jsonb) 전환 | product 테이블 | P2 |
| **P4 MCP 통합** | run 추적 + 통짜 도구 + 조회 도구, REST 정렬 | MCP 계약 | P1~P3 |
| **P5 정리·문서** | 파일출력 제거, 죽은코드 삭제, 문서/매뉴얼 | docs/ 일체 | 전체 |

권장 순서: **P0 → P1 → P2 → P3 → P4 → P5** (P1 내에서 병행 출력으로 안전 검증).

---

## 9. 문서 / 매뉴얼 작업 (산출물)

| 문서 | 내용 |
|------|------|
| `README.md` | 프로젝트 개요, 설치, 실행, env(`DATABASE_URL`, `GEMINI_API_KEY` 등) |
| `docs/ARCHITECTURE.md` | 단계별 데이터 흐름, 현행→목표 다이어그램 |
| `docs/DB_SCHEMA.md` | 테이블·뷰·관계·증분 패턴(본 문서 3장 확장) |
| `docs/QDRANT_CONTRACT.md` | payload/벡터 계약(0·1·5·6 공통) |
| `docs/MCP_USAGE.md` | MCP 도구 목록·입출력·예시(외부 프로젝트용) |
| `docs/RUNBOOK.md` | 운영: 마이그레이션 적용, 백필, 주차 재적재, 장애 대응 |
| `docs/MIGRATION_GUIDE.md` | CSV→PG 전환 절차, 롤백 |
| `Legacy_README.md` | (기존) Qdrant 검색 상세 — `QDRANT_CONTRACT`로 흡수/링크 |

---

## 10. 리스크 / 점검 항목

- **Qdrant 0단계 의존:** 1·5·6이 컬렉션 적재 완료를 전제. 0단계 미완 시 폴백(엑셀) 임시 유지 옵션.
- **TF-IDF 메모리:** 31.7만 키워드 × 537주 임시 피벗 → scipy sparse / 청크 처리 필요.
- **EWM 수치 동등성:** Python 유지(SQL 미전환)로 회귀 위험 낮음. 전환 전후 결과 diff 검증.
- **공유 DB 격리:** `newstrend` 스키마로 분리, 권한 최소화.
- **자격증명:** `DATABASE_URL` 등 env 주입, 코드 하드코딩 금지(현 정책 유지).
- **병행 검증:** P1~P3 동안 파일 vs DB 결과 동등성 비교 후 파일 제거.

---

## 11. 즉시 착수 가능한 P0 체크리스트

- [ ] `DATABASE_URL` env 규약 확정 + `squadone-postgres` 접속 확인
- [ ] `newstrend` 스키마 + `0001_init.sql` 작성·적용
- [ ] `db/` 액세스 계층(engine/repository/migrate) 스캐폴딩
- [ ] `requirements.txt`에 `psycopg[binary]` 추가
- [ ] `docs/QDRANT_CONTRACT.md` 초안(payload 고정)
- [ ] 기존 `data/output` → PG 백필 스크립트 초안

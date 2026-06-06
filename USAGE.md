# SquadOne_Tool 사용 가이드

뉴스 트렌드 파이프라인의 **실행·점검·조회** 명령어 모음입니다.
모든 명령은 레포 루트(`SquadOne_Tool/`)에서 실행하며, 가상환경(`.venv`)이 활성화된 상태를 가정합니다.

```bash
cd /Users/changhokang/Desktop/SquadOne_Tool
source .venv/bin/activate          # 의존성: pip install -r requirements.txt
```

---

## 0. 사전 준비 (최초 1회)

### 0-1. 환경변수 (`.env`)

`.env.example`을 복사해 채웁니다. 시크릿은 커밋되지 않습니다(`.gitignore` 처리됨).

```bash
cp .env.example .env
```

| 그룹 | 키 | 비고 |
| --- | --- | --- |
| PostgreSQL | `POSTGRES_HOST/PORT/DB/USER/PASSWORD` | DB 적재·조회의 접속 정보 |
| Qdrant | `QDRANT_HOST/PORT` | 벡터 검색(기본 `localhost:6333`) |
| LLM | `LLM_PROVIDER` (`ollama`/`gemini`), `GEMINI_API_KEY`, `OLLAMA_*` | 5~7단계에서 사용 |
| 임베딩 | `SQUADONE_EMBED_MODEL`, `SQUADONE_EMBED_DEVICE` | `auto` → cuda/mps/cpu 자동 |
| REST 서버 | `REST_MCP_API_KEY` | REST 호출 인증 키 |
| 로깅 | `MCP_LOG_LEVEL`, `REST_LOG_LEVEL` | |

### 0-2. DB 마이그레이션 적용

`db/migrations/*.sql`을 번호순으로 멱등 적용합니다(반복 실행 안전).

```bash
python -m db.migrate            # 전체 마이그레이션 적용
python -m db.migrate --check    # 접속 확인 + newstrend 객체 목록만 출력
```

### 0-3. (선택) 기존 CSV 부트스트랩 적재

기존 `data/output/*.csv`를 `newstrend` 테이블로 1회 이관합니다. 적재 전 해당 테이블을 TRUNCATE 합니다.

```bash
python -m scripts.backfill_csv_to_pg                       # weekly + zscore (기본)
python -m scripts.backfill_csv_to_pg --tables weekly,zscore,base
python -m scripts.backfill_csv_to_pg --tables base         # base만(대용량)
```

### 0-4. (선택) Kiwi SBG 모델 애드온

`model_type='sbg'`용 바이너리(`skipbigram.mdl`, `sj.knlm`)를 설치합니다.

```bash
python scripts/install_kiwi_sbg_addon.py            # 설치
python scripts/install_kiwi_sbg_addon.py --dry-run  # 변경 없이 미리보기
```

---

## 1. 파이프라인 단계별 실행 — `scripts/run_step.py`

REST 서버 없이 단계를 직접 실행합니다. **5+6단계는 `56`** 으로 통합 실행합니다.

| step | 단계 | 핵심 입력 |
| :--: | --- | --- |
| `1` | keyword_extractor (Qdrant→DB) | `--week` 또는 `--start-date`/`--end-date` |
| `2` | frequency_matrix (증분 집계) | — |
| `3` | base_calculation (전체 재계산) | — |
| `4` | z_score_filtering | — |
| `56` | trend_extractor (5+6 통합) | `--week` 또는 `--base-start-week`/`--base-end-week` |
| `7` | product_extractor | `--week`, `--news-keyword`(선택) |

```bash
python -m scripts.run_step 1  --week 2023-W13
python -m scripts.run_step 2
python -m scripts.run_step 3
python -m scripts.run_step 4
python -m scripts.run_step 56 --week 2023-W13
python -m scripts.run_step 7  --week 2023-W13 --news-keyword "캠핑"
```

**공통 옵션**

| 옵션 | 설명 |
| --- | --- |
| `--week YYYY-Www` | 편의 옵션. 단일 주차 지정 시 날짜범위(1단계)와 coverage(5~7단계)를 자동 설정 |
| `--start-date` / `--end-date` | `YYYY-MM-DD`. 1단계 입력을 뉴스 일자로 제한 |
| `--base-start-week` / `--base-end-week` | 5~7단계 처리 주차 구간(`YYYY-Www`) |
| `--news-keyword` | 7단계 상품 추출 스코프 키워드 |
| `--test-mode` / `--test-max-weeks N` | 처리 주차 수 제한(테스트) |
| `--log-level DEBUG\|INFO\|WARNING\|ERROR` | 로그 레벨(기본 INFO) |

> `--week`는 `--start/end-date`와 `--base-start/end-week`가 비어 있을 때만 자동으로 채웁니다.

---

## 2. 전체 파이프라인 실행 — `main.py`

MCP 클라이언트 또는 직접 호출로 파이프라인 전체를 한 번에 돌립니다.

```bash
# 직접 호출(권장: 가장 단순)
python main.py --mode direct --week 2023-W13

# 주차 구간/스코프 명시
python main.py --mode direct \
  --base-start-week 2023-W10 --base-end-week 2023-W13 \
  --news-keyword "캠핑" --log-file data/output/pipeline.log

# MCP 서버(stdio) 호출 — 실패 시 direct로 자동 폴백
python main.py --mode mcp --target-week 2026-W15
```

**옵션**

| 옵션 | 설명 |
| --- | --- |
| `--mode mcp\|direct` | `mcp`: MCP 서버 호출, `direct`: 파이프라인 함수 직접 호출(기본 `mcp`) |
| `--week YYYY-Www` | 단일 주차 → start/end-date + base-start/end-week 자동 설정 |
| `--target-week` | 예: `2026-W15` |
| `--start-date` / `--end-date` | 1단계 입력 일자 필터 |
| `--base-start-week` / `--base-end-week` | 5~7단계 coverage 주차 |
| `--news-keyword` | 7단계 상품 추출 스코프 |
| `--test-mode` / `--test-max-weeks N` | 주차 수 제한 |
| `--log-level` / `--log-file` / `--log-stream stdout\|stderr` | 로깅(기본 stream: mcp→stderr, direct→stdout) |

결과는 JSON으로 표준출력에 출력됩니다.

---

## 3. 결과 점검 — `scripts/db_check.py` (읽기 전용)

DB 객체 존재, 테이블 행수, 주차 커버리지, (옵션) 정합성을 점검합니다. **부작용 없음.**

```bash
python -m scripts.db_check          # 객체/행수/주차 커버리지 요약
python -m scripts.db_check --full   # + 정합성 검사
```

`--full`이 검사하는 항목: freq↔weekly 합계 일치, z_score 주차 ⊆ weekly 주차,
trend·keysentence의 `evidence_doc_ids` 채움 비율. 종합 결과가 OK면 종료코드 `0`, 불일치면 `2`.

---

## 4. 주차 결과 조회 — `scripts/show_week.py` (읽기 전용)

특정 주차의 트렌드 시계열·그룹·키센텐스·상품 후보 상위 N개를 출력합니다.

```bash
python -m scripts.show_week 2023-W13
python -m scripts.show_week 2023-W13 --top 20
```

| 인자/옵션 | 설명 |
| --- | --- |
| `week` (필수) | ISO 주차 라벨, 예: `2023-W13` |
| `--top N` | 상위 N개(기본 10) |

---

## 5. REST MCP 서버 — `rest_mcp_server/app.py`

단계를 HTTP API로 노출합니다(FastAPI, 기본 `127.0.0.1:8765`).

```bash
# 의존성: pip install -r rest_mcp_server/requirements.txt
python -m rest_mcp_server.app
# 또는
uvicorn rest_mcp_server.app:app --host 127.0.0.1 --port 8765
```

**인증** — `.env`의 `REST_MCP_API_KEY`를 `X-API-Key` 헤더로 전달합니다.

| 엔드포인트 | 설명 |
| --- | --- |
| `GET /health` | 헬스 체크(인증 불필요) |
| `GET /v1/tools/manifest` | 툴 매니페스트 |
| `POST /v1/tools/news_trend.keyword_extractor` | 1단계 |
| `POST /v1/tools/news_trend.frequency_matrix` | 2단계 |
| `POST /v1/tools/news_trend.base_calculation` | 3단계 |
| `POST /v1/tools/news_trend.z_score_filtering` | 4단계 |
| `POST /v1/tools/news_trend.trend_extractor` | 5+6단계 |
| `POST /v1/tools/news_trend.product_extractor` | 7단계 |

```bash
# 예: 1단계 호출
curl -s -X POST http://127.0.0.1:8765/v1/tools/news_trend.keyword_extractor \
  -H "X-API-Key: $REST_MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"start_date":"2023-03-27","end_date":"2023-04-02"}'
```

---

## 빠른 시작 (요약)

```bash
source .venv/bin/activate
cp .env.example .env && $EDITOR .env      # 1) 환경 채우기
python -m db.migrate                      # 2) DB 준비
python -m scripts.run_step 1  --week 2023-W13   # 3) 단계 실행
python -m scripts.run_step 2
python -m scripts.run_step 3
python -m scripts.run_step 4
python -m scripts.run_step 56 --week 2023-W13
python -m scripts.run_step 7  --week 2023-W13
python -m scripts.db_check --full         # 4) 점검
python -m scripts.show_week 2023-W13      # 5) 조회
```

> 파이프라인 설계/동작 상세는 [refactoring_plan.md](refactoring_plan.md),
> Qdrant·트렌드 그룹 설정은 [Legacy_README.md](Legacy_README.md),
> Qdrant 계약은 [docs/QDRANT_CONTRACT.md](docs/QDRANT_CONTRACT.md)를 참고하세요.

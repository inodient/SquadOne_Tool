# Qdrant Payload 계약 (뉴스 컬렉션)

> 단일 출처(single source of truth). **0단계 수집·적재, 1·5·6단계가 공통 의존**한다.
> 변경 시 의존 단계 전체에 영향 — 반드시 본 문서와 동기화할 것.

## 컬렉션
- 이름: `news_10y_ko_v1`
- 규모: 약 940만 포인트(10년치 한국어 뉴스)
- Named vectors: `body`(필수), `title`(선택)
- 임베딩: `sentence-transformers/paraphrase-multilingual-mpnet-base-v2`, `normalize_embeddings=True`
  (인덱싱 파이프라인과 동일 모델·정규화 가정)

## Payload 필드 (실측 기준)

| 실제 필드 | 타입 | 설명 | 파이프라인 추상명 |
|-----------|------|------|------------------|
| `news_id` | string | 기사 고유 ID | **doc_id** (evidence_doc_ids 의 원소) |
| `press` | string | 언론사 | **source** (1단계 source 집계 키) |
| `title` | string | 제목 | title |
| `body` | string | 본문 | body (1단계 Kiwi 분석 대상) |
| `date` | string `YYYY-MM-DD` | 발행일 | date (주차 라벨링 기준) |
| `url` | string | 원문 URL | (보조) |
| `source_file` | string | 원천 엑셀 파일명 | (보조) |
| `file_date_start` | string | 파일 기간 시작 | 서버측 주간 겹침 필터용 |
| `file_date_end` | string | 파일 기간 끝 | 서버측 주간 겹침 필터용 |

### ⚠️ 추상명 ↔ 실제 필드 매핑 (중요)
코드/설계 문서에서 쓰는 추상명과 Qdrant 실제 필드명이 다르다. **매핑을 통해 접근**한다.

- `doc_id`  → **`news_id`**
- `source`(언론사) → **`press`**
- `title` / `body` / `date` → 동일

> `refactoring_plan.md` 및 DB 스키마(`evidence_doc_ids`, `weekly_keywords.source`)는
> 위 매핑 기준이다: `evidence_doc_ids` 에는 `news_id` 값이, `weekly_keywords.source` 에는 `press` 값이 들어간다.

## 단계별 사용
- **0단계(수집):** 위 payload + `body`/`title` 벡터로 적재. 본 계약을 만족시켜야 함.
- **1단계 keyword_extractor:** 주차 범위 포인트 scroll → `body`(Kiwi 명사) + `press`(source) + `date`(주차) 사용.
- **5·6단계(통합 검색):** `body` 벡터로 검색 → 결과의 `news_id`를 `evidence_doc_ids`/`trend_contexts.doc_id`로 기록.

## 검색 경로 (기존 구현 참고)
- 벡터 검색: `POST /collections/news_10y_ko_v1/points/search` (named vector `body`/`title`)
- scroll 폴백: `POST /collections/news_10y_ko_v1/points/scroll` (임베딩 실패/0건 시)
- 서버측 날짜 필터 / 주간 겹침 옵션 등 상세는 `Legacy_README.md` 참조(추후 본 문서로 흡수 예정).

## 접속
- URL: `db.config.qdrant_url()` (`QDRANT_HOST`/`QDRANT_PORT`, 기본 `http://localhost:6333`)
- 자격증명/호스트는 공유 `SquadOne_AI/.env` 사용.

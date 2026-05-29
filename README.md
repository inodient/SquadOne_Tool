# SquadOne_Tool

## Qdrant 벡터 검색 (`vector_db`)

`trend_extractor`는 뉴스 Qdrant 컬렉션에서 근거 문맥을 가져올 때 다음 순서를 사용합니다.

1. **벡터 검색** — `sentence-transformers/paraphrase-multilingual-mpnet-base-v2`로 키워드를 임베딩한 뒤, Qdrant REST **`POST /collections/{collection}/points/search`** 에 named vector(`title` / `body`)를 지정해 검색합니다. 인덱싱 파이프라인은 `squadone_news_vectordb/scripts/ingest_news_to_qdrant.py` 와 동일한 모델·정규화(`normalize_embeddings=True`)를 가정합니다.
2. **scroll 폴백** — 임베딩 실패·0건·예외 시 **`/points/scroll`** 으로 전역 페이지를 가져온 뒤, 로컬에서 주차·키워드로 걸러냅니다.

### 서버 측 날짜 필터 vs 로컬 필터

- **`use_server_date_filter` (기본 true)**  
  검색·scroll에 Qdrant `filter`를 넣어 **주차로 먼저 좁힌 뒤** 벡터 유사도를 계산합니다. 순서는 **`date` 7일 `match.any`** 필터 → (옵션) **`date` 또는 `file_date_start`/`file_date_end` 주간 겹침**(`should` + nested `must`) → 실패·0건이면 **무필터**입니다.
- **`server_week_filter_file_overlap` (기본 true)**  
  `false`이면 서버에는 `date` 분기만 사용합니다(Qdrant가 `file_date` `range`를 거부하는 환경용).
- **scroll만 쓰는 경로**  
  페이지 수에 상한(`scroll_max_pages`)이 있어, 필터 없이 스캔할 때는 **그 주 전체 벡터를 보장하지 않습니다.**

### 해당 주 전체에 가깝게 스캔하기 (선택)

`config/pipeline_config.json`:

- **`week_scroll_mode`**: `"default"` — `scroll_max_pages` 만큼만 scroll 페이지를 돕니다.
- **`week_scroll_mode`**: `"extended"` 이고 **`scroll_full_week_max_pages`** 가 0보다 크면, scroll 폴백 시 `max(scroll_max_pages, scroll_full_week_max_pages)` 페이지 상한을 사용합니다. (느려질 수 있음)

### 주요 설정 키

| 키 | 설명 |
| --- | --- |
| `enable_query_embedding` | 벡터 검색 사용 여부 |
| `embed_model` / `embed_device` | SentenceTransformer 모델·디바이스 (`auto` 시 cuda/mps/cpu) |
| `query_vector_name` | `body`, `title`, `both`(두 벡터 검색 후 점수 병합) |
| `use_server_date_filter` | Qdrant payload 필터 사용 |
| `vector_search_limit_multiplier` / `vector_search_min_limit` | 벡터 검색 `limit` |
| `scroll_limit_multiplier` / `scroll_min_limit` / `scroll_max_pages` | scroll 폴백 |

환경변수 `SQUADONE_EMBED_MODEL`, `SQUADONE_EMBED_DEVICE`가 설정되면 `embed_model` / `embed_device` 보다 우선합니다.

## 주차 다중 트렌드 그룹 (`trend_extractor`)

`trend_extractor`는 키워드별 context를 만든 뒤, 주차 내 키워드 대표 텍스트 임베딩 코사인 유사도로 그룹을 만듭니다.

- `semantic_group_threshold`: 그룹 연결 임계치
- `max_groups_per_week`: 주차당 최대 그룹 수
- `group_link_threshold`: 이전 주 그룹과 슬롯 계승 임계치
- `group_score_weights`: 그룹 점수 결합 가중치(`z_sum`, `count_log`, `ctx_mean`, `cohesion`)

신규 산출물:

- `data/output/trend_group_report.csv`
- `data/output/trend_group_report.json`

`trend_timeseries_report.csv`에는 그룹 연결용 `group_id`, `group_score`, `group_status`가 추가됩니다.

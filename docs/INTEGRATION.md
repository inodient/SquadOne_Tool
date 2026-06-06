# SquadOne_Tool 통합(Grand Integration) 아키텍처·런북

모든 `SquadOne_Tool_*` 폴더 구현을 현행 `SquadOne_Tool` 에 통합한 결과 문서.
(2026-06, P0~P6. `SquadOne_Tool_Backup_*` 은 복귀용으로 무수정 보존.)

## 1. 전체 파이프라인

```
[Stage0 수집]  collectors/bigkinds_downloader.py  (빅카인즈 Selenium → data/news/*.xlsx)
               collectors/naver_categories.py     (쇼핑 분류체계 → newstrend.naver_categories)
               collectors/agentic_scraper/        (ReAct+Playwright 범용 수집, 선택)
                     │
[Stage0b 적재] steps/qdrant_ingest.py  (xlsx → 임베딩 → Qdrant news_10y_ko_v1, 증분)
                     │
[1~4 정형] keyword_extractor → frequency_matrix → base_calculation → z_score_filtering   (DB)
                     │ newstrend.z_score_keywords
[5 노이즈분류] keyword_classifier  (seasonal/politics/person → keyword_class)  ※6단계 anchor 제외용
                     │
        ┌────────────┴───────────────┐
[6 트렌드(본선)]                  [6B 군집(통합, 6과 병행)]
 trend_extractor(벡터검색 생애주기)  clustering → cluster_interpretation
                                    trend_time_series_builder · long_term_trend_bridge · period_trend
                     │
[7 상품(본선)]   product_extractor
   └ [7 보조·LLM인텔] llm_questionarie  6-1 브리프 → 6-2 관련상품 → 6-3 유튜브질의  (원 NewsTrend 6단계 유래)
   └ [7B] geo_query → youtube_signal(VERC)
   └ [7C] demand_forecast · market_competition · social_vibe   (외부 API)
   └ [7D] naver_grounding (상품↔카테고리)
                     │
[노출]  MCP: tool_news_trend.run_news_trend / run_product_extractor / run_enrichment
        REST: rest_mcp_server /v1/view/*  (enrichment·clusters·related-products 등)
        Frontend: frontend/ (React) — 신규 /v1/view/enrichment 소비(확장 예정)
```

## 2. 실행 (주차 단위)

```bash
# 0) 의존성 (.venv)
python -m pip install -r requirements.txt
python -m playwright install chromium     # agentic_scraper 사용 시
python scripts/install_kiwi_sbg_addon.py  # Kiwi SBG (최초 1회)

# 1) DB 마이그레이션 (원장 기반, 멱등)
python -m db.migrate            # 미적용분만 적용
python -m db.migrate --check    # 객체 목록

# 2) (선택) 수집·적재
python -m collectors.bigkinds_downloader --start-date 2024-01-01 --end-date 2024-01-31
python -m steps.qdrant_ingest                      # data/news → Qdrant
python -m collectors.naver_categories              # 카테고리 분류체계 적재

# 3) 정형 1~4 + 트렌드/상품 (현행)
python main.py --mode direct --week 2024-W05

# 4) 통합 인리치먼트 (Stage5B~7D)
python -m steps.enrichment_pipeline --week 2024-W05               # 전체
python -m steps.enrichment_pipeline --week 2024-W05 --skip-external   # 외부 API 생략

# 개별 단계
python -m steps.clustering --week 2024-W05
python -m steps.llm_questionarie --week 2024-W05 --step all
python -m steps.geo_query --week 2024-W05
python -m steps.youtube_signal --week 2024-W05
```

MCP: `run_enrichment(week, skip_external=False, top_n=30)` 도구로 통째 실행.

## 3. 신규 DB 테이블 (newstrend 스키마, 마이그 0006~0008)

| 테이블 | 단계 | 내용 |
|---|---|---|
| ingest_state | 0b | Qdrant 적재 상태(체크섬) |
| naver_categories / naver_category_trends | 7D | 쇼핑 분류체계/트렌드지수 |
| clusters / cluster_keywords / cluster_interpretation | 5B | 군집·멤버십·LLM 해석 |
| long_term_signals | 5B | 키워드 3년 지속성 스코어 |
| trend_ts_cluster | 5B | 군집 부피/강도/활성 |
| period_trend_signals | 5B | 미시/거시/계절/모멘텀 |
| llm_briefs / related_products / youtube_queries | 6 | 브리프/관련상품/유튜브질의 |
| geo_queries / youtube_signals | 7B | GEO질의 / VERC 수요검증 |
| demand_forecast / market_competition / social_vibe | 7C | 수요/경쟁/소셜 |
| schema_migrations | infra | 마이그레이션 원장 |

## 4. 설정 (config/pipeline_config.json 신규 섹션)
`ingest · clustering · long_term_trend · trend_time_series · period_trend ·
llm_questionarie · geo_query · youtube_signal · demand_forecast · market_competition ·
social_vibe · naver_categories` — 각 단계 파라미터/임계값/LLM role.

## 5. 환경변수 (.env)
- DB/Qdrant/LLM: 기존 POSTGRES_*/QDRANT_*/LLM_PROVIDER/GEMINI_API_KEY/OLLAMA_*.
- 수집: BIGKINDS_ID/PW, BIGKINDS_*(STEP_DAYS/SESSION_DIR/HEADLESS), LOGIN_PROVIDER.
- 인리치먼트: NAVER_CLIENT_ID/SECRET, YOUTUBE_API_KEY. **없으면 해당 단계 graceful skip.**
- Qdrant URL 은 .env(QDRANT_HOST/PORT)가 단일 출처, config.vector_db.qdrant_url 은 폴백.

## 6. 알려진 이슈 / 운영 노트
- **Qdrant 커버리지**: 컬렉션 `news_10y_ko_v1` 에 **9,437,094 포인트, 2016-01~2026-04 전 10년치
  적재 완료**(연도별 고른 분포). 신규 데이터만 `steps.qdrant_ingest` 로 증분 적재.
- **주차 필터 성능(해결됨)**: `date` 필드에 keyword 인덱스가 있어 날짜 필터는 빠르나(0.1s),
  `file_date_start/end` 오버랩은 미인덱스라 주차 대량 스크롤이 ~55s(타임아웃)였다. clustering
  article_title·6-1 브리프의 bulk 수집은 `include_file_overlap=False`(date-only)로 전환 →
  54s→0.2s. (추가 최적화: file_date_* 에 payload range 인덱스 생성도 가능.)
- **네이버 검색 API 401**: 현 NAVER 키가 데이터랩 전용일 수 있음. market_competition(쇼핑 검색)
  사용하려면 네이버 개발자센터 앱에 **검색 API** 사용 설정 필요.
- **.venv pip shebang**: .venv 가 다른 머신(chang-macmini)에서 생성돼 `bin/pip` 래퍼 shebang 이
  깨져 있음. `python -m pip` 사용(바이너리 자체는 정상).
- **마이그레이션**: 0001 의 CREATE OR REPLACE VIEW 가 후속 마이그로 컬럼이 바뀌면 전체 재적용 시
  충돌 → 원장(schema_migrations) 도입으로 해결. 기가동 DB 는 `--seed` 1회 권장.

## 7. 검증 현황 (live DB, 2026-W15)
- P1 qdrant_ingest 무데이터 graceful. P2 clustering(keyword_direct) 3군집/26키워드,
  long_term 198/80추적, period 40, trend_ts 3. P3 6-1/6-2/6-3 파싱·적재(mock). P4 geo 3+유튜브6,
  naver_grounding graceful, market_competition 실 API 호출 경로(401=권한). P6 read_enrichment 14키.
- LLM/외부 API 실값 산출은 키·권한·Qdrant 적재 확보 후 동일 경로로 동작.

# SquadOne_Tool — 사입 상품 추출 로직 (1~8단계 전체 스펙)

> 뉴스 트렌드 → 사입(소싱) 상품 추천 파이프라인. **모든 단계의 입출력은 PostgreSQL `newstrend` 스키마(DB)** 로 진행한다(단계 간 파일 경로 계약 없음). LLM 호출은 `steps/llm_factory.get_llm(role)` 일원화, Qdrant URL 은 `.env`(QDRANT_HOST/PORT) 단일 출처.
>
> 인프라(공유): PostgreSQL 5432, Qdrant 6333, Ollama 11434(Tailscale). **포트 8000/8001(SquadOne_AI·MCP_Server)·8010(본 REST)·5180(프론트)와 무관 — 본 로직은 서버 기동 없이 DB만 읽고 쓴다.**
>
> 최종 갱신: 2026-06-07 (7=클러스터링, 8=사입 상품 추출 다중 옵션 구조 확정).

---

## 0. 전체 구조 한눈에

```
[0 수집·적재] BigKinds/Naver/Agentic → qdrant_ingest → Qdrant(news_10y_ko_v1, 9.4M)
1 keyword → 2 frequency → 3 base → 4 z_score → 5 noise(keyword_class)
        │ z_score_keywords + keyword_class
        ├──────────────┬───────────────────┐
   [6 트렌드]       [7 클러스터링]
   trend_extractor   7-1 clustering → clusters, cluster_keywords(척추)
   (벡터검색)         7-2 trend_ts_cluster(군집 부피·강도)
        │                  │
        └────────┬─────────┘
        [8 사입 상품 추출 (다중 옵션)]
   (가)신호  8-신호-1 long_term(키워드 지속성)
            8-신호-2 period_trend(키워드 움직임)
            8-신호-3 cluster_interpretation(군집 해석)
   (결합)   v_keyword_enriched · v_cluster_enriched (cluster_keywords 축 JOIN/롤업)
   (나)옵션  8-1 product_extractor / 8-2 llm_questionarie / 8-3 geo_query→youtube_signal
   (다)검증  demand · market · social · naver_grounding
```

오케스트레이션:
- 본선 1~6: `tool_news_trend.run_news_trend_pipeline` (5 노이즈분류는 전제로 사전 적재)
- 7단계: `steps.enrichment_pipeline.run_stage7_clustering(week)` (MCP `run_clustering`)
- 8단계: `steps.enrichment_pipeline.run_stage8_sourcing(week)` (MCP `run_sourcing`)
- 7+8 통합: `run_enrichment_pipeline(week)` (MCP `run_enrichment`)

---

## 〔Stage 0〕 수집·적재 (상류)

**0a 수집 — collectors/**
- 입력: 외부 소스(빅카인즈 / 네이버 데이터랩 / 임의 웹).
- 처리: `bigkinds_downloader`(Selenium → 뉴스 xlsx) / `naver_categories`(getCategory.naver BFS → 분류체계) / `agentic_scraper`(ReAct+Playwright, 선택).
- 출력: `data/news/NewsResult_*.xlsx`, `newstrend.naver_categories`.

**0b 적재 — steps/qdrant_ingest.py**
- 입력: `data/news/*.xlsx` (config `ingest.data_dir`).
- 처리: 제목/본문 임베딩(paraphrase-multilingual-mpnet, normalize) → Qdrant named vector(title/body) upsert. 파일 sha256 체크섬 증분, 결정적 UUID point id.
- 출력: Qdrant `news_10y_ko_v1`, `newstrend.ingest_state`.

---

## 〔1~6단계〕 정형·트렌드

### 1단계 — keyword_extractor (키워드 추출)
- 입력: Qdrant `news_10y_ko_v1` 본문 (`input_source=qdrant`, 엑셀 폴백). 주차별 `iter_week_articles`(date 인덱스, file_overlap off).
- 처리: Kiwi(sbg) 명사 추출(NNG+형용사 기본형, 조사·인명 제외) → 불용어(`config/stopwords.txt`)·2자 이상 필터 → ISO 주차(YYYY-Www)별 (week, keyword, source) 빈도 집계. Kiwi 없으면 한글 2자+ 정규식 폴백.
- 출력: `newstrend.weekly_keywords (week, keyword, source, count)` upsert(증분).

### 2단계 — frequency_matrix (주차 빈도 집계)
- 입력: 1단계가 넘긴 `weeks` 리스트(또는 weekly_keywords.csv의 week).
- 처리: 순수 SQL 증분 — 해당 주차만 DELETE 후 `INSERT…SELECT sum(count) GROUP BY week,keyword`. 파이썬 왕복·dense 피벗 파일 없음.
- 출력: `newstrend.weekly_keyword_freq (week, keyword, count)`.

### 3단계 — base_calculation (기준선 계산)
- 입력: `weekly_keyword_freq`(long) → 메모리 dense 피벗(keyword×week).
- 처리: TF-IDF 필터(min_df=50, max_df=0.7; 짧은 테스트창 보정) → TfidfTransformer(norm=None) → 키워드별 104주(long_term_weeks) rolling(min_periods=1) mean/std(ddof=0, std=0→NaN).
- 출력: `newstrend.base_calculation (week, keyword, tfidf, base_mean, base_std)` dense(TRUNCATE 후 COPY).

### 4단계 — z_score_filtering (이상치 z-score)
- 입력: `base_calculation`(long→wide) + `weekly_keywords` 언론사 집계.
- 처리: 단기 평활 rolling(short_term_weeks=1) + EWM(span=moving_average_span=4, adjust=False) → z=(단기-base_mean)/base_std (std=0→NaN, NaN/Inf→0). 언론사 `a|b|c` 결합.
- 출력: `newstrend.z_score_keywords (week, keyword, z_score, sources)`. 고z(≥2.0)는 `v_z_score_high` 뷰.

### 5단계 — keyword_classifier (노이즈 분류, 6·7·8 필터용)
- 입력: `read_zscore_spikes(z≥z_threshold=2.0)` (keyword, week). person 은 추가로 Qdrant 본문.
- 처리(3종 멀티라벨):
  - seasonal(yoy_recurrence): 연도간 동일 ISO주차(±1) 재현 → seas_yrs≥3 & ratio≥0.6 & distinct_weeks≤15. + 12간지 사전.
  - politics(semantic_centroid): 시드 임베딩 centroid 대조 → top=politics & margin≥0.15 & top_sim≥0.5. + 정당약어 사전.
  - person(context_title): 성씨+길이 2~3 이름꼴이 본문에서 직책어 앞 ≥3회 → person.
- 출력: `newstrend.keyword_class (keyword, class, score, method, detail)`. → 6·7·8 anchor/후보에서 제외(seasonal/politics/person).

### 6단계 — trend_extractor (벡터검색 생애주기, 키워드 중심)
- 입력: 4단계 z_score_df + 2단계 weekly_df(또는 DB) + 5단계 keyword_class.
- 처리:
  - anchor: 주차별 고z 상위 anchor_top_n=5(노이즈 제외) + 전주 carryover(persistence_min_count=3, fading 2주 sunset).
  - 2-phase 벡터검색: Phase1 키워드 coarse 검색→상위 25기사 LLM 핵심문장 → Phase2 핵심문장 임베딩(body)→Qdrant 검색→근거 doc_id. date필터→file overlap→무필터→scroll 폴백.
  - 생애주기: Emerging(z≥2.0·신규)/Active(count비≥0.7)/Fading(z≤0.8·count비≤0.4)/Archived + delta_z/delta_count/count_ratio.
  - 시맨틱 그룹: feature 임베딩 코사인≥0.72 연결성분 → group_score(z_sum/count_log×1.2/ctx_mean/cohesion) 상위 max_groups=5, 전주 그룹 0.75 슬롯 연결.
  - LLM 요약(individual_summary/sequential_analysis).
- 출력: `newstrend.keysentence`, `trend_timeseries`, `trend_contexts`, `trend_groups`. (8-1 입력)

---

## 〔7단계〕 클러스터링 (군집 구조 + 결합 척추)

### 7-1 — clustering (기사 제목 군집화)
- 입력: 해당 주차 고z(z≥2.0, 노이즈 제외) 상위 `top_z_keywords=30` → 그 키워드가 제목에 포함된 기사(Qdrant, date-only, 키워드당 ≤max_titles_per_keyword=15, 총 ≤max_total_titles=8000). 기사 없으면 keyword_direct 폴백.
- 처리: 제목 임베딩(mpnet) → UMAP(n_neighbors=15, n_components=5, min_dist=0, cosine) → HDBSCAN(min_cluster_size=5, min_samples=1) → c-TF-IDF 대표어(top ctfidf_top_terms=8) → LLM 테마(role cluster_theme, 4~12자). 키워드는 자기 제목 다수결로 군집 배정(척추 cluster_keywords). 노이즈군집(-1)은 include_noise_cluster=false면 제외. **해석(cluster_interpretation)은 7단계에서 실행하지 않음 → 8-신호-3.**
- 출력: `newstrend.clusters (week, cluster_id, cluster_theme, representative_terms, keyword_count, avg_z_score, max_z_score, embedding_dim, reduced_dim)` + **`cluster_keywords (week, cluster_id, keyword, z_score)`** (8단계 결합의 축).

### 7-2 — trend_time_series_builder (군집 계량)
- 입력: 해당 주차 `cluster_keywords` + `weekly_keyword_freq`.
- 처리: 군집별 cluster_volume=Σ멤버빈도, cluster_intensity=평균 z, is_active=강도≥survival_threshold(0.5). (주차 스냅샷; 주차간 군집 추적 미도입 → stop_tracking=false.)
- 출력: `newstrend.trend_ts_cluster (week, cluster_id, cluster_volume, cluster_intensity, keyword_count, is_active, stop_tracking)`.

---

## 〔8단계〕 사입 상품 추출 (신호 → 결합 → 옵션 → 검증, 전부 실행)

### (가) 신호 — 판단 근거

#### 8-신호-1 — long_term_trend_bridge (키워드 장기 지속성)
- 입력: 후보 = 해당 주차 `cluster_keywords` ∪ 고z(≥2.0); `z_score_keywords` 이력(≤주차).
- 처리: 156주(window_weeks) z 매트릭스(결측 0) → active_ratio·mean/median/p75 z·slope(polyfit)·연속활성·peak → long_term_score=0.35·active_ratio+0.2·norm(mean_z)+0.15·norm(p75)+0.15·norm(slope)+0.15·norm(consec). passes_thresholds(active_ratio≥0.2 & active_weeks≥36 & mean_z≥0.15 & p75≥0.5 & slope≥-0.002 & score≥0.55), spike 폴백(peak≥2.5), 상위 top_n_per_week=80 selected_for_tracking.
- 출력: `newstrend.long_term_signals (week, keyword, window_weeks, active_weeks, active_ratio, mean_z, median_z, p75_z, slope_z_per_week, latest_consecutive_active_weeks, peak_week_z, long_term_score, passes_thresholds, selected_for_tracking)`.

#### 8-신호-2 — period_trend (키워드 현재 움직임)
- 입력: 후보 = 해당 주차 `cluster_keywords` ∪ 고z 상위 top_keywords=40(7단계 키워드 집합 일치); `weekly_keyword_freq` 이력.
- 처리: 미시 WoW%(직전주 대비 ≥micro_spike_pct=200) / 거시 최근 ~13주(macro_window_days=90/7) polyfit slope+R²(≥macro_min_r2=0.7 & slope>0) / 계절 52주 전 동주 비율 / 모멘텀 30주 베이스라인 z. 종합 라벨 short/long/mixed/neutral.
- 출력: `newstrend.period_trend_signals (as_of_week, keyword, point_mentions, z_score_30d_baseline, delta_1d_pct, wow_7d_pct, micro_spike_rule, macro_beta_ma7_90d, macro_r2_90d, macro_stable_uptrend, seasonal_ratio, window_primary_label, window_tags)`.

#### 8-신호-3 — cluster_interpretation (군집 해석)
- 입력: 7단계 `clusters` + `cluster_keywords`.
- 처리: 군집별 LLM 4구획 해석(role cluster_interpretation): ① 군집 의미 ② 핵심 고객/수요 신호 ③ 상품/콘텐츠 아이디어 ④ 리스크. include_noise_cluster=false면 -1 제외.
- 출력: `newstrend.cluster_interpretation (week, cluster_id, cluster_theme, keyword_count, avg_z_score, representative_terms, final_interpretation, llm_model)`.

### (결합) 8-결합 — 키워드↔클러스터 (DB 뷰, 읽기 시 자동 결합)
- 입력: cluster_keywords + clusters + trend_ts_cluster + long_term_signals + period_trend_signals + cluster_interpretation.
- 처리(마이그 0009):
  - **v_keyword_enriched**: cluster_keywords ⋈ clusters(theme) ⋈ long_term_signals(week,keyword) ⋈ period_trend_signals(as_of_week=week,keyword) → 키워드별 (cluster_id, cluster_theme, z_score, long_term_score, active_ratio, selected_for_tracking, window_primary_label, macro_stable_uptrend, wow_7d_pct).
  - **v_cluster_enriched**: clusters ⋈ trend_ts_cluster ⋈ cluster_interpretation + cluster_keywords 롤업(avg/max long_term_score, tracked_keyword_count, member_keywords) → 군집 종합 스코어카드.
- 출력: `newstrend.v_keyword_enriched`, `newstrend.v_cluster_enriched`. (REST `/v1/view/{enrichment,cluster-enriched,keyword-enriched}` · repository.read_*_enriched 소비)

### (나) 추출 옵션 — 결합 뷰 위에서 동작 (전부 구현)

#### 8-1 — product_extractor (트렌드 리포트 기반)
- 입력: 6단계 trend_timeseries(DB, report_df 없으면 repo.read_trend_timeseries) (+ 옵션 v_keyword_enriched로 selected_for_tracking 우선).
- 처리: 2단계 LLM(context_analyst→product_md), 제약(ip_clean/logistics/generic_naming), max_products=5.
- 출력: `newstrend.product_candidates (week, rank, product_name, selection_reason)` + `reports`.

#### 8-2 — llm_questionarie (기사 브리프 기반)
- 입력: 6-1 고z 키워드 + Qdrant 기사근거(≤max_article_chars=20000); 6-2/6-3 브리프 종합.
- 처리: 6-1 브리프(role trend_brief) → 6-2 관련상품 10개(role related_products) → 6-3 유튜브질의 12개(role youtube_queries, cluster_id=-1).
- 출력: `newstrend.llm_briefs`, `related_products (week, rank, product_name, rationale, source_keyword)`, `youtube_queries(cluster_id=-1)`.

#### 8-3 — geo_query → youtube_signal (군집·수요검증 기반)
- 입력: geo_query는 7단계 clusters+cluster_keywords(+해석); youtube_signal은 youtube_queries(군집별).
- 처리: geo_query — 군집별 페르소나 GEO질의 + 유튜브 검색셋(role geo_query, JSON, 폴백 템플릿). youtube_signal — YouTube Data API 검색→통계→VERC(V 조회수 log cap8, E 참여비 cap0.08, R 최신성 recency_weeks4, C 맥락키워드, P=0.25×4 가중합). 키 없으면 skip.
- 출력: `newstrend.geo_queries`, `youtube_queries`(군집별), `youtube_signals (…, p_score, meta)`.

### (다) 외부 검증·보강 (외부 API, 키 없으면 graceful skip)
- **demand_forecast**: related_products → 네이버 데이터랩(주간 90일)+PyTrends(today 3-m KR) → LLM(demand_analyst) → `demand_forecast (…, growth_signal[strong_up/up/flat/down], seasonality_hint, recommended_monitoring)`.
- **market_competition**: related_products → 네이버 쇼핑 API(display=sample_size=40) → LLM(market_analyst) → `market_competition (…, total_estimated, price_min/mean/max, estimated_margin_room[high/medium/low])`. ※현 키 401 → 네이버 앱 검색 API 권한 필요.
- **social_vibe**: related_products("○○ 리뷰") → YouTube → LLM(social_analyst) → `social_vibe (…, viral_potential[0~1], design_aesthetics, audience_pain_mentions)`.
- **naver_grounding**: related_products + product_candidates ↔ `naver_categories` 토큰 자카드+직접포함 fuzzy 매칭 → `reports(step='product_grounding', payload={mappings})`.

---

## 신규 DB 객체 요약 (마이그레이션)
- 0006 ingest_state · naver_categories · naver_category_trends
- 0007 clusters · cluster_keywords · cluster_interpretation · long_term_signals · trend_ts_cluster · period_trend_signals
- 0008 llm_briefs · related_products · youtube_queries · geo_queries · youtube_signals · demand_forecast · market_competition · social_vibe
- 0009 **v_keyword_enriched · v_cluster_enriched** (8-결합 뷰)

## 노출 (서버 기동 없이 호출; 포트 8000/8001 무관)
- MCP(`tool_news_trend.py`): run_news_trend(1~6) · run_clustering(7) · run_sourcing(8) · run_enrichment(7+8) · run_product_extractor.
- REST(`rest_mcp_server`, 8010): /v1/view/{enrichment, cluster-enriched, keyword-enriched, clusters, related-products, …}.

## 운영 노트
- Qdrant `news_10y_ko_v1`: 9.4M·2016~2026 전량. 주차 수집은 date 인덱스만(file_overlap off)으로 빠르게.
- LLM provider: `.env` LLM_PROVIDER(ollama 기본/gemini/mock). 외부 API 키 없으면 (다) 단계 skip.
- 단계 재실행은 주차 단위 멱등(테이블 replace / view 무상태).

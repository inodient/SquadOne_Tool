// 단계별 페이지 구성 — 1~8단계 + 각 단계가 보여줄 newstrend 객체(테이블/뷰).
// 1차: raw data 출력. 차트/결합 시각화는 후속 추가.

export interface StageTable {
  name: string; // newstrend 객체명 (REST /v1/view/raw?table=)
  label: string; // 화면 표기
  note?: string;
}

export interface StageDef {
  n: number; // 단계 번호
  slug: string; // 라우트 (/stage/:slug)
  title: string; // 탭 제목
  desc: string; // 부제
  tables: StageTable[];
}

export const STAGES: StageDef[] = [
  {
    n: 1, slug: "1", title: "1 키워드", desc: "keyword_extractor — Qdrant 본문→Kiwi 명사→주차 빈도",
    tables: [
      { name: "weekly_keywords_ctx_top", label: "맥락 보유 키워드 (week·keyword·source·count·대표맥락·주변어절)" },
      { name: "weekly_keywords_ctx_rest", label: "기타 키워드 (week·keyword·source·count)" },
    ],
  },
  {
    n: 2, slug: "2", title: "2 빈도", desc: "frequency_matrix — SQL 증분 집계",
    tables: [{ name: "weekly_keyword_freq", label: "weekly_keyword_freq (week·keyword·count)" }],
  },
  {
    n: 3, slug: "3", title: "3 기준선", desc: "base_calculation — TF-IDF + 104주 rolling mean/std",
    tables: [{ name: "base_calculation", label: "base_calculation (tfidf·base_mean·base_std)" }],
  },
  {
    n: 4, slug: "4", title: "4 z-score", desc: "z_score_filtering — EWM z-score",
    tables: [{ name: "z_score_keywords", label: "z_score_keywords (week·keyword·z_score·sources)" }],
  },
  {
    n: 5, slug: "5", title: "5 노이즈분류", desc: "keyword_classifier — seasonal/politics/person",
    tables: [{ name: "keyword_class", label: "keyword_class (keyword·class·method·score·detail)", note: "주차 무관(키워드 단위 라벨)" }],
  },
  {
    n: 6, slug: "6", title: "6 트렌드", desc: "trend_extractor — 벡터검색 생애주기 + 그룹 + 요약",
    tables: [
      { name: "trend_timeseries", label: "trend_timeseries (status·z_score·weekly_summary·evidence)" },
      { name: "trend_groups", label: "trend_groups (group_id·members·group_score·cohesion)" },
      { name: "keysentence", label: "keysentence (key_sentence·evidence_doc_ids)" },
      { name: "trend_contexts", label: "trend_contexts (doc_id·score·snippet)" },
    ],
  },
  {
    n: 7, slug: "7", title: "7 클러스터링", desc: "clustering — 기사 제목 군집화 + 군집 계량",
    tables: [
      { name: "clusters", label: "clusters (cluster_theme·representative_terms·avg/max_z)" },
      { name: "cluster_keywords", label: "cluster_keywords (cluster_id·keyword·z_score) — 결합 척추" },
      { name: "trend_ts_cluster", label: "trend_ts_cluster (cluster_volume·cluster_intensity·is_active)" },
    ],
  },
  {
    n: 8, slug: "8", title: "8 사입추출", desc: "사입 상품 추출 — 신호·결합·옵션·검증(다중 옵션)",
    tables: [
      { name: "v_cluster_enriched", label: "[결합] v_cluster_enriched (군집+부피/강도+해석+롤업)" },
      { name: "v_keyword_enriched", label: "[결합] v_keyword_enriched (키워드+군집+지속성+움직임)" },
      { name: "long_term_signals", label: "[신호-1] long_term_signals (지속성)" },
      { name: "period_trend_signals", label: "[신호-2] period_trend_signals (움직임)" },
      { name: "cluster_interpretation", label: "[신호-3] cluster_interpretation (군집 4구획 해석)" },
      { name: "product_candidates", label: "[옵션 8-1] product_candidates" },
      { name: "related_products", label: "[옵션 8-2] related_products" },
      { name: "llm_briefs", label: "[옵션 8-2] llm_briefs" },
      { name: "geo_queries", label: "[옵션 8-3] geo_queries" },
      { name: "youtube_queries", label: "[옵션 8-3] youtube_queries" },
      { name: "youtube_signals", label: "[옵션 8-3] youtube_signals (VERC)" },
      { name: "demand_forecast", label: "[검증] demand_forecast" },
      { name: "market_competition", label: "[검증] market_competition" },
      { name: "social_vibe", label: "[검증] social_vibe" },
    ],
  },
];

export const STAGE_BY_SLUG: Record<string, StageDef> = Object.fromEntries(
  STAGES.map((s) => [s.slug, s])
);

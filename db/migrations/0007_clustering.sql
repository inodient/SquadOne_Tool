-- 통합(Grand Integration) — Stage5B 클러스터링 분기 + 장기/시계열/주기 신호
-- serial 0007. 출처: SquadOne_Tool_NewsTrend(clustering·cluster_interpretation·
--   trend_time_series_builder·long_term_trend_bridge) + Prototype(period_trend).
CREATE SCHEMA IF NOT EXISTS newstrend;
SET search_path TO newstrend, public;

-- ──────────────────────────────────────────────────────────────
-- [5B-1] clusters: 주차별 군집 메타 (UMAP/HDBSCAN + c-TF-IDF + LLM 테마)
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.clusters (
    week                 text             NOT NULL,
    cluster_id           integer          NOT NULL,     -- -1 = noise
    cluster_theme        text,                          -- LLM 라벨
    representative_terms  text,                          -- c-TF-IDF 대표어(파이프 구분)
    keyword_count        integer,
    avg_z_score          double precision,
    max_z_score          double precision,
    embedding_dim        integer,
    reduced_dim          integer,
    updated_at           timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (week, cluster_id)
);
CREATE INDEX IF NOT EXISTS idx_clusters_week ON newstrend.clusters (week);

-- [5B-2] cluster_keywords: 군집-키워드 멤버십
CREATE TABLE IF NOT EXISTS newstrend.cluster_keywords (
    week       text             NOT NULL,
    cluster_id integer          NOT NULL,
    keyword    text             NOT NULL,
    z_score    double precision,
    updated_at timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (week, cluster_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_cluster_keywords_week_kw ON newstrend.cluster_keywords (week, keyword);

-- [5B-3] cluster_interpretation: 군집별 1인 셀러 인사이트(LLM 해석)
CREATE TABLE IF NOT EXISTS newstrend.cluster_interpretation (
    week               text        NOT NULL,
    cluster_id         integer     NOT NULL,
    cluster_theme      text,
    keyword_count      integer,
    avg_z_score        double precision,
    representative_terms text,
    final_interpretation text,                          -- 4구획(의미/수요신호/상품아이디어/리스크)
    llm_model          text,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, cluster_id)
);

-- ──────────────────────────────────────────────────────────────
-- [5B-4] long_term_signals: 키워드 3년 지속성 스코어 (long_term_trend_bridge)
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.long_term_signals (
    week                          text             NOT NULL,
    keyword                       text             NOT NULL,
    window_weeks                  integer,
    active_weeks                  integer,
    active_ratio                  double precision,
    mean_z                        double precision,
    median_z                      double precision,
    p75_z                         double precision,
    slope_z_per_week              double precision,
    latest_consecutive_active_weeks integer,
    peak_week_z                   double precision,
    long_term_score               double precision,
    passes_thresholds             boolean,
    selected_for_tracking         boolean,
    updated_at                    timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword)
);
CREATE INDEX IF NOT EXISTS idx_long_term_signals_week ON newstrend.long_term_signals (week);
CREATE INDEX IF NOT EXISTS idx_long_term_signals_score ON newstrend.long_term_signals (long_term_score);

-- ──────────────────────────────────────────────────────────────
-- [5B-5] trend_ts_cluster: 군집 시계열(부피/강도/생존) (trend_time_series_builder)
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.trend_ts_cluster (
    week              text             NOT NULL,
    cluster_id        integer          NOT NULL,
    cluster_volume    double precision,                 -- sum(frequency)
    cluster_intensity double precision,                 -- mean(z_score)
    keyword_count     integer,
    is_active         boolean,
    stop_tracking     boolean,
    updated_at        timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (week, cluster_id)
);
CREATE INDEX IF NOT EXISTS idx_trend_ts_cluster_week ON newstrend.trend_ts_cluster (week);

-- ──────────────────────────────────────────────────────────────
-- [5B-6] period_trend_signals: 미시/거시/계절/모멘텀 신호 (Prototype period_trend)
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.period_trend_signals (
    as_of_week           text             NOT NULL,     -- 분석 기준 주차
    keyword              text             NOT NULL,
    point_mentions       double precision,
    z_score_30d_baseline double precision,
    delta_1d_pct         double precision,
    wow_7d_pct           double precision,
    micro_spike_rule     boolean,
    macro_beta_ma7_90d   double precision,
    macro_r2_90d         double precision,
    macro_stable_uptrend boolean,
    seasonal_ratio       double precision,
    window_primary_label text,                          -- short/long/mixed/neutral
    window_tags          jsonb,
    updated_at           timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (as_of_week, keyword)
);
CREATE INDEX IF NOT EXISTS idx_period_trend_week ON newstrend.period_trend_signals (as_of_week);

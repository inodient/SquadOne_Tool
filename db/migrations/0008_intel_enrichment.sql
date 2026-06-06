-- 통합(Grand Integration) — Stage6 LLM 인텔 + Stage7B/C 상품 인리치먼트
-- serial 0008. 출처: NewsTrend(6-1/6-2/6-3) + Product_Extraction(GEO·VERC) +
--   Prototype(demand/market/social).
CREATE SCHEMA IF NOT EXISTS newstrend;
SET search_path TO newstrend, public;

-- ──────────────────────────────────────────────────────────────
-- [6-1] llm_briefs: 키워드별 트렌드 인텔리전스 브리프 (기사근거 요약)
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.llm_briefs (
    week          text        NOT NULL,
    keyword       text        NOT NULL,
    brief_text    text,
    article_count integer,
    llm_model     text,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword)
);
CREATE INDEX IF NOT EXISTS idx_llm_briefs_week ON newstrend.llm_briefs (week);

-- [6-2] related_products: 사입 가능 관련상품 추천(브리프 기반) — product_candidates와 별도 채널
CREATE TABLE IF NOT EXISTS newstrend.related_products (
    week           text        NOT NULL,
    rank           integer     NOT NULL,
    product_name   text,
    rationale      text,                              -- 트렌드-상품 연결근거
    source_keyword text,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, rank)
);

-- [6-3] youtube_queries: 콘텐츠 리서치용 유튜브 검색질의(군집별)
CREATE TABLE IF NOT EXISTS newstrend.youtube_queries (
    week         text        NOT NULL,
    cluster_id   integer     NOT NULL,
    seq          integer     NOT NULL,
    search_query text,
    search_type  text,                               -- 리뷰/하울/순위 등
    reasoning    text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, cluster_id, seq)
);

-- ──────────────────────────────────────────────────────────────
-- [7B-1] geo_queries: 페르소나 기반 GEO 상품발굴 질의(군집별)
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.geo_queries (
    week            text        NOT NULL,
    cluster_id      integer     NOT NULL,
    seq             integer     NOT NULL,
    target_audience text,
    query           text,
    expected_insight text,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, cluster_id, seq)
);

-- [7B-2] youtube_signals: VERC 수요검증 스코어 (Volume/Engagement/Recency/Context)
CREATE TABLE IF NOT EXISTS newstrend.youtube_signals (
    week                 text             NOT NULL,
    cluster_id           integer          NOT NULL,
    search_query         text             NOT NULL,
    search_type          text,
    video_count          integer,
    total_views          bigint,
    avg_views            double precision,
    v_volume_score       double precision,
    e_engagement_score   double precision,
    r_recency_score      double precision,
    c_context_score      double precision,
    p_score              double precision,            -- 종합
    engagement_ratio_raw double precision,
    context_ratio        double precision,
    meta                 jsonb,                        -- 가중치/정규화모드 등 실행 메타
    updated_at           timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (week, cluster_id, search_query)
);
CREATE INDEX IF NOT EXISTS idx_youtube_signals_week ON newstrend.youtube_signals (week);
CREATE INDEX IF NOT EXISTS idx_youtube_signals_pscore ON newstrend.youtube_signals (p_score);

-- ──────────────────────────────────────────────────────────────
-- [7C] demand / market / social 인리치먼트 (Prototype 에이전트, 외부 API)
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.demand_forecast (
    week                 text        NOT NULL,
    product_group        text        NOT NULL,
    keywords_used        jsonb,
    demand_summary       text,
    growth_signal        text,                         -- strong_up/up/flat/down
    seasonality_hint     text,
    recommended_monitoring jsonb,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, product_group)
);

CREATE TABLE IF NOT EXISTS newstrend.market_competition (
    week                text             NOT NULL,
    product_group       text             NOT NULL,
    market_query        text,
    total_estimated     bigint,
    price_min           double precision,
    price_mean          double precision,
    price_max           double precision,
    competition_summary text,
    estimated_margin_room text,                        -- high/medium/low
    sample_titles       jsonb,
    updated_at          timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (week, product_group)
);

CREATE TABLE IF NOT EXISTS newstrend.social_vibe (
    week             text             NOT NULL,
    product_group    text             NOT NULL,
    vibe_query       text,
    video_count      integer,
    vibe_summary     text,
    design_aesthetics jsonb,
    audience_pain_mentions jsonb,
    viral_potential  double precision,                 -- 0~1
    instagram_note   text,
    updated_at       timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (week, product_group)
);

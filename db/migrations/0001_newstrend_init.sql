-- SquadOne 뉴스트렌드 파이프라인 DB-ification — P0 초기 스키마
-- serial 0001. 공유 squadone DB 내 newstrend 스키마로 격리.
-- 멱등성: 모든 객체 IF NOT EXISTS. 기존 가동 DB에는 db/migrate.py(psql)로 수동 적용.
-- 설계 출처: 레포 루트 refactoring_plan.md §3.

CREATE EXTENSION IF NOT EXISTS pgcrypto;          -- gen_random_uuid()
CREATE SCHEMA IF NOT EXISTS newstrend;
SET search_path TO newstrend, public;

-- ──────────────────────────────────────────────────────────────
-- [1] 원천 fact (유일하게 반드시 저장). (week, keyword, source) 그레인.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.weekly_keywords (
    week        text        NOT NULL,          -- 'YYYY-Www'
    keyword     text        NOT NULL,
    source      text        NOT NULL,          -- 언론사 (Qdrant payload: press)
    count       integer     NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword, source)
);
CREATE INDEX IF NOT EXISTS idx_weekly_keywords_week    ON newstrend.weekly_keywords (week);
CREATE INDEX IF NOT EXISTS idx_weekly_keywords_keyword ON newstrend.weekly_keywords (keyword);

-- [2] frequency_matrix: dense 피벗은 저장하지 않음.
--     주차×키워드 합계(희소)만 materialized view로. 3단계 TF-IDF는 메모리에서 임시 피벗.
CREATE MATERIALIZED VIEW IF NOT EXISTS newstrend.mv_weekly_keyword_freq AS
SELECT week, keyword, sum(count)::int AS count
FROM newstrend.weekly_keywords
GROUP BY week, keyword
WITH NO DATA;
CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_weekly_keyword_freq
    ON newstrend.mv_weekly_keyword_freq (week, keyword);   -- REFRESH CONCURRENTLY 위해 필요

-- ──────────────────────────────────────────────────────────────
-- [3] base_calculation (long). 기존 wide CSV는 백필 시 melt 변환.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.base_calculation (
    week       text             NOT NULL,
    keyword    text             NOT NULL,
    tfidf      double precision,
    base_mean  double precision,
    base_std   double precision,
    updated_at timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword)
);
CREATE INDEX IF NOT EXISTS idx_base_calculation_week ON newstrend.base_calculation (week);

-- ──────────────────────────────────────────────────────────────
-- [4] z_score (long). 고z(≥2.0)는 뷰로. _group 산출물은 폐기(생성 안 함).
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.z_score_keywords (
    week       text             NOT NULL,
    keyword    text             NOT NULL,
    z_score    double precision NOT NULL,
    sources    text,
    updated_at timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword)
);
CREATE INDEX IF NOT EXISTS idx_z_score_week ON newstrend.z_score_keywords (week);

CREATE OR REPLACE VIEW newstrend.v_z_score_high AS
SELECT week, keyword, z_score, sources, updated_at
FROM newstrend.z_score_keywords
WHERE z_score >= 2.0;

-- ──────────────────────────────────────────────────────────────
-- [5] keysentence (벡터검색 + LLM 요약 결과). evidence_doc_ids = Qdrant news_id.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.keysentence (
    week             text        NOT NULL,
    keyword          text        NOT NULL,
    query_text       text,                      -- 6단계 검색 시드
    key_sentence     text,
    evidence_doc_ids text[],                    -- Qdrant news_id 목록
    evidence_count   integer,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword)
);

-- ──────────────────────────────────────────────────────────────
-- [6] trend 메인 + 보조 (contexts, groups). dashboard는 뷰로 대체.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.trend_timeseries (
    week             text             NOT NULL,
    keyword          text             NOT NULL,
    trend_slot_id    text,
    group_id         text,
    group_score      double precision,
    status           text,                      -- Emerging/Active/Fading/Archived
    status_reason    text,
    z_score          double precision,
    count            integer,
    weekly_summary   text,
    evidence_doc_ids text[],
    updated_at       timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword)
);
CREATE INDEX IF NOT EXISTS idx_trend_timeseries_week  ON newstrend.trend_timeseries (week);
CREATE INDEX IF NOT EXISTS idx_trend_timeseries_group ON newstrend.trend_timeseries (week, group_id);

CREATE TABLE IF NOT EXISTS newstrend.trend_contexts (
    week    text             NOT NULL,
    keyword text             NOT NULL,
    doc_id  text             NOT NULL,          -- Qdrant news_id
    score   double precision,
    snippet text,
    PRIMARY KEY (week, keyword, doc_id)
);

CREATE TABLE IF NOT EXISTS newstrend.trend_groups (
    week        text             NOT NULL,
    group_id    text             NOT NULL,
    members     jsonb,
    group_score double precision,
    cohesion    double precision,
    PRIMARY KEY (week, group_id)
);

CREATE OR REPLACE VIEW newstrend.v_trend_dashboard AS
SELECT week, keyword, trend_slot_id, group_id, group_score,
       status, z_score AS trend_strength, weekly_summary, evidence_doc_ids
FROM newstrend.trend_timeseries;

-- ──────────────────────────────────────────────────────────────
-- [7] product. 후보는 정형 테이블, LLM 자유서술 리포트는 범용 jsonb.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.product_candidates (
    week             text        NOT NULL,
    rank             integer     NOT NULL,
    product_name     text,
    selection_reason text,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, rank)
);

CREATE TABLE IF NOT EXISTS newstrend.reports (
    step       text        NOT NULL,            -- 'product_context' 등
    week       text        NOT NULL,
    payload    jsonb       NOT NULL,
    meta       jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (step, week)
);

-- ──────────────────────────────────────────────────────────────
-- 파이프라인 실행 추적 (MCP 통짜 노출용).
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.pipeline_runs (
    run_id      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    params      jsonb,
    status      text,                           -- running/success/failed
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

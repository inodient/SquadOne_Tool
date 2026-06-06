-- 통합(Grand Integration) — Stage0 수집·적재 + Naver 카테고리 그라운딩
-- serial 0006. 멱등성: 모든 객체 IF NOT EXISTS. newstrend 스키마.
CREATE SCHEMA IF NOT EXISTS newstrend;
SET search_path TO newstrend, public;

-- ──────────────────────────────────────────────────────────────
-- Stage0b: Qdrant 적재(ingest) 상태 추적. 파일 체크섬 기반 멱등 재적재.
--   (원본 .state/news_ingest_state.json 을 DB로 승격 — 운영 가시성)
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.ingest_state (
    source_file text        NOT NULL,             -- NewsResult_YYYYMMDD-YYYYMMDD.xlsx
    checksum    text        NOT NULL,             -- sha256 (또는 filename-only 모드 표식)
    point_count integer,                          -- 적재된 포인트 수
    collection  text,                             -- 대상 Qdrant 컬렉션
    ingested_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_file)
);
CREATE INDEX IF NOT EXISTS idx_ingest_state_ingested_at ON newstrend.ingest_state (ingested_at);

-- ──────────────────────────────────────────────────────────────
-- Stage7d: Naver 쇼핑 카테고리 분류체계 (상품 후보 그라운딩 참조 데이터)
--   fetch_shopping_categories.py(getCategory.naver) 산출물을 적재.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS newstrend.naver_categories (
    cid        text        NOT NULL,              -- 리프 category id
    level      integer,                           -- 1~4
    name       text,                              -- 리프 카테고리명
    full_path  text,                              -- "패션의류>여성의류>니트>풀오버"
    cat1       text,
    cat2       text,
    cat3       text,
    cat4       text,
    leaf       boolean     DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (cid)
);
CREATE INDEX IF NOT EXISTS idx_naver_categories_cat4 ON newstrend.naver_categories (cat4);
CREATE INDEX IF NOT EXISTS idx_naver_categories_name ON newstrend.naver_categories (name);

-- Stage7d(보조): 카테고리별 주간 트렌드 지수 (DataLab 쇼핑인사이트 다운로드 후 적재)
CREATE TABLE IF NOT EXISTS newstrend.naver_category_trends (
    week          text             NOT NULL,      -- 'YYYY-Www' (또는 date 정규화)
    category_path text             NOT NULL,
    category_id   text,
    trend_index   double precision,               -- 0~100 정규화 지수
    updated_at    timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (week, category_path)
);
CREATE INDEX IF NOT EXISTS idx_naver_cat_trends_week ON newstrend.naver_category_trends (week);

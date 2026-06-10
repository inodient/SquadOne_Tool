-- 0019_trend_threads.sql
-- 주 간 트렌드 연결(thread): A 동일 반복 + B 진화(1심→2심→3심)를 한 레이어에서.
--   재료: weekly_trend_clusters.centroid(군집 중심) — 같은 임베딩 공간이라 주 간 비교가 공짜.
--   연결기(steps/trend_linker.py)가 범위 단위로 산출 → trend_threads / trend_threads_meta.
-- 멱등: ADD COLUMN IF NOT EXISTS + CREATE TABLE IF NOT EXISTS + CREATE OR REPLACE VIEW.

-- 트렌드 군집에 centroid(중심 벡터) 저장 — 주 간 연결의 핵심 재료.
ALTER TABLE newstrend.weekly_trend_clusters
    ADD COLUMN IF NOT EXISTS centroid double precision[];

-- thread 멤버: 각 주차 트렌드가 어느 thread에 속하나.
CREATE TABLE IF NOT EXISTS newstrend.trend_threads (
    thread_id   integer     NOT NULL,
    week        text        NOT NULL,
    cluster_id  integer     NOT NULL,
    seq         integer     NOT NULL,          -- thread 내 순번(0=시작)
    link_score  double precision,              -- 이전 멤버와의 연결 점수
    link_type   text,                          -- seed | near(인접) | gap(간헐, 진화)
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, cluster_id)
);
CREATE INDEX IF NOT EXISTS idx_trend_threads_tid ON newstrend.trend_threads (thread_id);

-- thread 요약: 기간·종류(반복/진화)·라벨 궤적·정점.
CREATE TABLE IF NOT EXISTS newstrend.trend_threads_meta (
    thread_id    integer     PRIMARY KEY,
    n_weeks      integer     NOT NULL,          -- 활성 주차 수
    start_week   text,
    end_week     text,
    span_weeks   integer,                       -- 시작~종료 달력 주차 폭
    kind         text,                          -- 반복 | 진화 | 단발
    drift        double precision,              -- 연속 centroid 평균 코사인(낮을수록 진화)
    peak_weight  integer,
    label        text,                          -- 대표 라벨(정점 주차)
    label_path   text,                          -- 주차별 라벨 궤적(→ 연결)
    top_keywords text,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- 표시 뷰: thread 타임라인(thread별 주차 흐름).
CREATE OR REPLACE VIEW newstrend.trend_threads_disp AS
SELECT
    t.thread_id,
    m.kind                                      AS "종류",
    t.seq,
    t.week,
    c.weight                                    AS "가중치",
    c.label                                     AS "주차라벨",
    t.link_type                                 AS "연결",
    round(t.link_score::numeric, 3)             AS "점수",
    c.rep_sentence                              AS "대표문장"
FROM newstrend.trend_threads t
JOIN newstrend.weekly_trend_clusters c ON c.week = t.week AND c.cluster_id = t.cluster_id
LEFT JOIN newstrend.trend_threads_meta m ON m.thread_id = t.thread_id
ORDER BY t.thread_id, t.seq;

-- 요약 뷰: thread 한 줄 요약(기간·종류·라벨 궤적).
CREATE OR REPLACE VIEW newstrend.trend_threads_summary AS
SELECT
    thread_id,
    kind                                        AS "종류",
    n_weeks                                     AS "활성주",
    span_weeks                                  AS "기간(주)",
    start_week                                  AS "시작",
    end_week                                    AS "종료",
    peak_weight                                 AS "정점가중",
    label                                       AS "대표어",
    label_path                                  AS "라벨궤적",
    top_keywords                                AS "키워드"
FROM newstrend.trend_threads_meta
ORDER BY peak_weight DESC;

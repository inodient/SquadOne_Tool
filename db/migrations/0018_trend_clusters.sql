-- 0018_trend_clusters.sql
-- 대안 C 산출물: 주차 "트렌드 군집"(전역 문장 군집). 키워드 sense의 원천.
--   한 주의 (제목+리드) 문장을 전역 1회 군집 → 각 군집 = 그 주의 트렌드(이야기 갈래).
--   weekly_keyword_sense 는 이 트렌드에 키워드를 매핑한 결과(0015~0017).
-- 멱등: IF NOT EXISTS + CREATE OR REPLACE VIEW. 주차 단위 replace.

CREATE TABLE IF NOT EXISTS newstrend.weekly_trend_clusters (
    week          text        NOT NULL,
    cluster_id    integer     NOT NULL,          -- weight 순위(0=가장 큰 트렌드)
    size          integer     NOT NULL,          -- 군집 문장 수
    weight        integer     NOT NULL,          -- 군집 가중합(제목/리드 위치 가중)
    label         text,                          -- 대표어(군집 최빈 키워드)
    rep_sentence  text,                          -- 대표 문장(가중 최다)
    top_keywords  text,                          -- 군집 상위 키워드(쉼표)
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, cluster_id)
);

CREATE INDEX IF NOT EXISTS idx_wktrend_week ON newstrend.weekly_trend_clusters (week);

-- 표시용 뷰: weight 내림차순(큰 트렌드 위).
CREATE OR REPLACE VIEW newstrend.weekly_trend_clusters_disp AS
SELECT
    week,
    cluster_id                                  AS "트렌드",
    size                                        AS "문장수",
    weight                                      AS "가중치",
    label                                       AS "대표어",
    top_keywords                                AS "키워드",
    rep_sentence                                AS "대표문장",
    updated_at
FROM newstrend.weekly_trend_clusters
ORDER BY week, weight DESC;

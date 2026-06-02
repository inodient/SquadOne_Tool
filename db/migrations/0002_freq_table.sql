-- P1: frequency_matrix 증분화 — matview(mv_weekly_keyword_freq) → 일반 테이블(weekly_keyword_freq)
-- 이유: "주차별 누적 → 증분만 계산". matview는 전체 REFRESH만 가능하므로,
--       처리 주차만 DELETE+INSERT 하는 증분 유지 테이블로 대체한다.
-- 멱등: DROP IF EXISTS / CREATE IF NOT EXISTS / INSERT ON CONFLICT DO NOTHING.

SET search_path TO newstrend, public;

DROP MATERIALIZED VIEW IF EXISTS newstrend.mv_weekly_keyword_freq;

CREATE TABLE IF NOT EXISTS newstrend.weekly_keyword_freq (
    week       text        NOT NULL,
    keyword    text        NOT NULL,
    count      integer     NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword)
);
CREATE INDEX IF NOT EXISTS idx_weekly_keyword_freq_week ON newstrend.weekly_keyword_freq (week);

-- 초기 채움(멱등): weekly_keywords 집계로 1회 채운다(이미 있으면 no-op).
INSERT INTO newstrend.weekly_keyword_freq (week, keyword, count)
SELECT week, keyword, sum(count)::int
FROM newstrend.weekly_keywords
GROUP BY week, keyword
ON CONFLICT (week, keyword) DO NOTHING;

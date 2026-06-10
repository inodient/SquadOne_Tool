-- 0017_keyword_sense_sentence.sql
-- sense 문장 모드: 대표 "문장 전체"를 저장할 컬럼 추가 + 표시 뷰가 문장을 우선 표시.
--   어절 모드(기존)는 rep_before/rep_after로 조립, 문장 모드는 rep_sentence(문장 전체)로 표시.
-- 멱등: ADD COLUMN IF NOT EXISTS + CREATE OR REPLACE VIEW.

ALTER TABLE newstrend.weekly_keyword_sense
    ADD COLUMN IF NOT EXISTS rep_sentence text;

-- 표시 뷰(0015 disp): 대표맥락 = 문장 있으면 문장, 없으면 어절 조립.
CREATE OR REPLACE VIEW newstrend.weekly_keyword_sense_disp AS
SELECT
    s.week,
    s.keyword,
    s.sense_id,
    s.count,
    s.label,
    COALESCE(
        NULLIF(s.rep_sentence, ''),
        '…' || COALESCE(s.rep_before, '') || ' [' || s.keyword || '] '
             || COALESCE(s.rep_after, '') || '…'
    )                                                   AS "대표맥락",
    s.top_neighbors                                     AS "주변어절",
    s.updated_at
FROM newstrend.weekly_keyword_sense s
ORDER BY s.week, s.keyword, s.count DESC;

-- 단일 의미(0016 single): 대표맥락을 문장 우선으로.
CREATE OR REPLACE VIEW newstrend.weekly_keyword_sense_single AS
SELECT
    s.week,
    s.keyword,
    s.count                                             AS "빈도",
    s.label                                             AS "의미",
    COALESCE(
        NULLIF(s.rep_sentence, ''),
        '…' || COALESCE(s.rep_before, '') || ' [' || s.keyword || '] '
             || COALESCE(s.rep_after, '') || '…'
    )                                                   AS "대표맥락",
    s.top_neighbors                                     AS "주변어절"
FROM newstrend.weekly_keyword_sense s
WHERE (s.week, s.keyword) IN (
    SELECT week, keyword
    FROM newstrend.weekly_keyword_sense
    GROUP BY week, keyword
    HAVING count(*) = 1
)
ORDER BY s.week, s.count DESC;

-- 0016_keyword_sense_split.sql
-- 1단계 화면: 의미 분화(sense) 2분할 — 맥락 2분할(0013)과 동일 철학.
--   위:  다의어 키워드(의미 2개+) — 키워드당 1행, 의미 그룹을 빈도순 한 줄로 압축.
--   아래: 단일 의미 키워드(의미 1개) — 키워드당 1행 + 대표맥락/주변어절.
-- 원천 weekly_keyword_sense(week·keyword·sense_id·count·label·rep_before·rep_after·top_neighbors)는 불변.

-- 위 표: 다의어(의미 2개 이상)만. "가격 → ▸부동산 234  ▸생필품 120  ▸농산물 60".
CREATE OR REPLACE VIEW newstrend.weekly_keyword_sense_multi AS
SELECT
    week,
    keyword,
    count(*)                                                    AS "의미수",
    string_agg(
        '▸' || COALESCE(NULLIF(label, ''), '(미상)') || ' ' || count,
        '   ' ORDER BY count DESC
    )                                                           AS "의미 그룹",
    sum(count)                                                  AS "총빈도"
FROM newstrend.weekly_keyword_sense
GROUP BY week, keyword
HAVING count(*) >= 2
ORDER BY week, sum(count) DESC;

-- 아래 표: 단일 의미(의미 1개)만. 대표맥락은 0015 disp 스타일로 사람이 읽는 문장으로 조립.
CREATE OR REPLACE VIEW newstrend.weekly_keyword_sense_single AS
SELECT
    s.week,
    s.keyword,
    s.count                                                     AS "빈도",
    s.label                                                     AS "의미",
    '…' || COALESCE(s.rep_before, '') || ' [' || s.keyword || '] '
         || COALESCE(s.rep_after, '') || '…'                    AS "대표맥락",
    s.top_neighbors                                             AS "주변어절"
FROM newstrend.weekly_keyword_sense s
WHERE (s.week, s.keyword) IN (
    SELECT week, keyword
    FROM newstrend.weekly_keyword_sense
    GROUP BY week, keyword
    HAVING count(*) = 1
)
ORDER BY s.week, s.count DESC;

-- 0013_keyword_context_split.sql
-- 1단계 화면 분할: 맥락 보유 키워드(위, 7컬럼) / 나머지 키워드(아래, 5컬럼).
-- 0012 단일 뷰(weekly_keywords_ctx) 대체.

DROP VIEW IF EXISTS newstrend.weekly_keywords_ctx;

-- 위 표: 대표맥락·주변어절이 있는 키워드(=주차별 상위 N)만 + 맥락 컬럼.
CREATE OR REPLACE VIEW newstrend.weekly_keywords_ctx_top AS
SELECT
    wk.week,
    wk.keyword,
    wk.source,
    wk.count,
    ctx."대표맥락",
    nbr."주변어절",
    wk.updated_at
FROM newstrend.weekly_keywords wk
JOIN (
    SELECT
        week, keyword,
        string_agg(
            '…' || COALESCE(ctx_before, '') || ' [' || keyword || '] '
                || COALESCE(ctx_after, '') || '… (×' || occ_count || ')',
            E'\n' ORDER BY rank
        ) AS "대표맥락"
    FROM newstrend.weekly_keyword_context
    GROUP BY week, keyword
) ctx ON ctx.week = wk.week AND ctx.keyword = wk.keyword
LEFT JOIN (
    SELECT
        week, keyword,
        '앞: ' || COALESCE(string_agg(term, ', ' ORDER BY count DESC) FILTER (WHERE position = 'before'), '-')
              || '  /  뒤: ' || COALESCE(string_agg(term, ', ' ORDER BY count DESC) FILTER (WHERE position = 'after'), '-')
            AS "주변어절"
    FROM newstrend.weekly_keyword_neighbor
    GROUP BY week, keyword
) nbr ON nbr.week = wk.week AND nbr.keyword = wk.keyword;

-- 아래 표: 맥락이 없는 나머지 키워드(변경 전 5컬럼).
CREATE OR REPLACE VIEW newstrend.weekly_keywords_ctx_rest AS
SELECT
    wk.week, wk.keyword, wk.source, wk.count, wk.updated_at
FROM newstrend.weekly_keywords wk
WHERE NOT EXISTS (
    SELECT 1 FROM newstrend.weekly_keyword_context ctx
    WHERE ctx.week = wk.week AND ctx.keyword = wk.keyword
);

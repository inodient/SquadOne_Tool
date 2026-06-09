-- 0014_freq_context_view.sql
-- 2단계 화면 분할: 맥락 보유 키워드(위, 5컬럼+맥락) / 나머지(아래, 3컬럼).
-- weekly_keyword_freq 는 (week,keyword) 단위라 맥락이 키워드당 1행으로 깔끔하게 붙는다(source 중복 없음).

-- 위 표: 대표맥락·주변어절이 있는 키워드만.
CREATE OR REPLACE VIEW newstrend.weekly_keyword_freq_ctx_top AS
SELECT
    f.week,
    f.keyword,
    f.count,
    ctx."대표맥락",
    nbr."주변어절",
    f.updated_at
FROM newstrend.weekly_keyword_freq f
JOIN (
    SELECT week, keyword,
        string_agg(
            '…' || COALESCE(ctx_before, '') || ' [' || keyword || '] '
                || COALESCE(ctx_after, '') || '… (×' || occ_count || ')',
            E'\n' ORDER BY rank
        ) AS "대표맥락"
    FROM newstrend.weekly_keyword_context
    GROUP BY week, keyword
) ctx ON ctx.week = f.week AND ctx.keyword = f.keyword
LEFT JOIN (
    SELECT week, keyword,
        '앞: ' || COALESCE(string_agg(term, ', ' ORDER BY count DESC) FILTER (WHERE position = 'before'), '-')
              || '  /  뒤: ' || COALESCE(string_agg(term, ', ' ORDER BY count DESC) FILTER (WHERE position = 'after'), '-')
            AS "주변어절"
    FROM newstrend.weekly_keyword_neighbor
    GROUP BY week, keyword
) nbr ON nbr.week = f.week AND nbr.keyword = f.keyword;

-- 아래 표: 맥락 없는 나머지 키워드.
CREATE OR REPLACE VIEW newstrend.weekly_keyword_freq_ctx_rest AS
SELECT f.week, f.keyword, f.count, f.updated_at
FROM newstrend.weekly_keyword_freq f
WHERE NOT EXISTS (
    SELECT 1 FROM newstrend.weekly_keyword_context ctx
    WHERE ctx.week = f.week AND ctx.keyword = f.keyword
);

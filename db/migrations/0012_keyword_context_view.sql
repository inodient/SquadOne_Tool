-- 0012_keyword_context_view.sql
-- 1단계 화면용: weekly_keywords + 키워드 맥락(B 대표예문 + C 주변어절) 결합 뷰.
-- RawTable 이 컬럼을 동적 수용하므로 뷰 컬럼을 그대로 표시한다.
--   대표맥락 : weekly_keyword_context 의 rank순 예문 top3 (…앞 [키워드] 뒤… (×occ))
--   주변어절 : weekly_keyword_neighbor 의 앞/뒤 빈도순 어절
-- 맥락은 (week,keyword) 단위라 같은 키워드의 source별 행에 동일 표시된다.

CREATE OR REPLACE VIEW newstrend.weekly_keywords_ctx AS
SELECT
    wk.week,
    wk.keyword,
    wk.source,
    wk.count,
    ctx."대표맥락",
    nbr."주변어절",
    wk.updated_at
FROM newstrend.weekly_keywords wk
LEFT JOIN (
    SELECT
        week,
        keyword,
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
        week,
        keyword,
        '앞: ' || COALESCE(string_agg(term, ', ' ORDER BY count DESC) FILTER (WHERE position = 'before'), '-')
              || '  /  뒤: ' || COALESCE(string_agg(term, ', ' ORDER BY count DESC) FILTER (WHERE position = 'after'), '-')
            AS "주변어절"
    FROM newstrend.weekly_keyword_neighbor
    GROUP BY week, keyword
) nbr ON nbr.week = wk.week AND nbr.keyword = wk.keyword;

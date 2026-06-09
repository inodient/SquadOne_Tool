-- 0011_keyword_context.sql
-- 1단계 부가: 키워드 맥락(context). 주차별 상위 N 빈도 키워드만 저장(경량).
--   B. weekly_keyword_context  : 대표 맥락 예문 top3 (앞2어절 [키워드] 뒤2어절)
--   C. weekly_keyword_neighbor : 키워드 앞/뒤 어절 빈도 top-k (통계적 맥락)
-- 키워드 추출 로직(weekly_keywords)은 불변. 본문 어절 기준 부산물로 수집.

CREATE TABLE IF NOT EXISTS newstrend.weekly_keyword_context (
    week        text        NOT NULL,
    keyword     text        NOT NULL,
    rank        smallint    NOT NULL,          -- 1~3 (빈도순 대표 맥락)
    ctx_before  text,                           -- 앞 2어절
    ctx_after   text,                           -- 뒤 2어절
    occ_count   integer     NOT NULL,           -- 이 (before,after) 패턴 출현 수
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword, rank)
);

CREATE TABLE IF NOT EXISTS newstrend.weekly_keyword_neighbor (
    week        text        NOT NULL,
    keyword     text        NOT NULL,
    position    text        NOT NULL,           -- 'before' | 'after'
    term        text        NOT NULL,           -- 어절
    count       integer     NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword, position, term)
);

CREATE INDEX IF NOT EXISTS idx_wkctx_week ON newstrend.weekly_keyword_context (week);
CREATE INDEX IF NOT EXISTS idx_wknbr_week ON newstrend.weekly_keyword_neighbor (week);

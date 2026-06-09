-- 0015_keyword_sense.sql
-- 1단계 부가: 키워드 "의미 분화"(sense). 다의어를 맥락 클러스터로 갈라 의미 그룹화.
--   방법 B(문맥 임베딩): (앞2어절 [키워드] 뒤2어절) 맥락 문장을 임베딩 → 키워드별 군집.
--   예) "가격" → ▸부동산 234 / ▸생필품 120 / ▸농산물 60
-- 키워드 추출(weekly_keywords)·맥락(weekly_keyword_context/neighbor)은 불변. 별개 부산물.
-- 주차 단위 replace(처리 주차 DELETE 선행)로 멱등 재실행.

CREATE TABLE IF NOT EXISTS newstrend.weekly_keyword_sense (
    week          text        NOT NULL,
    keyword       text        NOT NULL,
    sense_id      smallint    NOT NULL,          -- 0,1,2... 의미 그룹 번호(count 내림차순)
    count         integer     NOT NULL,          -- 그룹 출현 수(맥락 occ_count 합)
    label         text,                          -- 그룹 대표어(주변어절 최빈, 예 "아파트")
    rep_before    text,                          -- 대표 예문 앞 2어절
    rep_after     text,                          -- 대표 예문 뒤 2어절
    top_neighbors text,                          -- 그룹 주변어절 요약(예 "아파트, 주택, 동향")
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week, keyword, sense_id)
);

CREATE INDEX IF NOT EXISTS idx_wksense_week ON newstrend.weekly_keyword_sense (week);
CREATE INDEX IF NOT EXISTS idx_wksense_kw   ON newstrend.weekly_keyword_sense (week, keyword);

-- 표시용 뷰: 대표맥락을 사람이 읽는 문장으로 조립(0014 스타일).
CREATE OR REPLACE VIEW newstrend.weekly_keyword_sense_disp AS
SELECT
    s.week,
    s.keyword,
    s.sense_id,
    s.count,
    s.label,
    '…' || COALESCE(s.rep_before, '') || ' [' || s.keyword || '] '
         || COALESCE(s.rep_after, '') || '…'           AS "대표맥락",
    s.top_neighbors                                     AS "주변어절",
    s.updated_at
FROM newstrend.weekly_keyword_sense s
ORDER BY s.week, s.keyword, s.count DESC;

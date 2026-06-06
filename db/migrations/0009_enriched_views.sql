-- 통합(Grand Integration) — 8-결합: 키워드↔클러스터 결합 뷰
-- serial 0009. cluster_keywords 를 축으로 키워드 신호(지속성·움직임)와
-- 군집 신호(해석·부피·강도)를 JOIN/롤업해 단일 소비 뷰 2종을 제공한다.
-- 읽기 전용 뷰(데이터 중복 없음) — 모든 입출력 DB 원칙 준수.
CREATE SCHEMA IF NOT EXISTS newstrend;
SET search_path TO newstrend, public;

-- [키워드 단위] 각 키워드: 소속 군집 + 장기 지속성 + 현재 움직임
CREATE OR REPLACE VIEW newstrend.v_keyword_enriched AS
SELECT
    ck.week,
    ck.keyword,
    ck.cluster_id,
    c.cluster_theme,
    ck.z_score,
    lt.long_term_score,
    lt.active_ratio,
    lt.selected_for_tracking,
    pt.window_primary_label,
    pt.macro_stable_uptrend,
    pt.wow_7d_pct
FROM newstrend.cluster_keywords ck
LEFT JOIN newstrend.clusters c
       ON c.week = ck.week AND c.cluster_id = ck.cluster_id
LEFT JOIN newstrend.long_term_signals lt
       ON lt.week = ck.week AND lt.keyword = ck.keyword
LEFT JOIN newstrend.period_trend_signals pt
       ON pt.as_of_week = ck.week AND pt.keyword = ck.keyword;

-- [군집 단위] 각 군집: 테마·대표어 + 부피/강도 + 해석 + 멤버 키워드 신호 롤업
CREATE OR REPLACE VIEW newstrend.v_cluster_enriched AS
SELECT
    c.week,
    c.cluster_id,
    c.cluster_theme,
    c.representative_terms,
    c.keyword_count,
    c.avg_z_score,
    c.max_z_score,
    t.cluster_volume,
    t.cluster_intensity,
    t.is_active,
    ci.final_interpretation,
    rk.avg_long_term_score,
    rk.max_long_term_score,
    rk.tracked_keyword_count,
    rk.member_keywords
FROM newstrend.clusters c
LEFT JOIN newstrend.trend_ts_cluster t
       ON t.week = c.week AND t.cluster_id = c.cluster_id
LEFT JOIN newstrend.cluster_interpretation ci
       ON ci.week = c.week AND ci.cluster_id = c.cluster_id
LEFT JOIN LATERAL (
    SELECT
        avg(lt.long_term_score)                                   AS avg_long_term_score,
        max(lt.long_term_score)                                   AS max_long_term_score,
        count(*) FILTER (WHERE lt.selected_for_tracking)          AS tracked_keyword_count,
        array_agg(ck.keyword ORDER BY ck.z_score DESC NULLS LAST) AS member_keywords
    FROM newstrend.cluster_keywords ck
    LEFT JOIN newstrend.long_term_signals lt
           ON lt.week = ck.week AND lt.keyword = ck.keyword
    WHERE ck.week = c.week AND ck.cluster_id = c.cluster_id
) rk ON true;

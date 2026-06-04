-- 5단계 노이즈 라벨 연결: trend_timeseries에 noise_classes 노출 + review 버킷 뷰.
-- 정책: 라벨된 키워드는 anchor 선정에서 제외하되 삭제하지 않고 review 버킷으로 보존.
-- 멱등: ADD COLUMN IF NOT EXISTS, CREATE OR REPLACE.
-- 설계 출처: memory/project_keyword_noise_classifier.md

SET search_path TO newstrend, public;

-- (1) 선정된 트렌드 행에 노이즈 라벨 부착(보통 비어 있음 — 제외되므로. 토글 off/부분 제외 시 의미).
ALTER TABLE newstrend.trend_timeseries
    ADD COLUMN IF NOT EXISTS noise_classes text[];

-- 대시보드 뷰에 noise_classes 노출(0003처럼 끝에 컬럼 추가).
CREATE OR REPLACE VIEW newstrend.v_trend_dashboard AS
SELECT week, keyword, trend_slot_id, group_id, group_score,
       status, z_score AS trend_strength, weekly_summary, evidence_doc_ids,
       delta_z, delta_count, count_ratio, noise_classes
FROM newstrend.trend_timeseries;

-- (2) review 버킷: 고z(≥2.0)지만 노이즈 라벨이 붙어 anchor에서 제외된 후보.
--     keyword_class 와 v_z_score_high 조인 — 추가 연산 없이 주차별 "제외된 후보" 제공.
CREATE OR REPLACE VIEW newstrend.v_trend_review_excluded AS
SELECT z.week,
       z.keyword,
       z.z_score,
       array_agg(DISTINCT kc.class ORDER BY kc.class) AS noise_classes
FROM newstrend.v_z_score_high z
JOIN newstrend.keyword_class kc ON kc.keyword = z.keyword
GROUP BY z.week, z.keyword, z.z_score;

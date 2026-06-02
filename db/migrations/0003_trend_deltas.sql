-- P4: trend_timeseries 에 주차 간 변화 수치 컬럼 추가.
-- (a) delta_z = z_score - 전주 z_score, delta_count = count - 전주 count, count_ratio = count/전주 count.
-- trend_extractor 가 이미 계산하던 값(report_df의 z_score_change/count_change_ratio)을 DB에 영속화한다.
-- 멱등: ADD COLUMN IF NOT EXISTS.

SET search_path TO newstrend, public;

ALTER TABLE newstrend.trend_timeseries
    ADD COLUMN IF NOT EXISTS delta_z      double precision,
    ADD COLUMN IF NOT EXISTS delta_count  integer,
    ADD COLUMN IF NOT EXISTS count_ratio  double precision;

-- dashboard 뷰에도 노출. CREATE OR REPLACE VIEW 는 기존 컬럼 순서를 유지하고
-- 새 컬럼은 끝에만 추가 가능하므로(중간 삽입 불가), delta_* 는 맨 뒤에 붙인다.
CREATE OR REPLACE VIEW newstrend.v_trend_dashboard AS
SELECT week, keyword, trend_slot_id, group_id, group_score,
       status, z_score AS trend_strength, weekly_summary, evidence_doc_ids,
       delta_z, delta_count, count_ratio
FROM newstrend.trend_timeseries;

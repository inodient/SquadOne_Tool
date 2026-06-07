-- 사용자 키워드 제외 목록 — 분석에서 영구 제외(앞으로 모든 단계 재실행 시 반영)
-- serial 0010. 5단계 자동 노이즈분류(keyword_class)와 별개의 '수동 제외' 목록.
CREATE SCHEMA IF NOT EXISTS newstrend;
SET search_path TO newstrend, public;

CREATE TABLE IF NOT EXISTS newstrend.keyword_exclusions (
    keyword    text        NOT NULL,
    reason     text,                          -- 'manual' 등
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (keyword)
);

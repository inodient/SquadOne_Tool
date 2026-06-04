-- 키워드 노이즈 분류 레이어 — keyword_class.
-- z>=2.0 트렌드 후보에 섞이는 노이즈를 유형별로 라벨링한다(제거가 아니라 라벨/플래그).
-- 1차: seasonal(연도간 동일 ISO주차 재현). 후속: person/politics/buzzword.
-- 멀티라벨 허용 위해 PK=(keyword, class). 멱등: IF NOT EXISTS.
-- 설계 출처: memory/project_keyword_noise_classifier.md

SET search_path TO newstrend, public;

CREATE TABLE IF NOT EXISTS newstrend.keyword_class (
    keyword    text        NOT NULL,
    class      text        NOT NULL,          -- 'seasonal' | 'person' | 'politics' | 'buzzword'
    score      double precision,              -- 분류 신뢰도(유형별 의미 상이)
    method     text        NOT NULL,          -- 'yoy_recurrence' 등 산출 방법
    detail     jsonb,                         -- 근거(예: {seas_yrs, ratio, best_wk})
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (keyword, class)
);
CREATE INDEX IF NOT EXISTS idx_keyword_class_class ON newstrend.keyword_class (class);

-- 계절어 라벨 뷰(review 대상). 정책상 자동 제거 아님 — 후보 선정 시 플래그로만 노출.
CREATE OR REPLACE VIEW newstrend.v_keyword_class_seasonal AS
SELECT keyword, score, method, detail, updated_at
FROM newstrend.keyword_class
WHERE class = 'seasonal';

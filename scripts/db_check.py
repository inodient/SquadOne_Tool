"""DB 적재·마이그레이션 점검 스크립트.

사용:
    python -m scripts.db_check          # 객체/행수/주차 커버리지 요약
    python -m scripts.db_check --full   # 정합성 검사 포함

부작용 없음(읽기 전용). 접속은 db.config(프로젝트 .env)의 POSTGRES_* 사용.
"""

from __future__ import annotations

import sys

from db.engine import get_conn

SCHEMA = "newstrend"

TABLES = [
    "weekly_keywords", "weekly_keyword_freq", "base_calculation", "z_score_keywords",
    "keysentence", "trend_timeseries", "trend_contexts", "trend_groups",
    "product_candidates", "reports", "pipeline_runs",
]
VIEWS = ["v_z_score_high", "v_trend_dashboard"]
# (테이블, 주차컬럼) — 커버리지 표시용
WEEK_TABLES = [
    "weekly_keywords", "weekly_keyword_freq", "base_calculation", "z_score_keywords",
    "keysentence", "trend_timeseries", "product_candidates",
]


def _scalar(cur, sql, params=None):
    cur.execute(sql, params or ())
    r = cur.fetchone()
    return r[0] if r else None


def main() -> int:
    full = "--full" in sys.argv
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1) 객체 존재
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=%s", (SCHEMA,)
            )
            present = {r[0] for r in cur.fetchall()}
            print(f"\n=== [1] newstrend 객체 ({len(present)}개) ===")
            for t in TABLES + VIEWS:
                mark = "✓" if t in present else "✗ 없음"
                print(f"  {mark:6} {t}")

            # 2) 테이블 행수
            print("\n=== [2] 테이블 행수 ===")
            counts = {}
            for t in TABLES:
                if t not in present:
                    continue
                n = _scalar(cur, f"SELECT count(*) FROM {SCHEMA}.{t}")
                counts[t] = n
                print(f"  {t:24} {n:>14,}")

            # 3) 주차 커버리지
            print("\n=== [3] 주차 커버리지 (min ~ max | distinct) ===")
            for t in WEEK_TABLES:
                if t not in present or not counts.get(t):
                    print(f"  {t:24} (비어있음)")
                    continue
                mn = _scalar(cur, f"SELECT min(week) FROM {SCHEMA}.{t}")
                mx = _scalar(cur, f"SELECT max(week) FROM {SCHEMA}.{t}")
                dc = _scalar(cur, f"SELECT count(DISTINCT week) FROM {SCHEMA}.{t}")
                print(f"  {t:24} {mn} ~ {mx} | {dc}주")

            if not full:
                print("\n(정합성 검사는 --full 옵션)")
                return 0

            # 4) 정합성 검사
            print("\n=== [4] 정합성 검사 ===")
            ok = True

            # 4-1) freq 합계 ↔ weekly 집계 (표본 1주)
            wk = _scalar(cur, f"SELECT max(week) FROM {SCHEMA}.weekly_keyword_freq")
            if wk:
                a = _scalar(cur, f"SELECT sum(count) FROM {SCHEMA}.weekly_keyword_freq WHERE week=%s", (wk,))
                b = _scalar(cur, f"SELECT sum(count) FROM {SCHEMA}.weekly_keywords WHERE week=%s", (wk,))
                same = (a == b)
                ok &= same
                print(f"  freq↔weekly 합계 (week={wk}): freq={a} vs weekly={b} -> {'OK' if same else '⚠️ 불일치'}")

            # 4-2) z_score 주차 ⊆ weekly 주차
            orphan = _scalar(
                cur,
                f"SELECT count(*) FROM (SELECT DISTINCT week FROM {SCHEMA}.z_score_keywords "
                f"EXCEPT SELECT DISTINCT week FROM {SCHEMA}.weekly_keywords) s",
            )
            ok &= (orphan == 0)
            print(f"  z_score 주차 ⊄ weekly: {orphan}건 -> {'OK' if orphan == 0 else '⚠️'}")

            # 4-3) trend evidence_doc_ids 채움 비율
            if counts.get("trend_timeseries"):
                filled = _scalar(
                    cur,
                    f"SELECT count(*) FROM {SCHEMA}.trend_timeseries WHERE array_length(evidence_doc_ids,1)>0",
                )
                total = counts["trend_timeseries"]
                pct = (filled / total * 100) if total else 0
                print(f"  trend evidence_doc_ids 채움: {filled}/{total} ({pct:.1f}%)")

            # 4-4) keysentence evidence 채움 비율
            if counts.get("keysentence"):
                kfilled = _scalar(
                    cur,
                    f"SELECT count(*) FROM {SCHEMA}.keysentence WHERE array_length(evidence_doc_ids,1)>0",
                )
                ktotal = counts["keysentence"]
                print(f"  keysentence evidence 채움: {kfilled}/{ktotal} ({(kfilled/ktotal*100) if ktotal else 0:.1f}%)")

            print(f"\n=== 정합성 종합: {'✅ 통과' if ok else '⚠️ 확인 필요'} ===")
            return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())

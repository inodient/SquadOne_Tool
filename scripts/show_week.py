"""특정 주차의 트렌드/키센텐스/상품을 조회한다(읽기 전용, 확인용).

사용:
    python -m scripts.show_week 2023-W13
    python -m scripts.show_week 2023-W13 --top 20
"""

from __future__ import annotations

import argparse

from db.engine import get_conn

SCHEMA = "newstrend"


def main() -> int:
    ap = argparse.ArgumentParser(description="주차 트렌드 조회(확인용)")
    ap.add_argument("week", help="ISO 주차 라벨, 예: 2023-W13")
    ap.add_argument("--top", type=int, default=10, help="상위 N개(기본 10)")
    args = ap.parse_args()
    week, top = args.week, args.top

    with get_conn() as conn:
        with conn.cursor() as cur:
            def q(sql, params):
                cur.execute(sql, params)
                return cur.fetchall()

            print(f"\n========== 주차 {week} ==========")

            # 트렌드 시계열 상위
            rows = q(
                f"SELECT keyword, status, round(z_score::numeric,2), count, group_id, "
                f"       COALESCE(array_length(evidence_doc_ids,1),0) "
                f"FROM {SCHEMA}.trend_timeseries WHERE week=%s "
                f"ORDER BY z_score DESC NULLS LAST LIMIT %s",
                (week, top),
            )
            print(f"\n[트렌드 상위 {top}] keyword | status | z | count | group | #evidence")
            if not rows:
                print("  (trend_timeseries 데이터 없음)")
            for kw, st, z, cnt, gid, nev in rows:
                print(f"  {str(kw)[:24]:24} | {str(st or ''):9} | {z} | {cnt} | {gid or '-'} | {nev}")

            # 그룹
            grp = q(
                f"SELECT group_id, round(group_score::numeric,2), round(cohesion::numeric,2), "
                f"       members->>'group_summary' FROM {SCHEMA}.trend_groups WHERE week=%s "
                f"ORDER BY group_score DESC NULLS LAST LIMIT %s",
                (week, top),
            )
            print(f"\n[그룹] group_id | score | cohesion | summary")
            if not grp:
                print("  (trend_groups 데이터 없음)")
            for gid, gs, coh, summ in grp:
                print(f"  {str(gid)[:16]:16} | {gs} | {coh} | {str(summ or '')[:60]}")

            # keysentence
            ks = q(
                f"SELECT keyword, COALESCE(array_length(evidence_doc_ids,1),0), query_text "
                f"FROM {SCHEMA}.keysentence WHERE week=%s ORDER BY evidence_count DESC NULLS LAST LIMIT %s",
                (week, top),
            )
            print(f"\n[keysentence] keyword | #evidence | query_text")
            if not ks:
                print("  (keysentence 데이터 없음)")
            for kw, nev, qt in ks:
                print(f"  {str(kw)[:20]:20} | {nev} | {str(qt or '')[:60]}")

            # 상품 후보
            pc = q(
                f"SELECT rank, product_name, selection_reason FROM {SCHEMA}.product_candidates "
                f"WHERE week=%s ORDER BY rank LIMIT %s",
                (week, top),
            )
            print(f"\n[상품 후보] rank | product_name | reason")
            if not pc:
                print("  (product_candidates 데이터 없음)")
            for rk, pn, rsn in pc:
                print(f"  {rk:>2} | {str(pn or '')[:24]:24} | {str(rsn or '')[:60]}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

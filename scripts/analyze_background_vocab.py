"""배경 어휘 게이트 임계값 탐색용 1회성 분석 스크립트 (서버측 집계).

DB newstrend.weekly_keyword_freq(이미 적재됨)에 대해 집계를 Postgres에서 수행하고
작은 결과만 받아온다(전체 테이블을 pandas로 끌어오지 않음).
"""
from __future__ import annotations

from db.engine import get_conn


def q(cur, sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchall()


def main() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            (total_weeks,) = q(cur, "SELECT count(DISTINCT week) FROM weekly_keyword_freq")[0]
            (n_keywords,) = q(cur, "SELECT count(DISTINCT keyword) FROM weekly_keyword_freq")[0]
            (total_count,) = q(cur, "SELECT coalesce(sum(count),0) FROM weekly_keyword_freq")[0]

            print("=== 코퍼스 개요 ===")
            print(f"전체 주차 수   : {total_weeks}")
            print(f"고유 키워드 수 : {n_keywords:,}")
            print(f"전체 count 합  : {total_count:,}")
            print()

            # 키워드별 집계 임시뷰(CTE)를 재사용하기 위한 베이스 SQL
            base = """
                WITH agg AS (
                    SELECT keyword,
                           count(DISTINCT week)::float / %(tw)s AS wpr,
                           sum(count) AS tc
                    FROM weekly_keyword_freq
                    GROUP BY keyword
                )
            """
            # 주차 점유율 임계별 제거 규모
            print("=== 주차점유율(week_presence_ratio) 임계별 제거 규모 ===")
            for thr in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0):
                rows = q(cur, base + "SELECT count(*) FROM agg WHERE wpr > %(thr)s",
                         {"tw": total_weeks, "thr": thr})
                dropped = rows[0][0]
                print(f"  wpr > {thr:>4}: 제거 {dropped:>7,}개 ({dropped / n_keywords * 100:5.2f}%)")
            print()

            # 주차점유율 상위 40 (배경어 후보)
            print("=== 주차점유율 상위 40 (배경어 후보) ===")
            print(f"{'keyword':<14}{'wpr':>8}{'total_count':>14}{'freq_share':>12}")
            rows = q(cur, base + """
                SELECT keyword, wpr, tc, tc::float / %(tot)s
                FROM agg ORDER BY wpr DESC, tc DESC LIMIT 40
            """, {"tw": total_weeks, "tot": total_count})
            for kw, wpr, tc, fs in rows:
                print(f"{kw:<14}{wpr:>8.4f}{tc:>14,}{fs:>12.5f}")
            print()

            # 예시 키워드 진단
            print("=== 예시 키워드 진단 ===")
            examples = ["명칭", "정치", "대통령", "기자", "사람", "문제", "관계", "지역", "사업", "회사", "경제"]
            rows = q(cur, base + """
                SELECT keyword, wpr, tc, tc::float / %(tot)s
                FROM agg WHERE keyword = ANY(%(kws)s)
            """, {"tw": total_weeks, "tot": total_count, "kws": examples})
            found = {r[0]: r for r in rows}
            for kw in examples:
                if kw in found:
                    _, wpr, tc, fs = found[kw]
                    tag = "제거됨" if wpr > 0.7 else "통과(샘)"
                    print(f"  {kw:<6} wpr={wpr:.3f} total={tc:>9,} freq_share={fs:.5f} | 현재(0.7) {tag}")
                else:
                    print(f"  {kw:<6} (집계에 없음 — min_df 등에서 이미 탈락/미등장)")
            print()

            # freq_share 상위 40 (총량 기준 흔한 단어)
            print("=== 총빈도 점유율(freq_share) 상위 40 ===")
            print(f"{'keyword':<14}{'wpr':>8}{'total_count':>14}{'freq_share':>12}")
            rows = q(cur, base + """
                SELECT keyword, wpr, tc, tc::float / %(tot)s
                FROM agg ORDER BY tc DESC LIMIT 40
            """, {"tw": total_weeks, "tot": total_count})
            for kw, wpr, tc, fs in rows:
                print(f"{kw:<14}{wpr:>8.4f}{tc:>14,}{fs:>12.5f}")


if __name__ == "__main__":
    main()

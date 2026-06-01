"""기존 data/output/*.csv → newstrend 테이블 1회 이관(부트스트랩).

P0 확정 전략: 엑셀 기반 기존 산출물을 빠르게 적재해 DB 경로를 실데이터로 검증.
권위 기준은 추후 P1+ 파이프라인(Qdrant 증분)이 가져간다.

사용:
    python -m scripts.backfill_csv_to_pg                  # weekly + zscore (기본)
    python -m scripts.backfill_csv_to_pg --tables weekly,zscore,base
    python -m scripts.backfill_csv_to_pg --tables base    # base 만(대용량, 선택)

매핑:
  - weekly_keywords.csv (long: week,keyword,source,count)        -> weekly_keywords  [직접 COPY]
  - z_score_keywords.csv (long: week,keyword,z_score,sources)    -> z_score_keywords [직접 COPY] (_group 제외)
  - base_calculation.csv (wide: keyword, tfidf_/mean_/std_<week>) -> base_calculation [melt 후 적재]
       * tfidf 가 0/NaN 인 (week,keyword)는 적재 생략(희소화) — dense 170M행 폭증 방지.

멱등: 각 테이블 적재 전 TRUNCATE.
실행 후 mv_weekly_keyword_freq 를 REFRESH.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from db.engine import get_conn

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "output"


def _truncate(cur, table: str) -> None:
    cur.execute(f"TRUNCATE newstrend.{table}")


def backfill_weekly(conn) -> int:
    src = OUTPUT_DIR / "weekly_keywords.csv"
    print(f"[weekly] COPY {src}")
    with conn.cursor() as cur:
        _truncate(cur, "weekly_keywords")
        with open(src, "r", encoding="utf-8-sig") as f:
            with cur.copy(
                "COPY newstrend.weekly_keywords (week, keyword, source, count) "
                "FROM STDIN WITH (FORMAT csv, HEADER true)"
            ) as cp:
                while chunk := f.read(1 << 20):
                    cp.write(chunk)
        cur.execute("SELECT count(*) FROM newstrend.weekly_keywords")
        n = int(cur.fetchone()[0])
    conn.commit()
    print(f"[weekly] 적재 완료: {n:,} 행")
    return n


def backfill_zscore(conn) -> int:
    src = OUTPUT_DIR / "z_score_keywords.csv"
    print(f"[zscore] COPY {src}")
    with conn.cursor() as cur:
        _truncate(cur, "z_score_keywords")
        with open(src, "r", encoding="utf-8-sig") as f:
            with cur.copy(
                "COPY newstrend.z_score_keywords (week, keyword, z_score, sources) "
                "FROM STDIN WITH (FORMAT csv, HEADER true)"
            ) as cp:
                while chunk := f.read(1 << 20):
                    cp.write(chunk)
        cur.execute("SELECT count(*) FROM newstrend.z_score_keywords")
        n = int(cur.fetchone()[0])
    conn.commit()
    print(f"[zscore] 적재 완료: {n:,} 행")
    return n


def backfill_base(conn, *, chunk_rows: int = 20000) -> int:
    """wide base_calculation.csv 를 melt 하여 long 적재(tfidf 0/NaN 제외)."""
    src = OUTPUT_DIR / "base_calculation.csv"
    print(f"[base] melt+적재 {src} (chunk={chunk_rows})")
    total = 0
    with conn.cursor() as cur:
        _truncate(cur, "base_calculation")
        reader = pd.read_csv(src, encoding="utf-8-sig", chunksize=chunk_rows)
        for ci, chunk in enumerate(reader):
            tfidf_cols = [c for c in chunk.columns if c.startswith("tfidf_")]
            weeks = [c[len("tfidf_"):] for c in tfidf_cols]
            rows: list[tuple] = []
            for _, r in chunk.iterrows():
                kw = str(r["keyword"])
                for w in weeks:
                    tv = r.get(f"tfidf_{w}")
                    if pd.isna(tv) or float(tv) == 0.0:
                        continue  # 희소화: 실제 등장(비0)만 저장
                    mv = r.get(f"mean_{w}")
                    sv = r.get(f"std_{w}")
                    rows.append((
                        w, kw, float(tv),
                        None if pd.isna(mv) else float(mv),
                        None if pd.isna(sv) else float(sv),
                    ))
            if rows:
                with cur.copy(
                    "COPY newstrend.base_calculation (week, keyword, tfidf, base_mean, base_std) "
                    "FROM STDIN"
                ) as cp:
                    for row in rows:
                        cp.write_row(row)
                total += len(rows)
            print(f"[base] chunk {ci}: 누적 {total:,} 행", end="\r")
    conn.commit()
    print(f"\n[base] 적재 완료: {total:,} 행")
    return total


def refresh_mv(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("REFRESH MATERIALIZED VIEW newstrend.mv_weekly_keyword_freq")
    conn.commit()
    print("[mv] mv_weekly_keyword_freq REFRESH 완료")


def main() -> None:
    parser = argparse.ArgumentParser(description="CSV → newstrend 백필(이관)")
    parser.add_argument("--tables", default="weekly,zscore",
                        help="적재 대상(쉼표): weekly,zscore,base (기본: weekly,zscore)")
    args = parser.parse_args()
    targets = {t.strip() for t in args.tables.split(",") if t.strip()}

    with get_conn() as conn:
        if "weekly" in targets:
            backfill_weekly(conn)
            refresh_mv(conn)
        if "zscore" in targets:
            backfill_zscore(conn)
        if "base" in targets:
            backfill_base(conn)
    print("[backfill] 완료:", ", ".join(sorted(targets)))


if __name__ == "__main__":
    sys.exit(main())

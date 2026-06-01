"""newstrend 스키마 Repository.

P0 범위: 시그니처 정의 + weekly_keywords upsert 실동작.
나머지 write_*/read_* 는 P1~P3에서 구현(현재 NotImplementedError stub).

대량 적재(수천만 행)는 scripts/backfill_csv_to_pg.py 의 COPY 경로를 사용한다.
여기 upsert_weekly_keywords 는 증분(주차 단위) 갱신용이다.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

from db.engine import get_conn

WeeklyRow = Tuple[str, str, str, int]  # (week, keyword, source, count)


def upsert_weekly_keywords(rows: Iterable[WeeklyRow], *, batch_size: int = 5000) -> int:
    """weekly_keywords 증분 upsert. (week, keyword, source) 충돌 시 count 갱신.

    반환: 처리한 행 수.
    """
    rows = list(rows)
    if not rows:
        return 0
    sql = (
        "INSERT INTO newstrend.weekly_keywords (week, keyword, source, count) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (week, keyword, source) "
        "DO UPDATE SET count = EXCLUDED.count, updated_at = now()"
    )
    processed = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for i in range(0, len(rows), batch_size):
                chunk = rows[i : i + batch_size]
                cur.executemany(sql, chunk)
                processed += len(chunk)
        conn.commit()
    return processed


def refresh_weekly_freq(*, concurrently: bool = False) -> None:
    """mv_weekly_keyword_freq 갱신. 최초 1회는 concurrently=False(데이터 없음)."""
    mode = "CONCURRENTLY " if concurrently else ""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"REFRESH MATERIALIZED VIEW {mode}newstrend.mv_weekly_keyword_freq")
        conn.commit()


def read_weekly_freq(weeks: Sequence[str] | None = None) -> List[Dict[str, Any]]:
    """주차×키워드 빈도 조회(mv). weeks 미지정 시 전체."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            if weeks:
                cur.execute(
                    "SELECT week, keyword, count FROM newstrend.mv_weekly_keyword_freq "
                    "WHERE week = ANY(%s)",
                    (list(weeks),),
                )
            else:
                cur.execute("SELECT week, keyword, count FROM newstrend.mv_weekly_keyword_freq")
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def table_count(table: str) -> int:
    """행 수(검증용). table 은 newstrend 내 객체명만 허용."""
    if not table.replace("_", "").isalnum():
        raise ValueError(f"허용되지 않는 테이블명: {table}")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM newstrend.{table}")
            return int(cur.fetchone()[0])


# ── P1~P3에서 구현 예정 (stub) ──────────────────────────────────

def write_base(df) -> int:  # noqa: ANN001
    raise NotImplementedError("P1: base_calculation long 적재")


def write_zscore(df) -> int:  # noqa: ANN001
    raise NotImplementedError("P1: z_score_keywords 적재")


def write_keysentence(df) -> int:  # noqa: ANN001
    raise NotImplementedError("P2: keysentence 적재")


def write_trend(df) -> int:  # noqa: ANN001
    raise NotImplementedError("P2: trend_timeseries/contexts/groups 적재")


def write_product(df) -> int:  # noqa: ANN001
    raise NotImplementedError("P3: product_candidates 적재")


def write_report(step: str, week: str, payload: Dict[str, Any], meta: Dict[str, Any] | None = None) -> None:
    raise NotImplementedError("P3: reports(jsonb) 적재")

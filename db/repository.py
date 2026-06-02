"""newstrend 스키마 Repository (P1).

정형 단계(1~4)의 DB I/O를 담당한다.
- 1단계: upsert_weekly_keywords (증분)
- 2단계: replace_weekly_keyword_freq (주차 단위 증분)
- 3단계: read_weekly_freq / write_base (전체 재계산)
- 4단계: read_base / write_zscore (전체 재계산)

대량 적재는 psycopg3 COPY 사용.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

import pandas as pd

from db.engine import get_conn

WeeklyRow = Tuple[str, str, str, int]  # (week, keyword, source, count)


# ── 1단계 ────────────────────────────────────────────────────────

def upsert_weekly_keywords(rows: Iterable[WeeklyRow], *, batch_size: int = 5000) -> int:
    """weekly_keywords 증분 upsert. (week, keyword, source) 충돌 시 count 갱신."""
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


# ── 2단계 (증분) ─────────────────────────────────────────────────

def recompute_weekly_keyword_freq(weeks: Sequence[str]) -> int:
    """주어진 주차들의 빈도 집계를 DB 내에서 재계산(증분). 순수 SQL(데이터 왕복 없음).

    해당 주차만 DELETE 후 weekly_keywords 집계로 INSERT. 반환: 영향 주차 수.
    """
    weeks = list(dict.fromkeys(weeks))
    if not weeks:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM newstrend.weekly_keyword_freq WHERE week = ANY(%s)", (weeks,)
            )
            cur.execute(
                "INSERT INTO newstrend.weekly_keyword_freq (week, keyword, count) "
                "SELECT week, keyword, sum(count)::int "
                "FROM newstrend.weekly_keywords WHERE week = ANY(%s) "
                "GROUP BY week, keyword",
                (weeks,),
            )
        conn.commit()
    return len(weeks)


# ── 3단계 ────────────────────────────────────────────────────────

def read_weekly_freq() -> pd.DataFrame:
    """weekly_keyword_freq 전체를 long DataFrame(week, keyword, count)으로 반환."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT week, keyword, count FROM newstrend.weekly_keyword_freq")
            data = cur.fetchall()
    return pd.DataFrame(data, columns=["week", "keyword", "count"])


def write_base(df: pd.DataFrame, *, truncate: bool = True) -> int:
    """base_calculation(long) 적재. df 컬럼: week, keyword, tfidf, base_mean, base_std.

    전체 재계산이므로 기본 TRUNCATE 후 COPY.
    """
    cols = ["week", "keyword", "tfidf", "base_mean", "base_std"]
    sub = df[cols]
    n = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            if truncate:
                cur.execute("TRUNCATE newstrend.base_calculation")
            with cur.copy(
                "COPY newstrend.base_calculation (week, keyword, tfidf, base_mean, base_std) FROM STDIN"
            ) as cp:
                for row in sub.itertuples(index=False, name=None):
                    week, keyword, tfidf, mean, std = row
                    cp.write_row((
                        week, keyword,
                        None if pd.isna(tfidf) else float(tfidf),
                        None if pd.isna(mean) else float(mean),
                        None if pd.isna(std) else float(std),
                    ))
                    n += 1
        conn.commit()
    return n


# ── 4단계 ────────────────────────────────────────────────────────

def read_weekly_sources() -> pd.DataFrame:
    """(week, keyword)별 source(언론사) 집계: 'a|b|c'. z_score 단계 sources 컬럼용."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT week, keyword, string_agg(DISTINCT source, '|' ORDER BY source) AS sources "
                "FROM newstrend.weekly_keywords GROUP BY week, keyword"
            )
            data = cur.fetchall()
    return pd.DataFrame(data, columns=["week", "keyword", "sources"])


def read_base() -> pd.DataFrame:
    """base_calculation(long) 전체를 DataFrame(week, keyword, tfidf, base_mean, base_std)으로 반환."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT week, keyword, tfidf, base_mean, base_std FROM newstrend.base_calculation"
            )
            data = cur.fetchall()
    return pd.DataFrame(data, columns=["week", "keyword", "tfidf", "base_mean", "base_std"])


def write_zscore(df: pd.DataFrame, *, truncate: bool = True) -> int:
    """z_score_keywords 적재. df 컬럼: week, keyword, z_score, sources. 전체 재계산 → TRUNCATE 후 COPY."""
    cols = ["week", "keyword", "z_score", "sources"]
    sub = df[cols]
    n = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            if truncate:
                cur.execute("TRUNCATE newstrend.z_score_keywords")
            with cur.copy(
                "COPY newstrend.z_score_keywords (week, keyword, z_score, sources) FROM STDIN"
            ) as cp:
                for row in sub.itertuples(index=False, name=None):
                    week, keyword, z, sources = row
                    cp.write_row((
                        week, keyword,
                        0.0 if pd.isna(z) else float(z),
                        None if (sources is None or (isinstance(sources, float) and pd.isna(sources))) else str(sources),
                    ))
                    n += 1
        conn.commit()
    return n


# ── 공통/검증 ────────────────────────────────────────────────────

def table_count(table: str) -> int:
    """행 수(검증용). newstrend 내 객체명만 허용."""
    if not table.replace("_", "").isalnum():
        raise ValueError(f"허용되지 않는 테이블명: {table}")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM newstrend.{table}")
            return int(cur.fetchone()[0])


# ── P2~P3 stub ──────────────────────────────────────────────────

def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x)]
    if isinstance(v, float) and pd.isna(v):
        return []
    s = str(v).strip()
    return [p for p in s.split("|") if p] if s else []


def write_keysentence(df: pd.DataFrame, weeks: Sequence[str]) -> int:
    """keysentence 적재(주차 단위 replace). evidence_doc_ids 는 text[]."""
    weeks = list(dict.fromkeys(weeks))
    sql = (
        "INSERT INTO newstrend.keysentence "
        "(week, keyword, query_text, key_sentence, evidence_doc_ids, evidence_count) "
        "VALUES (%s, %s, %s, %s, %s, %s)"
    )
    cols = ["week", "keyword", "query_text", "key_sentence", "evidence_doc_ids", "evidence_count"]
    rows = [
        (
            str(week), str(keyword),
            None if (qt is None or (isinstance(qt, float) and pd.isna(qt))) else str(qt),
            None if (ks is None or (isinstance(ks, float) and pd.isna(ks))) else str(ks),
            _as_list(ev),
            int(ec) if not (ec is None or (isinstance(ec, float) and pd.isna(ec))) else 0,
        )
        for week, keyword, qt, ks, ev, ec in df[cols].itertuples(index=False, name=None)
    ]
    with get_conn() as conn:
        with conn.cursor() as cur:
            if weeks:
                cur.execute("DELETE FROM newstrend.keysentence WHERE week = ANY(%s)", (weeks,))
            if rows:
                cur.executemany(sql, rows)
        conn.commit()
    return len(rows)


def write_trend(
    timeseries_df: pd.DataFrame,
    contexts_df: pd.DataFrame,
    groups_df: pd.DataFrame,
    weeks: Sequence[str],
) -> Dict[str, int]:
    """trend_timeseries/contexts/groups 적재(주차 단위 replace). members 는 jsonb."""
    from psycopg.types.json import Json

    weeks = list(dict.fromkeys(weeks))
    ts_sql = (
        "INSERT INTO newstrend.trend_timeseries "
        "(week, keyword, trend_slot_id, group_id, group_score, status, status_reason, "
        " z_score, count, weekly_summary, evidence_doc_ids) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    )
    cx_sql = (
        "INSERT INTO newstrend.trend_contexts (week, keyword, doc_id, score, snippet) "
        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (week, keyword, doc_id) DO NOTHING"
    )
    gp_sql = (
        "INSERT INTO newstrend.trend_groups (week, group_id, members, group_score, cohesion) "
        "VALUES (%s,%s,%s,%s,%s)"
    )

    def _f(v):
        return None if (v is None or (isinstance(v, float) and pd.isna(v))) else float(v)

    def _s(v):
        return None if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v)

    def _i(v):
        return 0 if (v is None or (isinstance(v, float) and pd.isna(v))) else int(v)

    ts_cols = ["week", "keyword", "trend_slot_id", "group_id", "group_score", "status",
               "status_reason", "z_score", "count", "weekly_summary", "evidence_doc_ids"]
    ts_rows = [
        (str(w), str(kw), _s(slot), _s(gid), _f(gs), _s(st), _s(sr), _f(z), _i(cnt), _s(ws), _as_list(ev))
        for w, kw, slot, gid, gs, st, sr, z, cnt, ws, ev in timeseries_df[ts_cols].itertuples(index=False, name=None)
    ]
    cx_cols = ["week", "keyword", "doc_id", "score", "snippet"]
    cx_rows = [
        (str(w), str(kw), str(did), _f(sc), (None if (sn is None or (isinstance(sn, float) and pd.isna(sn))) else str(sn)[:2000]))
        for w, kw, did, sc, sn in contexts_df[cx_cols].itertuples(index=False, name=None) if str(did or "")
    ]
    gp_cols = ["week", "group_id", "members", "group_score", "cohesion"]
    gp_rows = [
        (str(w), str(gid), Json(mem if isinstance(mem, (list, dict)) else []), _f(gs), _f(coh))
        for w, gid, mem, gs, coh in groups_df[gp_cols].itertuples(index=False, name=None)
    ]

    with get_conn() as conn:
        with conn.cursor() as cur:
            if weeks:
                cur.execute("DELETE FROM newstrend.trend_timeseries WHERE week = ANY(%s)", (weeks,))
                cur.execute("DELETE FROM newstrend.trend_contexts WHERE week = ANY(%s)", (weeks,))
                cur.execute("DELETE FROM newstrend.trend_groups WHERE week = ANY(%s)", (weeks,))
            if ts_rows:
                cur.executemany(ts_sql, ts_rows)
            if cx_rows:
                cur.executemany(cx_sql, cx_rows)
            if gp_rows:
                cur.executemany(gp_sql, gp_rows)
        conn.commit()
    return {"timeseries": len(ts_rows), "contexts": len(cx_rows), "groups": len(gp_rows)}


def write_product(df: pd.DataFrame, weeks: Sequence[str]) -> int:
    """product_candidates 적재(주차 단위 replace). df: week, rank, product_name, selection_reason."""
    weeks = list(dict.fromkeys(weeks))
    cols = ["week", "rank", "product_name", "selection_reason"]
    sql = (
        "INSERT INTO newstrend.product_candidates (week, rank, product_name, selection_reason) "
        "VALUES (%s, %s, %s, %s)"
    )
    rows = [
        (str(w), int(rank), (None if pd.isna(pn) else str(pn)), (None if pd.isna(sr) else str(sr)))
        for w, rank, pn, sr in df[cols].itertuples(index=False, name=None)
    ]
    with get_conn() as conn:
        with conn.cursor() as cur:
            if weeks:
                cur.execute("DELETE FROM newstrend.product_candidates WHERE week = ANY(%s)", (weeks,))
            if rows:
                cur.executemany(sql, rows)
        conn.commit()
    return len(rows)


def write_reports(rows: Iterable[Tuple[str, str, Dict[str, Any], Dict[str, Any] | None]]) -> int:
    """reports(jsonb) upsert. rows: (step, week, payload, meta). PK(step, week)."""
    from psycopg.types.json import Json

    rows = list(rows)
    if not rows:
        return 0
    sql = (
        "INSERT INTO newstrend.reports (step, week, payload, meta) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (step, week) DO UPDATE SET payload = EXCLUDED.payload, meta = EXCLUDED.meta, created_at = now()"
    )
    params = [
        (str(step), str(week), Json(payload or {}), Json(meta) if meta is not None else None)
        for step, week, payload, meta in rows
    ]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, params)
        conn.commit()
    return len(params)


def write_report(step: str, week: str, payload: Dict[str, Any], meta: Dict[str, Any] | None = None) -> None:
    write_reports([(step, week, payload, meta)])

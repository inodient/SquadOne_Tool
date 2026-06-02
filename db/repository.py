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


def read_distinct_weeks() -> List[str]:
    """weekly_keywords 에 적재된 distinct week 목록(정렬). 2단계 단독 실행 시 CSV 의존 제거용."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT week FROM newstrend.weekly_keywords ORDER BY week")
            return [str(r[0]) for r in cur.fetchall()]


# ── 3단계 ────────────────────────────────────────────────────────

def _week_range_clause(start_week: str | None, end_week: str | None) -> Tuple[str, list]:
    """ISO 주차('YYYY-Www')는 사전식=시간순이라 BETWEEN으로 안전하게 범위 필터.

    둘 다 없으면 빈 절(전체 읽기, 하위호환). 한쪽만 있으면 >=, <= 단방향.
    """
    conds: list[str] = []
    params: list = []
    if start_week:
        conds.append("week >= %s")
        params.append(start_week)
    if end_week:
        conds.append("week <= %s")
        params.append(end_week)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


def read_weekly_freq(start_week: str | None = None, end_week: str | None = None) -> pd.DataFrame:
    """weekly_keyword_freq 를 long DataFrame(week, keyword, count)으로 반환.

    start_week/end_week 지정 시 해당 주차 범위만(단일 주차 trend 실행 고속화). 기본 None=전체.
    """
    where, params = _week_range_clause(start_week, end_week)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT week, keyword, count FROM newstrend.weekly_keyword_freq" + where, params)
            data = cur.fetchall()
    return pd.DataFrame(data, columns=["week", "keyword", "count"])


def write_base(df: pd.DataFrame, *, truncate: bool = True) -> int:
    """base_calculation(long, dense) 적재. df 컬럼: week, keyword, tfidf, base_mean, base_std.

    4단계 EWM 은 dense 시계열(tfidf=0 셀의 rolling mean/std 포함)을 필요로 하므로
    필터 통과 키워드 × 전체 주차의 모든 셀을 저장한다(tfidf!=0 희소화 금지).
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


def read_zscore(start_week: str | None = None, end_week: str | None = None) -> pd.DataFrame:
    """z_score_keywords 를 DataFrame(week, keyword, z_score, sources)으로 반환.

    5+6단계(trend) 단독 실행 시 입력 CSV 대신 DB에서 직접 읽기 위함.
    4단계가 TRUNCATE 후 전량 재기록하므로 in-memory frame과 내용이 동일하다.
    start_week/end_week 지정 시 해당 주차 범위만(단일 주차 trend 실행 고속화). 기본 None=전체.
    """
    where, params = _week_range_clause(start_week, end_week)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT week, keyword, z_score, sources FROM newstrend.z_score_keywords" + where,
                params,
            )
            data = cur.fetchall()
    return pd.DataFrame(data, columns=["week", "keyword", "z_score", "sources"])


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
        " z_score, count, weekly_summary, evidence_doc_ids, delta_z, delta_count, count_ratio) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
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

    # delta_* 컬럼은 trend_extractor report_df에 있으면 사용, 없으면(구 호출) 0으로 채움.
    _ts_df = timeseries_df.copy()
    for _c in ("delta_z", "delta_count", "count_ratio"):
        if _c not in _ts_df.columns:
            _ts_df[_c] = 0
    ts_cols = ["week", "keyword", "trend_slot_id", "group_id", "group_score", "status",
               "status_reason", "z_score", "count", "weekly_summary", "evidence_doc_ids",
               "delta_z", "delta_count", "count_ratio"]
    ts_rows = [
        (str(w), str(kw), _s(slot), _s(gid), _f(gs), _s(st), _s(sr), _f(z), _i(cnt), _s(ws), _as_list(ev),
         _f(dz), _i(dc), _f(cr))
        for w, kw, slot, gid, gs, st, sr, z, cnt, ws, ev, dz, dc, cr
        in _ts_df[ts_cols].itertuples(index=False, name=None)
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


def read_trend_timeseries() -> pd.DataFrame:
    """trend_timeseries 전체를 DataFrame으로 반환.

    7단계(product) 단독 실행 시 입력 CSV(trend_timeseries_report.csv) 대신 DB에서 직접 읽기 위함.
    product 가 사용하는 컬럼(week, keyword, weekly_summary)을 포함한다.
    NOTE: CSV 리포트의 transition_from_prev 는 trend가 항상 빈 값으로 채우므로(미사용 placeholder)
          DB 직접 읽기에서 누락돼도 동작 차이가 없다.
    """
    cols = ["week", "keyword", "trend_slot_id", "group_id", "group_score", "status",
            "status_reason", "z_score", "count", "weekly_summary"]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT week, keyword, trend_slot_id, group_id, group_score, status, "
                "status_reason, z_score, count, weekly_summary "
                "FROM newstrend.trend_timeseries"
            )
            data = cur.fetchall()
    return pd.DataFrame(data, columns=cols)


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


# ── 대시보드 조회 (읽기 전용, 주차 필터/상세/집계) ────────────────
# rest_mcp_server/views.py 의 /v1/view/* 가 소비한다. 모두 list[dict]/dict 반환(JSON 직렬화 용이).

def _rows_to_dicts(cur) -> List[Dict[str, Any]]:
    """커서 결과를 컬럼명 기반 dict 리스트로. cur.description 사용."""
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def latest_week() -> str | None:
    """가장 최근 주차(없으면 None). week 미지정 요청의 폴백."""
    weeks = read_distinct_weeks()
    return weeks[-1] if weeks else None


def read_product_candidates(week: str) -> List[Dict[str, Any]]:
    """해당 주차 상품 추천 후보(rank 오름차순)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT week, rank, product_name, selection_reason "
                "FROM newstrend.product_candidates WHERE week = %s ORDER BY rank",
                (week,),
            )
            return _rows_to_dicts(cur)


def read_reports(week: str, step: str | None = None) -> List[Dict[str, Any]]:
    """해당 주차 LLM 리포트(jsonb payload). step 지정 시 필터."""
    sql = "SELECT step, week, payload, meta, created_at FROM newstrend.reports WHERE week = %s"
    params: list[Any] = [week]
    if step:
        sql += " AND step = %s"
        params.append(step)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = _rows_to_dicts(cur)
    for r in rows:
        if r.get("created_at") is not None:
            r["created_at"] = r["created_at"].isoformat()
    return rows


def read_trends(week: str, status: str | None = None) -> List[Dict[str, Any]]:
    """해당 주차 트렌드 시계열(group_score 내림차순). status 지정 시 필터."""
    sql = (
        "SELECT week, keyword, trend_slot_id, group_id, group_score, status, "
        "status_reason, z_score, count, weekly_summary, evidence_doc_ids, "
        "delta_z, delta_count, count_ratio "
        "FROM newstrend.trend_timeseries WHERE week = %s"
    )
    params: list[Any] = [week]
    if status:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY group_score DESC NULLS LAST, z_score DESC NULLS LAST"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return _rows_to_dicts(cur)


def read_status_counts(week: str) -> Dict[str, int]:
    """해당 주차 status별 트렌드 수(Emerging/Active/Fading/Archived). KPI용."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, count(*) FROM newstrend.trend_timeseries "
                "WHERE week = %s GROUP BY status",
                (week,),
            )
            return {str(s): int(c) for s, c in cur.fetchall()}


def read_keyword_count(week: str) -> int:
    """해당 주차 분석 키워드 수(KPI '분석 뉴스' 대용). weekly_keyword_freq 기준."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM newstrend.weekly_keyword_freq WHERE week = %s",
                (week,),
            )
            return int(cur.fetchone()[0])


def read_trend_detail(week: str, keyword: str) -> Dict[str, Any]:
    """단일 (week, keyword)의 트렌드 + 근거 뉴스(contexts) + 핵심문장(keysentence)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT week, keyword, trend_slot_id, group_id, group_score, status, "
                "status_reason, z_score, count, weekly_summary, evidence_doc_ids, "
                "delta_z, delta_count, count_ratio "
                "FROM newstrend.trend_timeseries WHERE week = %s AND keyword = %s",
                (week, keyword),
            )
            ts_rows = _rows_to_dicts(cur)
            cur.execute(
                "SELECT doc_id, score, snippet FROM newstrend.trend_contexts "
                "WHERE week = %s AND keyword = %s ORDER BY score DESC NULLS LAST",
                (week, keyword),
            )
            contexts = _rows_to_dicts(cur)
            cur.execute(
                "SELECT query_text, key_sentence, evidence_doc_ids, evidence_count "
                "FROM newstrend.keysentence WHERE week = %s AND keyword = %s",
                (week, keyword),
            )
            ks_rows = _rows_to_dicts(cur)
    return {
        "timeseries": ts_rows[0] if ts_rows else None,
        "contexts": contexts,
        "keysentence": ks_rows[0] if ks_rows else None,
    }


def read_trend_groups(week: str) -> List[Dict[str, Any]]:
    """해당 주차 시맨틱 그룹(group_score 내림차순). members 는 jsonb→list/dict."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT week, group_id, members, group_score, cohesion "
                "FROM newstrend.trend_groups WHERE week = %s "
                "ORDER BY group_score DESC NULLS LAST",
                (week,),
            )
            return _rows_to_dicts(cur)


def read_zscore_series(keyword: str, *, limit: int = 200) -> List[Dict[str, Any]]:
    """단일 키워드의 주차별 z-score 추이(week 오름차순). 최근 limit 주차."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT week, z_score FROM newstrend.z_score_keywords "
                "WHERE keyword = %s ORDER BY week DESC LIMIT %s",
                (keyword, limit),
            )
            rows = _rows_to_dicts(cur)
    rows.reverse()  # 오름차순으로 반환(차트 X축)
    return rows


def read_source_distribution(week: str, keyword: str) -> List[Dict[str, Any]]:
    """해당 (week, keyword)의 언론사별 기사 수(내림차순). P3 도넛."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source, sum(count)::int AS count FROM newstrend.weekly_keywords "
                "WHERE week = %s AND keyword = %s GROUP BY source ORDER BY count DESC",
                (week, keyword),
            )
            return _rows_to_dicts(cur)

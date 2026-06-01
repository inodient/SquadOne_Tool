"""Qdrant 뉴스 컬렉션에서 주차 단위로 기사를 전수 수집한다.

1단계 keyword_extractor 입력용(엑셀 대체). payload 계약은 docs/QDRANT_CONTRACT.md 참조.
검증된 주차 필터/페이지네이션 패턴(steps/keysentence_extractor.py::_collect_week_points)을
무제한 페이지네이션(주차 전수)으로 정리한 재사용 모듈. P2에서 5·6 검색 통합 시 확장 예정.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, List
from urllib import request
from urllib.error import HTTPError

import pandas as pd


def _parse_week_to_ts(week_label: str) -> pd.Timestamp:
    m = re.match(r"^(\d{4})-W(\d{2})$", str(week_label).strip())
    if not m:
        raise ValueError(f"Invalid ISO week label: {week_label}")
    year, week = int(m.group(1)), int(m.group(2))
    ts = pd.to_datetime(f"{year}-W{week:02d}-1", format="%G-W%V-%u", errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"Invalid ISO week label: {week_label}")
    return pd.Timestamp(ts)


def weeks_in_range(start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> List[str]:
    """[start, end] 기간에 걸치는 ISO 주차 라벨 목록(YYYY-Www)."""
    if start_ts is None or end_ts is None:
        raise ValueError("weeks_in_range: start_ts/end_ts 모두 필요합니다(증분 주차 범위).")
    cur = pd.Timestamp(start_ts).normalize()
    # 주 시작(월요일)으로 정렬
    cur = cur - pd.Timedelta(days=int(cur.weekday()))
    out: List[str] = []
    seen: set[str] = set()
    while cur <= end_ts:
        iso = cur.isocalendar()
        label = f"{iso.year}-W{int(iso.week):02d}"
        if label not in seen:
            seen.add(label)
            out.append(label)
        cur = cur + pd.Timedelta(days=7)
    return out


def _week_yyyymmdd_days(week: str) -> List[str]:
    start = _parse_week_to_ts(week)
    return [(start + pd.Timedelta(days=i)).strftime("%Y%m%d") for i in range(7)]


def _normalized_yyyymmdd(value: Any) -> str:
    s = re.sub(r"[^0-9]", "", str(value or "").strip())
    return s[:8] if len(s) >= 8 else ""


def _week_server_filter(
    days: List[str], *, date_field: str, date_start_field: str, date_end_field: str,
    include_file_overlap: bool,
) -> Dict[str, Any]:
    date_branch = {"key": date_field, "match": {"any": days}}
    if not include_file_overlap:
        return {"must": [date_branch]}
    ws, we = int(days[0]), int(days[-1])
    file_branch = {"must": [
        {"key": date_start_field, "range": {"lte": we}},
        {"key": date_end_field, "range": {"gte": ws}},
    ]}
    return {"should": [date_branch, file_branch]}


def _payload_hits_week(payload: Dict[str, Any], days: List[str], *,
                       date_field: str, date_start_field: str, date_end_field: str) -> bool:
    ws, we = days[0], days[-1]
    dv = _normalized_yyyymmdd(payload.get(date_field))
    if dv:
        return ws <= dv <= we
    ds = _normalized_yyyymmdd(payload.get(date_start_field))
    de = _normalized_yyyymmdd(payload.get(date_end_field))
    if ds and de:
        return ds <= we and de >= ws
    if ds:
        return ws <= ds <= we
    if de:
        return ws <= de <= we
    return False


def _post_json_safe(url: str, payload: Dict[str, Any], timeout_sec: float, logger: Any) -> Dict[str, Any] | None:
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with request.urlopen(req, timeout=timeout_sec) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        logger.warning("QDRANT_SCROLL_FAIL | http=%s | url=%s | body=%s", getattr(exc, "code", "?"), url, body)
        return None
    except Exception as exc:
        logger.warning("QDRANT_SCROLL_FAIL | url=%s | err=%s", url, exc)
        return None


def iter_week_articles(
    week: str,
    *,
    logger: Any,
    qdrant_url: str,
    collection: str,
    date_field: str = "date",
    date_start_field: str = "file_date_start",
    date_end_field: str = "file_date_end",
    source_field: str = "press",
    body_field: str = "body",
    title_field: str = "title",
    id_field: str = "news_id",
    timeout_sec: float = 30.0,
    page_limit: int = 1000,
    include_file_overlap: bool = True,
    max_pages: int = 100000,
) -> Iterator[Dict[str, Any]]:
    """주차 전수 scroll(무제한 페이지네이션). 각 기사 dict: {news_id, source, title, body}."""
    days = _week_yyyymmdd_days(week)
    qfilter = _week_server_filter(
        days, date_field=date_field, date_start_field=date_start_field,
        date_end_field=date_end_field, include_file_overlap=include_file_overlap,
    )
    url = f"{qdrant_url.rstrip('/')}/collections/{collection}/points/scroll"
    payload: Dict[str, Any] = {"limit": int(max(1, page_limit)), "with_payload": True, "filter": qfilter}
    seen: set[Any] = set()
    pages = 0
    while pages < max_pages:
        pages += 1
        data = _post_json_safe(url, payload, timeout_sec, logger)
        if not data:
            break
        result = data.get("result") or {}
        points = result.get("points") or []
        if not isinstance(points, list) or not points:
            break
        for p in points:
            pid = p.get("id")
            if pid in seen:
                continue
            seen.add(pid)
            pl = p.get("payload") or {}
            if not _payload_hits_week(pl, days, date_field=date_field,
                                      date_start_field=date_start_field, date_end_field=date_end_field):
                continue
            yield {
                "news_id": str(pl.get(id_field) or ""),
                "source": str(pl.get(source_field) or ""),
                "title": str(pl.get(title_field) or ""),
                "body": str(pl.get(body_field) or ""),
            }
        next_off = result.get("next_page_offset")
        if not next_off:
            break
        payload = dict(payload)
        payload["offset"] = next_off

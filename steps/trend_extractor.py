from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Dict, Optional
from urllib import request
from urllib.error import HTTPError

import pandas as pd

from steps.qdrant_embed import embed_query_for_news

from steps.common import (
    ensure_output_dir,
    get_logger,
    load_config,
    log_artifact,
    log_step,
    read_csv,
    write_csv,
    write_dataframe_json_export,
)
from steps.llm_factory import get_llm
from steps.qdrant_search import search_contexts
from steps.keysentence_extractor import _build_keyword_prompt
from db import repository as repo


def _parse_week_to_ts(week_label: str) -> pd.Timestamp:
    m = re.fullmatch(r"(\d{4})-W(\d{2})", str(week_label).strip())
    if not m:
        raise ValueError(f"Invalid week label: {week_label}")
    year = int(m.group(1))
    week = int(m.group(2))
    ts = pd.to_datetime(f"{year}-W{week:02d}-1", format="%G-W%V-%u", errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"Invalid ISO week label: {week_label}")
    return pd.Timestamp(ts)


def _sort_week_labels(values: list[str]) -> list[str]:
    return sorted(values, key=_parse_week_to_ts)


def _normalize_keyword(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", str(text).lower())


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        if pd.isna(out):
            return default
        return out
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        out = int(float(value))
        return out
    except Exception:
        return default


def _week_in_coverage(week: str, start_week: str, end_week: str) -> bool:
    p = _parse_week_to_ts(week)
    return _parse_week_to_ts(start_week) <= p <= _parse_week_to_ts(end_week)


def _week_to_date_range_yyyymmdd(week: str) -> tuple[str, str]:
    start_ts = _parse_week_to_ts(week)
    end_ts = start_ts + pd.Timedelta(days=6)
    return start_ts.strftime("%Y%m%d"), end_ts.strftime("%Y%m%d")


def _normalized_yyyymmdd(value: Any) -> str:
    s = re.sub(r"[^0-9]", "", str(value or "").strip())
    if len(s) >= 8:
        return s[:8]
    return ""


def _payload_hits_week_range(payload: dict[str, Any], *, week_start: str, week_end: str, date_field: str, date_start_field: str, date_end_field: str) -> bool:
    date_val = _normalized_yyyymmdd(payload.get(date_field))
    if date_val:
        return week_start <= date_val <= week_end
    ds = _normalized_yyyymmdd(payload.get(date_start_field))
    de = _normalized_yyyymmdd(payload.get(date_end_field))
    if ds and de:
        return ds <= week_end and de >= week_start
    if ds:
        return week_start <= ds <= week_end
    if de:
        return week_start <= de <= week_end
    return False


@dataclass(slots=True)
class TrendLifecycleConfig:
    emerging_z_threshold: float
    active_min_count_ratio: float
    fading_z_threshold: float
    fading_count_ratio: float
    first_seen_lookback_weeks: int


def _classify_trend_status(
    *,
    keyword: str,
    z_score: float,
    count: int,
    prev_count: int,
    prev_status: str | None,
    seen_weeks: list[str],
    lifecycle: TrendLifecycleConfig,
) -> tuple[str, str, str, bool]:
    count_ratio = count / prev_count if prev_count > 0 else (1.0 if count > 0 else 0.0)
    lookback_seen = bool(seen_weeks[-lifecycle.first_seen_lookback_weeks :] if lifecycle.first_seen_lookback_weeks > 0 else seen_weeks)

    if z_score >= lifecycle.emerging_z_threshold and not lookback_seen:
        return (
            "Emerging",
            f"z>={lifecycle.emerging_z_threshold} & first_seen=true",
            "create_slot",
            True,
        )

    if z_score <= lifecycle.fading_z_threshold and count_ratio <= lifecycle.fading_count_ratio:
        return (
            "Fading",
            f"z<={lifecycle.fading_z_threshold} & count_ratio<={lifecycle.fading_count_ratio:.2f}",
            "close_slot_archive",
            False,
        )

    if count_ratio >= lifecycle.active_min_count_ratio:
        return (
            "Active",
            f"count_ratio>={lifecycle.active_min_count_ratio:.2f}",
            "update_slot",
            True,
        )

    if prev_status in {"Emerging", "Active"} and count > 0:
        return ("Active", "prev_status_active & count>0", "update_slot", True)

    return ("Fading", "fallback_fading", "close_slot_archive", False)


def _post_json(url: str, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json_safe(
    url: str,
    payload: dict[str, Any],
    timeout_sec: float,
    logger: Any,
    log_tag: str,
) -> dict[str, Any] | None:
    try:
        return _post_json(url, payload, timeout_sec)
    except HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(exc)
        logger.warning(
            "%s | http=%s | url=%s | body=%s",
            log_tag,
            getattr(exc, "code", "?"),
            url,
            err_body[:1200],
        )
        return None
    except Exception as exc:
        logger.warning("%s | url=%s | err=%s", log_tag, url, exc)
        return None


def _week_yyyymmdd_days(week_start: str, week_end: str) -> list[str]:
    ts0 = pd.Timestamp(f"{week_start[:4]}-{week_start[4:6]}-{week_start[6:8]}")
    ts1 = pd.Timestamp(f"{week_end[:4]}-{week_end[4:6]}-{week_end[6:8]}")
    out: list[str] = []
    d = ts0
    while d <= ts1:
        out.append(d.strftime("%Y%m%d"))
        d = d + pd.Timedelta(days=1)
    return out


def _week_server_filter(
    week_start: str,
    week_end: str,
    *,
    date_field: str,
    date_start_field: str,
    date_end_field: str,
    include_file_overlap: bool = True,
) -> dict[str, Any]:
    """
    Qdrant 서버 측 주차 제한 — payload로 후보를 줄인 뒤 벡터 유사도에 사용.

    - ``include_file_overlap=False``: ``date`` 가 해당 주 7일(YYYYMMDD) 중 하나인 포인트만.
    - ``include_file_overlap=True``: 위 조건 **또는** ``file_date_start`` ≤ 주말 **그리고**
      ``file_date_end`` ≥ 주초.
      ``file_date`` 분기는 `should` 안에 **nested Filter(`must`만)** 로 둡니다(Qdrant Condition oneOf).
    """
    days = _week_yyyymmdd_days(week_start, week_end)
    date_branch: dict[str, Any] = {"key": date_field, "match": {"any": days}}
    if not include_file_overlap:
        return {"must": [date_branch]}
    # `should` 원소는 Qdrant Condition(oneOf) — 중첩 Filter는 `must`/`should`만 두고 `filter` 래퍼 없음.
    week_start_num = _safe_int(week_start, 0)
    week_end_num = _safe_int(week_end, 0)
    file_branch: dict[str, Any] = {
        "must": [
            {"key": date_start_field, "range": {"lte": week_end_num}},
            {"key": date_end_field, "range": {"gte": week_start_num}},
        ]
    }
    return {"should": [date_branch, file_branch]}


def _map_hit_to_context(hit: dict[str, Any], *, keyword_norm: str) -> dict[str, Any]:
    payload = hit.get("payload") or {}
    pid = hit.get("id")
    score = float(hit.get("score", 0.0))
    text = " ".join(
        [
            str(payload.get("title") or payload.get("제목") or ""),
            str(payload.get("body") or payload.get("summary") or payload.get("본문") or payload.get("content") or ""),
        ]
    )
    t_norm = _normalize_keyword(text)
    bumped = score + (0.005 if keyword_norm and keyword_norm in t_norm else 0.0)
    return {
        # 근거 ID는 계약상 payload news_id 사용(없으면 point id 폴백). docs/QDRANT_CONTRACT.md
        "doc_id": str(payload.get("news_id") or pid or ""),
        "score": bumped,
        "title": str(payload.get("title") or payload.get("제목") or ""),
        "summary_or_body": str(
            payload.get("body") or payload.get("summary") or payload.get("본문") or payload.get("content") or ""
        ),
        "source": str(payload.get("press") or payload.get("source") or payload.get("언론사") or ""),
        "published_at": str(payload.get("date") or payload.get("published_at") or payload.get("일자") or ""),
    }


def _merge_hits_by_id(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[Any, dict[str, Any]] = {}
    for h in hits:
        pid = h.get("id")
        sc = float(h.get("score", 0.0))
        prev = best.get(pid)
        if prev is None or sc > float(prev.get("score", 0.0)):
            best[pid] = h
    return list(best.values())


def _dedupe_points_preserve_order(points_in: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """scroll 페이지를 이을 때 동일 point id가 반복되면 첫 등장만 유지."""
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for p in points_in:
        pid = p.get("id")
        if pid in seen:
            continue
        seen.add(pid)
        out.append(p)
    return out


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    if n <= 0:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(n):
        av = float(a[i])
        bv = float(b[i])
        dot += av * bv
        na += av * av
        nb += bv * bv
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return float(dot / ((na ** 0.5) * (nb ** 0.5)))


def _mean_vector(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    if dim <= 0:
        return []
    acc = [0.0] * dim
    cnt = 0
    for v in vectors:
        if len(v) != dim:
            continue
        cnt += 1
        for i, x in enumerate(v):
            acc[i] += float(x)
    if cnt <= 0:
        return []
    return [x / cnt for x in acc]


def _semantic_components(features: list[dict[str, Any]], threshold: float) -> list[list[int]]:
    n = len(features)
    if n == 0:
        return []
    adj: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        vi = features[i].get("feature_vec") or []
        for j in range(i + 1, n):
            vj = features[j].get("feature_vec") or []
            sim = _cosine_similarity(vi, vj)
            if sim >= threshold:
                adj[i].append(j)
                adj[j].append(i)
    seen = [False] * n
    comps: list[list[int]] = []
    for i in range(n):
        if seen[i]:
            continue
        stack = [i]
        seen[i] = True
        comp: list[int] = []
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nxt in adj[cur]:
                if not seen[nxt]:
                    seen[nxt] = True
                    stack.append(nxt)
        comps.append(sorted(comp))
    return comps


def _calc_group_cohesion(group_items: list[dict[str, Any]]) -> float:
    if len(group_items) <= 1:
        return 1.0
    sims: list[float] = []
    for i in range(len(group_items)):
        vi = group_items[i].get("feature_vec") or []
        for j in range(i + 1, len(group_items)):
            vj = group_items[j].get("feature_vec") or []
            sims.append(_cosine_similarity(vi, vj))
    if not sims:
        return 0.0
    return float(sum(sims) / len(sims))


def _vector_search_named(
    *,
    base: str,
    collection: str,
    vector: list[float],
    using: str,
    limit: int,
    qfilter: dict[str, Any] | None,
    timeout_sec: float,
    logger: Any,
    trace: str = "",
    attempt_label: str = "primary",
) -> list[dict[str, Any]]:
    url = f"{base}/collections/{collection}/points/search"
    dim = len(vector) if vector else 0
    filter_on = qfilter is not None
    logger.info(
        "QDRANT_PHASE | step=vector_search | attempt=%s | using=%s | dim=%d | limit=%d | server_filter=%s | %s",
        attempt_label,
        using,
        dim,
        int(limit),
        filter_on,
        trace,
    )
    body: dict[str, Any] = {
        "vector": {"name": using, "vector": vector},
        "limit": int(limit),
        "with_payload": True,
    }
    if qfilter is not None:
        body["filter"] = qfilter
    data = _post_json_safe(url, body, timeout_sec, logger, "QDRANT_VECTOR_SEARCH_FAIL")
    if not data:
        logger.info(
            "QDRANT_PHASE | step=vector_search | attempt=%s | using=%s | result=HTTP_OR_PARSE_FAIL | %s",
            attempt_label,
            using,
            trace,
        )
        return []
    res = data.get("result") or []
    hits = res if isinstance(res, list) else []
    logger.info(
        "QDRANT_PHASE | step=vector_search | attempt=%s | using=%s | hits=%d | %s",
        attempt_label,
        using,
        len(hits),
        trace,
    )
    return hits


def _scroll_collect_points(
    *,
    base: str,
    collection: str,
    scroll_limit: int,
    max_pages: int,
    qfilter: dict[str, Any] | None,
    timeout_sec: float,
    logger: Any,
    trace: str = "",
    scroll_attempt: str = "primary",
) -> list[dict[str, Any]]:
    scroll_url = f"{base}/collections/{collection}/points/scroll"
    scroll_payload: dict[str, Any] = {
        "limit": int(scroll_limit),
        "with_payload": True,
    }
    if qfilter is not None:
        scroll_payload["filter"] = qfilter
    filter_on = qfilter is not None
    logger.info(
        "QDRANT_PHASE | step=scroll_begin | attempt=%s | limit=%d | max_pages=%d | server_filter=%s | %s",
        scroll_attempt,
        int(scroll_limit),
        int(max_pages),
        filter_on,
        trace,
    )
    points: list[dict[str, Any]] = []
    page_payload = dict(scroll_payload)
    max_p = max(1, int(max_pages))
    for page_idx in range(max_p):
        data = _post_json_safe(scroll_url, page_payload, timeout_sec, logger, "QDRANT_SCROLL_FAIL")
        if not data:
            logger.info(
                "QDRANT_PHASE | step=scroll_page | attempt=%s | page=%d/%d | result=STOP_HTTP_OR_ERROR | raw_total=%d | %s",
                scroll_attempt,
                page_idx + 1,
                max_p,
                len(points),
                trace,
            )
            logger.info(
                "QDRANT_PHASE | step=scroll_end | attempt=%s | pages_used=%d/%d | raw_points=%d | exhausted=HTTP_OR_ERROR | %s",
                scroll_attempt,
                page_idx + 1,
                max_p,
                len(points),
                trace,
            )
            break
        result = data.get("result") or {}
        page_points = result.get("points") or []
        points.extend(page_points)
        logger.debug(
            "QDRANT_PHASE | step=scroll_page | attempt=%s | page=%d/%d | page_hits=%d | raw_total=%d | %s",
            scroll_attempt,
            page_idx + 1,
            max_p,
            len(page_points),
            len(points),
            trace,
        )
        next_off = result.get("next_page_offset")
        if not next_off or not page_points:
            logger.info(
                "QDRANT_PHASE | step=scroll_end | attempt=%s | pages_used=%d/%d | raw_points=%d | exhausted=%s | %s",
                scroll_attempt,
                page_idx + 1,
                max_p,
                len(points),
                bool(not next_off or not page_points),
                trace,
            )
            break
    else:
        logger.info(
            "QDRANT_PHASE | step=scroll_end | attempt=%s | pages_used=%d/%d | raw_points=%d | exhausted=False | %s",
            scroll_attempt,
            max_p,
            max_p,
            len(points),
            trace,
        )
    deduped = _dedupe_points_preserve_order(points)
    if len(deduped) != len(points):
        logger.info(
            "QDRANT_PHASE | step=scroll_dedupe | attempt=%s | before=%d | after=%d | %s",
            scroll_attempt,
            len(points),
            len(deduped),
            trace,
        )
    return deduped


def _query_qdrant_contexts(
    *,
    logger: Any,
    qdrant_url: str,
    collection: str,
    date_field: str,
    date_start_field: str,
    date_end_field: str,
    week: str,
    keyword: str,
    query_text: str | None,
    top_k: int,
    timeout_sec: float,
    enable_query_embedding: bool = True,
    embed_model: str | None = None,
    embed_device: str | None = None,
    query_vector_name: str = "body",
    use_server_date_filter: bool = True,
    vector_search_limit_multiplier: int = 3,
    vector_search_min_limit: int = 30,
    scroll_limit_multiplier: int = 12,
    scroll_min_limit: int = 100,
    scroll_max_pages: int = 12,
    week_scroll_mode: str = "default",
    scroll_full_week_max_pages: int = 0,
    server_week_filter_file_overlap: bool = True,
) -> list[dict[str, Any]]:
    week_start, week_end = _week_to_date_range_yyyymmdd(week)
    base = qdrant_url.rstrip("/")
    keyword_norm = _normalize_keyword(keyword)

    vf_chain: list[dict[str, Any]] = []
    if use_server_date_filter:
        if server_week_filter_file_overlap:
            vf_chain.append(
                _week_server_filter(
                    week_start,
                    week_end,
                    date_field=date_field,
                    date_start_field=date_start_field,
                    date_end_field=date_end_field,
                    include_file_overlap=True,
                )
            )
        vf_chain.append(
            _week_server_filter(
                week_start,
                week_end,
                date_field=date_field,
                date_start_field=date_start_field,
                date_end_field=date_end_field,
                include_file_overlap=False,
            )
        )
    scroll_tries: tuple[dict[str, Any] | None, ...] = (
        tuple(vf_chain + [None]) if use_server_date_filter else (None,)
    )

    search_limit = max(int(vector_search_min_limit), int(top_k) * int(vector_search_limit_multiplier))
    scroll_limit = max(int(scroll_min_limit), int(top_k) * int(scroll_limit_multiplier))
    eff_scroll_pages = max(1, int(scroll_max_pages))
    if str(week_scroll_mode).strip().lower() == "extended" and int(scroll_full_week_max_pages) > 0:
        eff_scroll_pages = max(eff_scroll_pages, int(scroll_full_week_max_pages))

    query_seed = (query_text or keyword or "").strip()
    _kw_disp = (keyword or "").strip().replace("\n", " ")[:120]
    trace = f"week={week}|keyword={_kw_disp}|collection={collection}|query_chars={len(query_seed)}"
    logger.info(
        "QDRANT_PIPELINE_BEGIN | %s | range_yyyymmdd=%s..%s | top_k=%d | base_url=%s | "
        "embed_enabled=%s | query_vector_name=%s | server_date_filter=%s | server_week_file_overlap=%s | "
        "vector_search_limit=%d | scroll_batch_limit=%d | scroll_max_pages_eff=%d | week_scroll_mode=%s | timeout_sec=%s",
        trace,
        week_start,
        week_end,
        int(top_k),
        base,
        bool(enable_query_embedding and query_seed),
        str(query_vector_name),
        bool(use_server_date_filter),
        bool(server_week_filter_file_overlap),
        int(search_limit),
        int(scroll_limit),
        int(eff_scroll_pages),
        str(week_scroll_mode),
        str(timeout_sec),
    )

    def _local_week_filter(points_in: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out_pts: list[dict[str, Any]] = []
        for p in points_in:
            payload = p.get("payload") or {}
            if _payload_hits_week_range(
                payload,
                week_start=week_start,
                week_end=week_end,
                date_field=date_field,
                date_start_field=date_start_field,
                date_end_field=date_end_field,
            ):
                out_pts.append(p)
        return out_pts

    def _keyword_rerank(points_in: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ranked_local: list[dict[str, Any]] = []
        for p in points_in:
            payload = p.get("payload") or {}
            text = " ".join(
                [
                    str(payload.get("title") or payload.get("제목") or ""),
                    str(payload.get("body") or payload.get("summary") or payload.get("본문") or payload.get("content") or ""),
                ]
            )
            t_norm = _normalize_keyword(text)
            score = 1.0 if keyword_norm and keyword_norm in t_norm else 0.0
            ranked_local.append(
                {
                    "doc_id": str(payload.get("news_id") or p.get("id") or ""),
                    "score": score,
                    "title": payload.get("title") or payload.get("제목") or "",
                    "summary_or_body": payload.get("body")
                    or payload.get("summary")
                    or payload.get("본문")
                    or payload.get("content")
                    or "",
                    "source": payload.get("press") or payload.get("source") or payload.get("언론사") or "",
                    "published_at": payload.get("date") or payload.get("published_at") or payload.get("일자") or "",
                }
            )
        ranked_local.sort(key=lambda x: (-float(x.get("score") or 0.0), str(x.get("doc_id") or "")))
        out_unique: list[dict[str, Any]] = []
        seen_doc: set[Any] = set()
        for row in ranked_local:
            did = row.get("doc_id")
            if did in seen_doc:
                continue
            seen_doc.add(did)
            out_unique.append(row)
            if len(out_unique) >= top_k:
                break
        return out_unique

    # --- 1) 벡터 검색 (ingest와 동일 SentenceTransformer + /points/search)
    if enable_query_embedding and query_seed:
        try:
            qvn = str(query_vector_name or "body").strip().lower()
            if qvn not in {"title", "body", "both"}:
                qvn = "body"
            logger.info(
                "QDRANT_PHASE | step=embed_query | vector_mode=%s | model=%s | device=%s | %s",
                qvn,
                embed_model or "(config/env default)",
                embed_device or "(config/env default)",
                trace,
            )
            vecs = embed_query_for_news(
                query_seed,
                vector_name=qvn,  # type: ignore[arg-type]
                model_name=embed_model or None,
                device=embed_device or None,
                normalize=True,
            )
            all_hits: list[dict[str, Any]] = []
            if qvn == "both":
                if use_server_date_filter:
                    for fi, filt in enumerate(vf_chain):
                        batch: list[dict[str, Any]] = []
                        if "title" in vecs:
                            batch.extend(
                                _vector_search_named(
                                    base=base,
                                    collection=collection,
                                    vector=vecs["title"],
                                    using="title",
                                    limit=search_limit,
                                    qfilter=filt,
                                    timeout_sec=timeout_sec,
                                    logger=logger,
                                    trace=trace,
                                    attempt_label=f"both_try{fi}_title",
                                )
                            )
                        if "body" in vecs:
                            batch.extend(
                                _vector_search_named(
                                    base=base,
                                    collection=collection,
                                    vector=vecs["body"],
                                    using="body",
                                    limit=search_limit,
                                    qfilter=filt,
                                    timeout_sec=timeout_sec,
                                    logger=logger,
                                    trace=trace,
                                    attempt_label=f"both_try{fi}_body",
                                )
                            )
                        if batch:
                            all_hits = batch
                            break
                else:
                    if "title" in vecs:
                        all_hits.extend(
                            _vector_search_named(
                                base=base,
                                collection=collection,
                                vector=vecs["title"],
                                using="title",
                                limit=search_limit,
                                qfilter=None,
                                timeout_sec=timeout_sec,
                                logger=logger,
                                trace=trace,
                                attempt_label="both_no_server_filter_title",
                            )
                        )
                    if "body" in vecs:
                        all_hits.extend(
                            _vector_search_named(
                                base=base,
                                collection=collection,
                                vector=vecs["body"],
                                using="body",
                                limit=search_limit,
                                qfilter=None,
                                timeout_sec=timeout_sec,
                                logger=logger,
                                trace=trace,
                                attempt_label="both_no_server_filter_body",
                            )
                        )
                if use_server_date_filter and not all_hits:
                    logger.info(
                        "QDRANT_PHASE | step=vector_search | note=retry_without_server_filter | %s",
                        trace,
                    )
                    if "title" in vecs:
                        all_hits.extend(
                            _vector_search_named(
                                base=base,
                                collection=collection,
                                vector=vecs["title"],
                                using="title",
                                limit=search_limit,
                                qfilter=None,
                                timeout_sec=timeout_sec,
                                logger=logger,
                                trace=trace,
                                attempt_label="retry_filter_off_title",
                            )
                        )
                    if "body" in vecs:
                        all_hits.extend(
                            _vector_search_named(
                                base=base,
                                collection=collection,
                                vector=vecs["body"],
                                using="body",
                                limit=search_limit,
                                qfilter=None,
                                timeout_sec=timeout_sec,
                                logger=logger,
                                trace=trace,
                                attempt_label="retry_filter_off_body",
                            )
                        )
            else:
                using = "body" if qvn == "body" else "title"
                vec = vecs.get(using) or vecs.get("body") or vecs.get("title")
                if vec:
                    if use_server_date_filter:
                        for fi, filt in enumerate(vf_chain):
                            all_hits = _vector_search_named(
                                base=base,
                                collection=collection,
                                vector=vec,
                                using=using,
                                limit=search_limit,
                                qfilter=filt,
                                timeout_sec=timeout_sec,
                                logger=logger,
                                trace=trace,
                                attempt_label=f"single_try{fi}",
                            )
                            if all_hits:
                                break
                    else:
                        all_hits = _vector_search_named(
                            base=base,
                            collection=collection,
                            vector=vec,
                            using=using,
                            limit=search_limit,
                            qfilter=None,
                            timeout_sec=timeout_sec,
                            logger=logger,
                            trace=trace,
                            attempt_label="single_no_server_filter",
                        )
                if use_server_date_filter and not all_hits and vec:
                    logger.info(
                        "QDRANT_PHASE | step=vector_search | note=retry_without_server_filter | %s",
                        trace,
                    )
                    all_hits = _vector_search_named(
                        base=base,
                        collection=collection,
                        vector=vec,
                        using=using,
                        limit=search_limit,
                        qfilter=None,
                        timeout_sec=timeout_sec,
                        logger=logger,
                        trace=trace,
                        attempt_label="retry_filter_off",
                    )

            merged = _merge_hits_by_id(all_hits) if qvn == "both" else _dedupe_points_preserve_order(all_hits)
            n_raw = len(merged)
            merged = _local_week_filter(merged)
            logger.info(
                "QDRANT_PHASE | step=local_week_filter | path=vector | raw_hits=%d | after_week=%d | %s",
                n_raw,
                len(merged),
                trace,
            )
            ctx_vec = [_map_hit_to_context(h, keyword_norm=keyword_norm) for h in merged]
            ctx_vec.sort(key=lambda x: x["score"], reverse=True)
            ctx_vec = ctx_vec[:top_k]
            if ctx_vec:
                logger.info(
                    "QDRANT_PIPELINE_DONE | path=vector | contexts=%d | merged_for_week=%d | %s",
                    len(ctx_vec),
                    len(merged),
                    trace,
                )
                return ctx_vec
            logger.info(
                "QDRANT_PHASE | step=vector_path_zero_contexts | after_top_k_slice=%d | falling_back_to_scroll | %s",
                len(ctx_vec),
                trace,
            )
        except Exception as exc:
            logger.warning(
                "QDRANT_EMBED_OR_VECTOR_FAIL | %s | err=%s",
                trace,
                exc,
            )

    # --- 2) scroll 폴백 (서버 필터 체인 → 빈 결과 시 다음 필터, 마지막은 무필터 + 로컬 주차)
    logger.info(
        "QDRANT_PHASE | step=scroll_fallback | scroll_passes_planned=%d | %s",
        len(scroll_tries),
        trace,
    )
    points: list[dict[str, Any]] = []
    for _si, qfilter_try in enumerate(scroll_tries):
        scroll_attempt = "no_server_filter" if qfilter_try is None else f"server_filter_try{_si}"
        points = _scroll_collect_points(
            base=base,
            collection=collection,
            scroll_limit=scroll_limit,
            max_pages=eff_scroll_pages,
            qfilter=qfilter_try,
            timeout_sec=timeout_sec,
            logger=logger,
            trace=trace,
            scroll_attempt=scroll_attempt,
        )
        if points:
            logger.info(
                "QDRANT_PHASE | step=scroll_fallback | pass=%d | attempt=%s | raw_points=%d | stopping_passes | %s",
                _si + 1,
                scroll_attempt,
                len(points),
                trace,
            )
            break
        logger.info(
            "QDRANT_PHASE | step=scroll_fallback | pass=%d | attempt=%s | raw_points=0 | try_next_pass | %s",
            _si + 1,
            scroll_attempt,
            trace,
        )

    n_scroll_week_before = len(points)
    points = _local_week_filter(points)
    logger.info(
        "QDRANT_PHASE | step=local_week_filter | path=scroll | before=%d | after=%d | %s",
        n_scroll_week_before,
        len(points),
        trace,
    )
    out = _keyword_rerank(points)
    n_kw_positive = sum(1 for x in out if float(x.get("score") or 0) > 0.0)
    logger.info(
        "QDRANT_PIPELINE_DONE | path=scroll | out_top_k=%d | keyword_text_hits_in_output=%d | %s",
        len(out),
        n_kw_positive,
        trace,
    )
    if not out:
        logger.info(
            "QDRANT_CONTEXT_EMPTY | %s | range_yyyymmdd=%s..%s | date_field=%s | scroll_after_week_filter=%d",
            trace,
            week_start,
            week_end,
            date_field,
            len(points),
        )
    return out


def run_trend_extractor(
    z_score_csv: Optional[Path] = None,
    high_z_score_csv: Optional[Path] = None,
    weekly_keywords_csv: Optional[Path] = None,
    *,
    base_start_week: str | None = None,
    base_end_week: str | None = None,
    test_week_offset: int = 0,
    test_max_weeks: Optional[int] = None,
    test_mode: bool = False,
    z_score_df: Optional["pd.DataFrame"] = None,
    weekly_df: Optional["pd.DataFrame"] = None,
) -> Dict[str, Path]:
    """5+6단계: keysentence 통합 trend 추출.

    입력 우선순위(파일 의존 제거용):
      - z_score: z_score_df 인자 → 없으면 z_score_csv
      - weekly counts: weekly_df 인자(week,keyword,count) → 없으면 weekly_keywords_csv
    high_z_score 는 현재 미사용(읽기만, 향후 호환용).
    """
    logger = get_logger("steps.trend_extractor")
    with log_step(
        logger,
        "5+6",
        "trend_extractor",
        z_score_csv=str(z_score_csv.resolve()) if z_score_csv else None,
        high_z_score_csv=str(high_z_score_csv.resolve()) if high_z_score_csv else None,
        weekly_keywords_csv=str(weekly_keywords_csv.resolve()) if weekly_keywords_csv else None,
    ):
        cfg = load_config()
        tr_cfg = cfg.get("trend_extractor", {})
        vector_cfg = cfg.get("vector_db", {})
        lc = cfg.get("lifecycle", {})

        lifecycle = TrendLifecycleConfig(
            emerging_z_threshold=float(lc.get("emerging_z_threshold", 2.0)),
            active_min_count_ratio=float(lc.get("active_min_count_ratio", 0.7)),
            fading_z_threshold=float(lc.get("fading_z_threshold", 0.8)),
            fading_count_ratio=float(lc.get("fading_count_ratio", 0.4)),
            first_seen_lookback_weeks=int(lc.get("first_seen_lookback_weeks", 4)),
        )
        anchor_top_n = int(tr_cfg.get("anchor_top_n", 5))
        persistence_min_count = int(tr_cfg.get("persistence_min_count", 3))
        # 노이즈 라벨(keyword_class) 소비: 라벨된 키워드를 anchor 선정에서 제외(삭제 아님 — review 버킷 보존).
        exclude_noise = bool(tr_cfg.get("exclude_noise_classes", True))
        exclude_classes = list(tr_cfg.get("exclude_classes", ["seasonal", "politics"]))
        # (b)(c) 캐리오버: 전 주 선정 키워드를 무조건 다음 주로 이월하고 변화 수치를 기록.
        #   carryover_unconditional=False 면 기존 동작(major & count>=persistence_min_count)으로 복귀.
        #   carryover_sunset_fading_weeks: Fading 이 N주 연속이면 이월 종료(sunset). 0이면 무한 추적.
        carryover_unconditional = bool(tr_cfg.get("carryover_unconditional", True))
        carryover_sunset_fading_weeks = int(tr_cfg.get("carryover_sunset_fading_weeks", 2))
        vector_top_k = int(vector_cfg.get("top_k", tr_cfg.get("vector_top_k", 10)))
        date_field = str(vector_cfg.get("date_field", "date"))
        date_start_field = str(vector_cfg.get("date_start_field", "file_date_start"))
        date_end_field = str(vector_cfg.get("date_end_field", "file_date_end"))
        # Qdrant URL은 .env(QDRANT_HOST/PORT)를 단일 출처로 사용한다(db.config.qdrant_url).
        # config의 qdrant_url 은 env 미설정 시의 폴백으로만 사용(원격 Tailscale 주소를 커밋하지 않기 위함).
        from db.config import qdrant_url as _resolve_qdrant_url

        qdrant_url = _resolve_qdrant_url() or str(vector_cfg.get("qdrant_url", "http://localhost:6333"))
        collection = str(vector_cfg.get("collection", "news_10y_ko_v1"))
        timeout_sec = float(vector_cfg.get("timeout_sec", 20))
        enable_query_embedding = bool(vector_cfg.get("enable_query_embedding", True))
        embed_model = vector_cfg.get("embed_model")
        embed_device = vector_cfg.get("embed_device")
        query_vector_name = str(vector_cfg.get("query_vector_name", "body"))
        use_server_date_filter = bool(vector_cfg.get("use_server_date_filter", True))
        vector_search_limit_multiplier = int(vector_cfg.get("vector_search_limit_multiplier", 3))
        vector_search_min_limit = int(vector_cfg.get("vector_search_min_limit", 30))
        scroll_limit_multiplier = int(vector_cfg.get("scroll_limit_multiplier", 12))
        scroll_min_limit = int(vector_cfg.get("scroll_min_limit", 100))
        scroll_max_pages = int(vector_cfg.get("scroll_max_pages", 12))
        week_scroll_mode = str(vector_cfg.get("week_scroll_mode", "default"))
        scroll_full_week_max_pages = int(vector_cfg.get("scroll_full_week_max_pages", 0))
        server_week_filter_file_overlap = bool(vector_cfg.get("server_week_filter_file_overlap", True))

        if enable_query_embedding:
            try:
                from steps.qdrant_embed import get_embed_model

                get_embed_model(
                    model_name=str(embed_model).strip() if embed_model else None,
                    device=str(embed_device).strip() if embed_device else None,
                )
            except Exception as exc:
                logger.warning("임베딩 모델 선로드 실패(실행 시 scroll 폴백 가능): %s", exc)

        coverage = tr_cfg.get("vector_date_coverage", ["2016-W01", "2022-W52"])
        coverage_start = str(base_start_week or coverage[0])
        coverage_end = str(base_end_week or coverage[1])
        semantic_group_threshold = float(tr_cfg.get("semantic_group_threshold", 0.72))
        max_groups_per_week = int(tr_cfg.get("max_groups_per_week", 5))
        group_link_threshold = float(tr_cfg.get("group_link_threshold", 0.75))
        score_w = tr_cfg.get(
            "group_score_weights",
            {"z_sum": 1.0, "count_log": 1.2, "ctx_mean": 1.0, "cohesion": 1.0},
        )
        w_z_sum = float(score_w.get("z_sum", 1.0))
        w_count_log = float(score_w.get("count_log", 1.2))
        w_ctx_mean = float(score_w.get("ctx_mean", 1.0))
        w_cohesion = float(score_w.get("cohesion", 1.0))

        # 입력 우선순위: in-memory frame → 제공된 CSV(존재 시) → DB 직접 읽기
        if z_score_df is not None:
            z_df = z_score_df.copy()
        elif z_score_csv is not None and z_score_csv.exists():
            z_df = read_csv(z_score_csv)
        else:
            # coverage 범위로 스코핑(아래에서 어차피 coverage로 clamp되므로 결과 동일, 단일 주차는 수 초).
            z_df = repo.read_zscore(start_week=coverage_start, end_week=coverage_end)
            logger.info(
                "z_score 입력 | DB newstrend.z_score_keywords 직접 읽기 | range=%s~%s | rows=%d",
                coverage_start, coverage_end, len(z_df),
            )
        high_df = read_csv(high_z_score_csv) if (high_z_score_csv is not None and high_z_score_csv.exists()) else pd.DataFrame()
        if weekly_df is not None:
            wk_df = weekly_df.copy()
        elif weekly_keywords_csv is not None and weekly_keywords_csv.exists():
            wk_df = read_csv(weekly_keywords_csv)
        else:
            wk_df = repo.read_weekly_freq(start_week=coverage_start, end_week=coverage_end)
            logger.info(
                "weekly counts 입력 | DB newstrend.weekly_keyword_freq 직접 읽기 | range=%s~%s | rows=%d",
                coverage_start, coverage_end, len(wk_df),
            )
        # P2: keysentence는 더 이상 외부 입력이 아니라 이 단계에서 2-phase 검색으로 생성한다.
        keysentence_max_titles = int(cfg.get("keysentence_extractor", {}).get("max_titles_for_prompt", 25))
        if z_df.empty or wk_df.empty:
            raise ValueError("trend_extractor 입력 CSV가 비어 있습니다.")

        z_df["week"] = z_df["week"].astype(str)
        z_df["keyword"] = z_df["keyword"].astype(str)
        # 벡터화(동작 보존): DB 직접 읽기 시 z_df/wk_df 가 전체 이력(수백만 행)이라
        # .apply / iterrows 는 단일 주차 실행에도 수십 분이 걸린다. 동일 결과를 벡터 연산으로 산출.
        z_df["z_score"] = pd.to_numeric(z_df["z_score"], errors="coerce").fillna(0.0)
        wk_counts = wk_df.groupby(["week", "keyword"], as_index=False)["count"].sum()
        wk_counts["week"] = wk_counts["week"].astype(str)
        wk_counts["keyword"] = wk_counts["keyword"].astype(str)
        _counts = pd.to_numeric(wk_counts["count"], errors="coerce").fillna(0).astype(int).tolist()
        count_map = dict(zip(zip(wk_counts["week"].tolist(), wk_counts["keyword"].tolist()), _counts))

        all_weeks = _sort_week_labels(sorted(z_df["week"].unique().tolist()))
        clamped_weeks = [w for w in all_weeks if _week_in_coverage(w, coverage_start, coverage_end)]
        if not clamped_weeks:
            raise ValueError(f"Coverage 범위({coverage_start}~{coverage_end})에서 실행 가능한 주차가 없습니다.")

        offset = max(0, int(test_week_offset or 0))
        weeks = clamped_weeks[offset:]
        if test_mode and test_max_weeks is None:
            test_max_weeks = 10
        if test_max_weeks is not None:
            weeks = weeks[: max(1, int(test_max_weeks))]
        if not weeks:
            raise ValueError("test_week_offset/test_max_weeks 조합으로 실행 주차가 비었습니다.")
        logger.info("trend_extractor 실행 주차 수=%d | first=%s | last=%s", len(weeks), weeks[0], weeks[-1])

        # 노이즈 라벨 로드: 키워드별 라벨(플래그) + anchor 제외 집합. 정책=라벨된 건 anchor에서 빼되 삭제 안 함.
        kw2classes: dict[str, list[str]] = {}
        exclude_kw: set[str] = set()
        try:
            kc_df = repo.read_keyword_class()
            for _kw, _cls in zip(kc_df["keyword"].astype(str), kc_df["class"].astype(str)):
                lst = kw2classes.setdefault(_kw, [])
                if _cls not in lst:
                    lst.append(_cls)
            if exclude_noise:
                exclude_kw = {k for k, cl in kw2classes.items() if any(c in exclude_classes for c in cl)}
            logger.info(
                "노이즈 라벨 로드 | 라벨키워드=%d | anchor제외=%d | 제외class=%s",
                len(kw2classes), len(exclude_kw), exclude_classes if exclude_noise else "OFF(토글)",
            )
        except Exception as exc:
            logger.warning("keyword_class 로드 실패(제외 없이 진행): %s", exc)

        llm_weekly = get_llm("individual_summary")
        llm_seq = get_llm("sequential_analysis")

        seen_history: dict[str, list[str]] = {}
        prev_status_map: dict[str, str] = {}
        prev_major_keywords: set[str] = set()
        carryover_keywords: set[str] = set()   # (b) 다음 주로 무조건 이월할 전 주 선정 키워드
        fading_streak: dict[str, int] = {}     # (c) 키워드별 연속 Fading 주차 수(sunset 판정)
        prev_week_summary: str = ""

        anchor_rows: list[dict[str, Any]] = []
        context_rows: list[dict[str, Any]] = []
        report_rows: list[dict[str, Any]] = []
        group_rows: list[dict[str, Any]] = []
        keysentence_rows: list[dict[str, Any]] = []  # P2: 2-phase 검색 부산물
        prev_group_items: list[dict[str, Any]] = []
        slot_seq = 0

        for week_idx, week in enumerate(weeks):
            week_z = z_df[z_df["week"] == week].sort_values("z_score", ascending=False)
            # 노이즈 라벨 키워드는 anchor 후보에서 제외 → 진짜 트렌드가 top-N을 채움(week_z 자체는
            # 이후 키워드별 z 조회에 그대로 쓰므로 anchor용으로만 필터링).
            week_z_anchor = week_z[~week_z["keyword"].astype(str).isin(exclude_kw)] if exclude_kw else week_z
            group_a = week_z_anchor.head(anchor_top_n)["keyword"].astype(str).tolist()

            # Group B = 전 주에서 이월된 키워드.
            #  (b) carryover_unconditional: 전 주 선정분을 count 조건 없이 무조건 이월(변화 추적용).
            #      기존 동작은 'major & count>=persistence_min_count'.
            group_b: list[str] = []
            if carryover_unconditional:
                group_b = sorted(carryover_keywords)
            else:
                for keyword in sorted(prev_major_keywords):
                    c = count_map.get((week, keyword), 0)
                    if c >= persistence_min_count:
                        group_b.append(keyword)
            if exclude_kw:
                group_b = [k for k in group_b if k not in exclude_kw]
            selected_keywords = sorted(set(group_a).union(group_b))
            if not selected_keywords:
                continue

            logger.info(
                "TREND_EXTRACTOR_PHASE | step=week_batch_begin | week_index=%d/%d | week=%s | anchor_top_n=%d | "
                "selected_keywords=%d",
                week_idx + 1,
                len(weeks),
                week,
                anchor_top_n,
                len(selected_keywords),
            )

            keyword_features: list[dict[str, Any]] = []
            for kw_idx, keyword in enumerate(selected_keywords, start=1):
                week_keyword = week_z[week_z["keyword"] == keyword]
                z_score = _safe_float(week_keyword["z_score"].iloc[0] if not week_keyword.empty else 0.0)
                count = count_map.get((week, keyword), 0)
                prev_count = 0
                prev_week = weeks[week_idx - 1] if week_idx > 0 else None
                if prev_week:
                    prev_count = count_map.get((prev_week, keyword), 0)
                prev_status = prev_status_map.get(keyword)
                seen_weeks = seen_history.get(keyword, [])
                status, reason, action, major = _classify_trend_status(
                    keyword=keyword,
                    z_score=z_score,
                    count=count,
                    prev_count=prev_count,
                    prev_status=prev_status,
                    seen_weeks=seen_weeks,
                    lifecycle=lifecycle,
                )

                anchor_group = []
                if keyword in group_a:
                    anchor_group.append("A")
                if keyword in group_b:
                    anchor_group.append("B")

                trend_slot_id = f"slot_{_normalize_keyword(keyword)[:48] or 'unknown'}"
                anchor_rows.append(
                    {
                        "week": week,
                        "keyword": keyword,
                        "anchor_group": "+".join(anchor_group),
                        "z_score": z_score,
                        "count": count,
                        "prev_count": prev_count,
                        "count_change_ratio": (count / prev_count) if prev_count > 0 else (1.0 if count > 0 else 0.0),
                        "prev_status": prev_status or "",
                        "status": status,
                        "status_reason": reason,
                        "action_taken": action,
                        "trend_slot_id": trend_slot_id,
                        "is_major_trend": major,
                        "final_archive_flag": status == "Fading",
                    }
                )

                # (b) 비용 분리: 신규/활성(또는 이번 주 top-N anchor)만 Qdrant 검색 + LLM 요약을 돌린다.
                #     이월된 비활성(Fading 등) 키워드는 수치(z/count/delta)만 anchor_rows에 기록되고
                #     비싼 벡터검색/LLM은 건너뛴다(집합이 커져도 비용이 폭증하지 않도록).
                needs_full = (keyword in group_a) or (status in {"Emerging", "Active"})
                if needs_full:
                    _kw_log = str(keyword).replace("\n", " ")[:100]
                    # 2-phase 검색(P2): ① 키워드로 거친 검색 → ② LLM 요약(query_text) → ③ 요약 시드로 정밀 검색
                    c0 = search_contexts(week, keyword, keyword, logger=logger, config=cfg, top_k=vector_top_k)
                    if c0:
                        ks_articles = [
                            {"title": d.get("title"), "body": d.get("summary_or_body"), "source": d.get("source")}
                            for d in c0[: max(1, keysentence_max_titles)]
                        ]
                        key_sentence = (llm_weekly.invoke(_build_keyword_prompt(week, keyword, ks_articles)) or "").strip()
                    else:
                        key_sentence = ""
                    query_for_search = key_sentence if key_sentence else keyword
                    logger.info(
                        "TREND_EXTRACTOR_PHASE | step=qdrant_context | week_index=%d/%d | kw_index=%d/%d | week=%s | "
                        "keyword=%s | z=%.3f | count=%d | query_chars=%d | query_from=%s",
                        week_idx + 1, len(weeks), kw_idx, len(selected_keywords), week, _kw_log,
                        z_score, count, len(str(query_for_search)), "keysentence" if key_sentence else "keyword",
                    )
                    contexts = search_contexts(week, keyword, query_for_search, logger=logger, config=cfg, top_k=vector_top_k)
                    logger.info(
                        "TREND_EXTRACTOR_PHASE | step=qdrant_context_done | week=%s | keyword=%s | context_docs=%d",
                        week,
                        _kw_log,
                        len(contexts),
                    )
                    doc_ids: list[str] = []
                    for ctx in contexts:
                        doc_ids.append(str(ctx.get("doc_id", "")))
                        context_rows.append({"week": week, "keyword": keyword, **ctx})

                    # keysentence 부산물 적립(evidence_doc_ids = news_id)
                    _uniq_docs = list(dict.fromkeys([d for d in doc_ids if d]))
                    keysentence_rows.append({
                        "week": week,
                        "keyword": keyword,
                        "query_text": key_sentence,
                        "key_sentence": key_sentence,
                        "evidence_doc_ids": _uniq_docs,
                        "evidence_count": len(contexts),
                    })

                    evidence_text = " ".join(str(c.get("summary_or_body", ""))[:220] for c in contexts[:3]).strip()
                    feature_text = str(query_for_search or evidence_text or keyword).strip()
                    ctx_scores = [float(c.get("score", 0.0) or 0.0) for c in contexts]
                    avg_ctx_score = float(sum(ctx_scores) / len(ctx_scores)) if ctx_scores else 0.0
                    feature_vec: list[float] = []
                    try:
                        fv = embed_query_for_news(
                            feature_text,
                            vector_name="body",
                            model_name=str(embed_model).strip() if embed_model else None,
                            device=str(embed_device).strip() if embed_device else None,
                            normalize=True,
                        ).get("body")
                        feature_vec = [float(x) for x in (fv or [])]
                    except Exception:
                        feature_vec = []
                    keyword_features.append(
                        {
                            "week": week,
                            "keyword": keyword,
                            "z_score": z_score,
                            "count": count,
                            "trend_slot_id": trend_slot_id,
                            "status": status,
                            "status_reason": reason,
                            "action_taken": action,
                            "is_major_trend": major,
                            "prev_status": prev_status or "",
                            "prev_count": prev_count,
                            "count_change_ratio": (count / prev_count) if prev_count > 0 else (1.0 if count > 0 else 0.0),
                            "evidence_text": evidence_text,
                            "query_text": feature_text,
                            "contexts": contexts,
                            "feature_vec": feature_vec,
                            "avg_ctx_score": avg_ctx_score,
                        }
                    )

                seen_history.setdefault(keyword, []).append(week)
                prev_status_map[keyword] = status

            comps = _semantic_components(keyword_features, semantic_group_threshold)
            groups: list[dict[str, Any]] = []
            for gi, comp in enumerate(comps, start=1):
                items = [keyword_features[idx] for idx in comp]
                items_sorted = sorted(items, key=lambda x: float(x.get("z_score", 0.0)), reverse=True)
                centroid = _mean_vector([list(x.get("feature_vec") or []) for x in items_sorted])
                sum_z = float(sum(max(0.0, float(x.get("z_score", 0.0))) for x in items_sorted))
                sum_count = int(sum(int(x.get("count", 0) or 0) for x in items_sorted))
                mean_ctx = float(sum(float(x.get("avg_ctx_score", 0.0)) for x in items_sorted) / len(items_sorted)) if items_sorted else 0.0
                cohesion = _calc_group_cohesion(items_sorted)
                group_score = (
                    w_z_sum * sum_z
                    + w_count_log * float((sum_count + 1) ** 0.5)
                    + w_ctx_mean * mean_ctx
                    + w_cohesion * cohesion
                )
                groups.append(
                    {
                        "group_idx": gi,
                        "items": items_sorted,
                        "keywords": [str(x.get("keyword")) for x in items_sorted],
                        "centroid": centroid,
                        "sum_z": sum_z,
                        "sum_count": sum_count,
                        "mean_ctx": mean_ctx,
                        "cohesion": cohesion,
                        "group_score": group_score,
                    }
                )
            groups.sort(key=lambda x: float(x.get("group_score", 0.0)), reverse=True)
            if max_groups_per_week > 0:
                groups = groups[:max_groups_per_week]
            grouped_keywords = {str(k) for g in groups for k in list(g.get("keywords") or [])}
            ungrouped_items = [x for x in keyword_features if str(x.get("keyword")) not in grouped_keywords]

            week_group_summaries: list[str] = []
            current_group_items: list[dict[str, Any]] = []
            for rank, g in enumerate(groups, start=1):
                kws = g.get("keywords") or []
                items = g.get("items") or []
                snippets = []
                for it in items[:8]:
                    snippets.append(
                        f"- {it.get('keyword')} (z={float(it.get('z_score', 0.0)):.2f}, count={int(it.get('count', 0))}): {str(it.get('evidence_text') or '')[:220]}"
                    )
                group_prompt = (
                    f"주차: {week}\n"
                    f"트렌드 그룹 키워드: {', '.join(kws)}\n"
                    "아래 근거를 바탕으로 이 그룹의 핵심 트렌드를 사실 기반 1~2문장으로 요약하세요.\n"
                    + "\n".join(snippets)
                )
                group_summary = llm_weekly.invoke(group_prompt).strip()
                if not group_summary:
                    group_summary = f"{week} 트렌드 그룹({', '.join(kws[:3])}) 요약 생성 실패"

                prev_match: dict[str, Any] | None = None
                prev_sim = 0.0
                for pg in prev_group_items:
                    sim = _cosine_similarity(list(g.get("centroid") or []), list(pg.get("centroid") or []))
                    if sim > prev_sim:
                        prev_sim = sim
                        prev_match = pg
                linked = prev_match is not None and prev_sim >= group_link_threshold
                if linked:
                    slot_id = str(prev_match.get("trend_slot_id"))
                    prev_group_id = str(prev_match.get("group_id"))
                else:
                    slot_seq += 1
                    slot_id = f"trendgrp_{slot_seq:06d}"
                    prev_group_id = ""
                group_id = f"{week}_g{rank}"

                transition_text = ""
                if linked and str(prev_match.get("group_summary") or "").strip():
                    seq_prompt = (
                        f"이전 주 그룹 요약: {str(prev_match.get('group_summary') or '')}\n"
                        f"현재 주 그룹 요약: {group_summary}\n"
                        "두 그룹의 연속성/변화를 1~2문장으로 설명하세요."
                    )
                    transition_text = llm_seq.invoke(seq_prompt).strip()

                group_status = "Emerging" if not linked else "Active"
                if linked and transition_text:
                    low = transition_text.lower()
                    if "약화" in transition_text or "감소" in transition_text or "축소" in transition_text or "decline" in low:
                        group_status = "Fading"

                evidence_doc_ids: list[str] = []
                anchor_keywords = [str(it.get("keyword")) for it in items if bool(it.get("is_major_trend"))]
                for it in items:
                    for c in list(it.get("contexts") or [])[:10]:
                        did = str(c.get("doc_id", "")).strip()
                        if did and did not in evidence_doc_ids:
                            evidence_doc_ids.append(did)

                group_rows.append(
                    {
                        "week": week,
                        "group_id": group_id,
                        "trend_slot_id": slot_id,
                        "group_rank": rank,
                        "group_score": float(g.get("group_score", 0.0)),
                        "group_status": group_status,
                        "keywords": "|".join(kws),
                        "anchor_keywords": "|".join(anchor_keywords),
                        "keyword_count": len(kws),
                        "sum_z_score": float(g.get("sum_z", 0.0)),
                        "sum_count": int(g.get("sum_count", 0)),
                        "mean_context_score": float(g.get("mean_ctx", 0.0)),
                        "cohesion": float(g.get("cohesion", 0.0)),
                        "group_summary": group_summary,
                        "transition_from_prev_group": transition_text,
                        "prev_group_id": prev_group_id,
                        "similarity_to_prev_group": float(prev_sim),
                        "evidence_doc_ids": "|".join(evidence_doc_ids[:20]),
                    }
                )
                week_group_summaries.append(group_summary)
                current_group_items.append(
                    {
                        "group_id": group_id,
                        "trend_slot_id": slot_id,
                        "group_summary": group_summary,
                        "centroid": list(g.get("centroid") or []),
                    }
                )

                for it in items:
                    it["group_id"] = group_id
                    it["group_summary"] = group_summary
                    it["group_score"] = float(g.get("group_score", 0.0))
                    it["group_status"] = group_status

            # max_groups_per_week로 잘린 키워드는 강제 병합하지 않고 단독 그룹으로 유지한다.
            for ui in ungrouped_items:
                rank = len(group_rows) + 1
                group_id = f"{week}_solo_{_normalize_keyword(str(ui.get('keyword') or 'kw'))[:24] or 'kw'}"
                summary = str(ui.get("query_text") or ui.get("evidence_text") or ui.get("keyword") or "").strip()
                if not summary:
                    summary = f"{week} 단독 키워드 요약 생성 실패"
                ui["group_id"] = group_id
                ui["group_summary"] = summary
                ui["group_score"] = float(ui.get("z_score", 0.0) or 0.0)
                ui["group_status"] = "Emerging"
                group_rows.append(
                    {
                        "week": week,
                        "group_id": group_id,
                        "trend_slot_id": f"trendgrp_solo_{_normalize_keyword(str(ui.get('keyword') or 'kw'))[:24] or 'kw'}",
                        "group_rank": rank,
                        "group_score": float(ui.get("group_score", 0.0) or 0.0),
                        "group_status": "Emerging",
                        "keywords": str(ui.get("keyword") or ""),
                        "anchor_keywords": str(ui.get("keyword") or ""),
                        "keyword_count": 1,
                        "sum_z_score": float(ui.get("z_score", 0.0) or 0.0),
                        "sum_count": int(ui.get("count", 0) or 0),
                        "mean_context_score": float(ui.get("avg_ctx_score", 0.0) or 0.0),
                        "cohesion": 1.0,
                        "group_summary": summary,
                        "transition_from_prev_group": "",
                        "prev_group_id": "",
                        "similarity_to_prev_group": 0.0,
                        "evidence_doc_ids": "|".join(
                            str(c.get("doc_id", ""))
                            for c in list(ui.get("contexts") or [])[:10]
                            if str(c.get("doc_id", "")).strip()
                        ),
                    }
                )
                week_group_summaries.append(summary)

            week_anchor_df = pd.DataFrame([r for r in anchor_rows if r["week"] == week])
            prev_status_lookup = {
                (r["week"], r["keyword"]): r["status"]
                for r in anchor_rows
            }
            for _, row in week_anchor_df.iterrows():
                keyword = str(row["keyword"])
                f_item = next((x for x in keyword_features if str(x.get("keyword")) == keyword), None)
                prev_week = weeks[week_idx - 1] if week_idx > 0 else ""
                prev_z = 0.0
                if prev_week:
                    prev_slice = z_df[(z_df["week"] == prev_week) & (z_df["keyword"] == keyword)]
                    if not prev_slice.empty:
                        prev_z = _safe_float(prev_slice["z_score"].iloc[0], 0.0)
                next_week_status = ""
                if week_idx + 1 < len(weeks):
                    nw = weeks[week_idx + 1]
                    next_week_status = prev_status_lookup.get((nw, keyword), "")
                evidence = []
                if f_item is not None:
                    evidence = [str(c.get("doc_id", "")) for c in list(f_item.get("contexts") or [])[:10] if str(c.get("doc_id", "")).strip()]
                weekly_summary = str(f_item.get("group_summary") if f_item else "") or (week_group_summaries[0] if week_group_summaries else "")
                group_id = str(f_item.get("group_id") if f_item else "")
                group_score = _safe_float(f_item.get("group_score") if f_item else 0.0, 0.0)
                group_status = str(f_item.get("group_status") if f_item else "")
                report_rows.append(
                    {
                        "week": week,
                        "keyword": keyword,
                        "trend_slot_id": row["trend_slot_id"],
                        "group_id": group_id,
                        "group_score": group_score,
                        "group_status": group_status,
                        "status": row["status"],
                        "status_reason": row["status_reason"],
                        "action_taken": row["action_taken"],
                        "is_major_trend": bool(row["is_major_trend"]),
                        "prev_week_status": row["prev_status"],
                        "next_week_status": next_week_status,
                        "count": int(row["count"]),
                        "prev_count": int(row["prev_count"]),
                        "count_change_ratio": _safe_float(row["count_change_ratio"]),
                        "z_score": _safe_float(row["z_score"]),
                        "prev_z_score": prev_z,
                        "z_score_change": _safe_float(row["z_score"]) - prev_z,
                        # (a) DB 영속화용 명시 컬럼: write_trend 이 그대로 적재한다.
                        "delta_z": _safe_float(row["z_score"]) - prev_z,
                        "delta_count": int(row["count"]) - int(row["prev_count"]),
                        "count_ratio": _safe_float(row["count_change_ratio"]),
                        "weekly_summary": weekly_summary,
                        "transition_from_prev": "",
                        "trend_strength": _safe_float(row["z_score"]),
                        "evidence_doc_ids": "|".join(str(x) for x in evidence if x),
                        "final_archive_flag": bool(row["final_archive_flag"]),
                        # 노이즈 라벨 플래그(제외 토글 off/부분 제외 시 의미). write_trend 가 noise_classes 로 적재.
                        "noise_classes": kw2classes.get(keyword, []),
                    }
                )

            logger.info(
                "TREND_EXTRACTOR_PHASE | step=week_batch_end | week_index=%d/%d | week=%s | anchors_this_week=%d | "
                "report_rows_total=%d",
                week_idx + 1,
                len(weeks),
                week,
                len(week_anchor_df),
                len(report_rows),
            )

            prev_week_summary = " ".join(week_group_summaries[:3]).strip()
            prev_group_items = current_group_items
            prev_major_keywords = {r["keyword"] for r in anchor_rows if r["week"] == week and r["is_major_trend"]}

            # (c) 다음 주 이월 집합 갱신 + Fading 사멸(sunset).
            #   이번 주 선정 키워드별 status로 연속 Fading 카운트를 갱신하고,
            #   N주 연속 Fading 이면 이월 대상에서 제외(추적 종료). N=0이면 무한 추적.
            week_status = {r["keyword"]: r["status"] for r in anchor_rows if r["week"] == week}
            next_carryover: set[str] = set()
            for kw, st in week_status.items():
                if st == "Fading":
                    fading_streak[kw] = fading_streak.get(kw, 0) + 1
                else:
                    fading_streak[kw] = 0
                sunset = (
                    carryover_sunset_fading_weeks > 0
                    and fading_streak[kw] >= carryover_sunset_fading_weeks
                )
                if not sunset:
                    next_carryover.add(kw)
            carryover_keywords = next_carryover

        output_dir = ensure_output_dir()
        anchors_df = pd.DataFrame(anchor_rows)
        contexts_df = pd.DataFrame(context_rows)
        report_df = pd.DataFrame(report_rows)
        group_df = pd.DataFrame(group_rows)
        dashboard_df = report_df[
            [
                "week",
                "keyword",
                "trend_slot_id",
                "group_id",
                "group_score",
                "status",
                "trend_strength",
                "weekly_summary",
                "transition_from_prev",
                "evidence_doc_ids",
            ]
        ].copy() if not report_df.empty else pd.DataFrame(
            columns=[
                "week",
                "keyword",
                "trend_slot_id",
                "group_id",
                "group_score",
                "status",
                "trend_strength",
                "weekly_summary",
                "transition_from_prev",
                "evidence_doc_ids",
            ]
        )

        anchors_csv = write_csv(anchors_df, output_dir / "trend_anchors.csv")
        anchors_json = write_dataframe_json_export(anchors_df, anchors_csv, step="trend_extractor_anchors")
        contexts_csv = write_csv(contexts_df, output_dir / "trend_contexts.csv")
        contexts_json = write_dataframe_json_export(contexts_df, contexts_csv, step="trend_extractor_contexts")
        report_csv = write_csv(report_df, output_dir / "trend_timeseries_report.csv")
        report_json = write_dataframe_json_export(report_df, report_csv, step="trend_extractor_report")
        group_csv = write_csv(group_df, output_dir / "trend_group_report.csv")
        group_json = write_dataframe_json_export(group_df, group_csv, step="trend_extractor_group_report")
        dashboard_csv = write_csv(dashboard_df, output_dir / "trend_dashboard_timeseries.csv")
        dashboard_json = write_dataframe_json_export(dashboard_df, dashboard_csv, step="trend_extractor_dashboard")

        # ── keysentence 산출(P2 부산물): 병행 CSV ──
        ks_df = pd.DataFrame(keysentence_rows) if keysentence_rows else pd.DataFrame(
            columns=["week", "keyword", "query_text", "key_sentence", "evidence_doc_ids", "evidence_count"]
        )
        ks_csv_df = ks_df.copy()
        if not ks_csv_df.empty:
            ks_csv_df["evidence_doc_ids"] = ks_csv_df["evidence_doc_ids"].apply(
                lambda v: "|".join(v) if isinstance(v, (list, tuple)) else str(v or "")
            )
        keysentence_csv = write_csv(ks_csv_df, output_dir / "keysentence_summary.csv")
        keysentence_json = write_dataframe_json_export(ks_csv_df, keysentence_csv, step="keysentence")

        # ── DB 적재(주차 단위 replace): keysentence + trend(timeseries/contexts/groups) ──
        cx_db = contexts_df.rename(columns={"summary_or_body": "snippet"}) if not contexts_df.empty else pd.DataFrame(
            columns=["week", "keyword", "doc_id", "score", "snippet"]
        )
        if "snippet" not in cx_db.columns:
            cx_db["snippet"] = ""
        if not group_df.empty:
            gp_db = pd.DataFrame({
                "week": group_df["week"],
                "group_id": group_df["group_id"],
                "members": group_df.apply(
                    lambda r: {
                        "keywords": str(r.get("keywords", "") or "").split("|") if r.get("keywords") else [],
                        "anchor_keywords": str(r.get("anchor_keywords", "") or "").split("|") if r.get("anchor_keywords") else [],
                        "group_summary": str(r.get("group_summary", "") or ""),
                        "group_status": str(r.get("group_status", "") or ""),
                    },
                    axis=1,
                ),
                "group_score": group_df["group_score"],
                "cohesion": group_df.get("cohesion", 0.0),
            })
        else:
            gp_db = pd.DataFrame(columns=["week", "group_id", "members", "group_score", "cohesion"])
        n_ks = repo.write_keysentence(ks_df, weeks)
        n_trend = repo.write_trend(report_df, cx_db, gp_db, weeks)
        logger.info("DB 적재 | keysentence=%d | trend=%s", n_ks, n_trend)

        for label, p in [
            ("OUTPUT_ANCHORS_CSV", anchors_csv),
            ("OUTPUT_CONTEXTS_CSV", contexts_csv),
            ("OUTPUT_REPORT_CSV", report_csv),
            ("OUTPUT_GROUP_REPORT_CSV", group_csv),
            ("OUTPUT_DASHBOARD_CSV", dashboard_csv),
            ("OUTPUT_KEYSENTENCE_CSV", keysentence_csv),
        ]:
            log_artifact(logger, label, p)

        return {
            "anchors_csv": anchors_csv,
            "anchors_json": anchors_json,
            "contexts_csv": contexts_csv,
            "contexts_json": contexts_json,
            "report_csv": report_csv,
            "report_json": report_json,
            "group_report_csv": group_csv,
            "group_report_json": group_json,
            "dashboard_csv": dashboard_csv,
            "dashboard_json": dashboard_json,
            "keysentence_csv": keysentence_csv,
            "keysentence_json": keysentence_json,
            "report_frame": report_df,   # 7단계 product in-memory 입력(transition_from_prev 등 전체 컬럼 보존)
        }

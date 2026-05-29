from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict
from urllib import request
from urllib.error import HTTPError

import pandas as pd

from steps.common import (
    ensure_output_dir,
    get_logger,
    load_config,
    log_artifact,
    log_step,
    read_csv,
    resolve_path,
    write_csv,
    write_dataframe_json_export,
)
from steps.llm_factory import get_llm


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


def _week_in_coverage(week: str, start_week: str, end_week: str) -> bool:
    p = _parse_week_to_ts(week)
    return _parse_week_to_ts(start_week) <= p <= _parse_week_to_ts(end_week)


def _week_to_date_range_yyyymmdd(week: str) -> tuple[str, str]:
    start_ts = _parse_week_to_ts(week)
    end_ts = start_ts + pd.Timedelta(days=6)
    return start_ts.strftime("%Y%m%d"), end_ts.strftime("%Y%m%d")


def _week_yyyymmdd_days(week_start: str, week_end: str) -> list[str]:
    ts0 = pd.Timestamp(f"{week_start[:4]}-{week_start[4:6]}-{week_start[6:8]}")
    ts1 = pd.Timestamp(f"{week_end[:4]}-{week_end[4:6]}-{week_end[6:8]}")
    out: list[str] = []
    d = ts0
    while d <= ts1:
        out.append(d.strftime("%Y%m%d"))
        d = d + pd.Timedelta(days=1)
    return out


def _normalized_yyyymmdd(value: Any) -> str:
    s = re.sub(r"[^0-9]", "", str(value or "").strip())
    if len(s) >= 8:
        return s[:8]
    return ""


def _payload_hits_week_range(
    payload: dict[str, Any],
    *,
    week_start: str,
    week_end: str,
    date_field: str,
    date_start_field: str,
    date_end_field: str,
) -> bool:
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


def _week_server_filter(
    week_start: str,
    week_end: str,
    *,
    date_field: str,
    date_start_field: str,
    date_end_field: str,
    include_file_overlap: bool = True,
) -> dict[str, Any]:
    days = _week_yyyymmdd_days(week_start, week_end)
    date_branch: dict[str, Any] = {"key": date_field, "match": {"any": days}}
    if not include_file_overlap:
        return {"must": [date_branch]}
    week_start_num = int(week_start) if str(week_start).isdigit() else 0
    week_end_num = int(week_end) if str(week_end).isdigit() else 0
    file_branch: dict[str, Any] = {
        "must": [
            {"key": date_start_field, "range": {"lte": week_end_num}},
            {"key": date_end_field, "range": {"gte": week_start_num}},
        ]
    }
    return {"should": [date_branch, file_branch]}


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _normalize_keyword(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", str(text).lower())


def _pick_column(columns: list[str], candidates: list[str]) -> str | None:
    lowered = {c.lower(): c for c in columns}
    for candidate in candidates:
        found = lowered.get(candidate.lower())
        if found:
            return found
    return None


def _coerce_news_date_series(raw: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(raw):
        s_int = pd.to_numeric(raw, errors="coerce").round(0).astype("Int64")
        return pd.to_datetime(s_int.astype(str), format="%Y%m%d", errors="coerce")
    s_str = raw.astype(str).str.strip()
    mask = s_str.str.fullmatch(r"\d{8}")
    out = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")
    if mask.any():
        out.loc[mask] = pd.to_datetime(s_str.loc[mask], format="%Y%m%d", errors="coerce")
    if (~mask).any():
        out.loc[~mask] = pd.to_datetime(s_str.loc[~mask], errors="coerce")
    return out


def _iso_week_label(date_series: pd.Series) -> pd.Series:
    dt = pd.to_datetime(date_series, errors="coerce")
    iso = dt.dt.isocalendar()
    return iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)


def _split_sources(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split("|") if x.strip()]


def _build_fallback_summary(keyword: str, article_rows: list[dict[str, Any]]) -> str:
    if not article_rows:
        return f"{keyword} 관련 핵심 기사 근거를 찾지 못했습니다."
    top_titles = [str(r.get("title") or "").strip() for r in article_rows if str(r.get("title") or "").strip()]
    if not top_titles:
        return f"{keyword} 관련 핵심 기사 근거를 찾지 못했습니다."
    top_titles = top_titles[:2]
    return f"{keyword} 관련 보도에서는 {top_titles[0]}" + (f" 및 {top_titles[1]}" if len(top_titles) > 1 else "") + "가 핵심 이슈로 다뤄졌습니다."


def _build_keyword_prompt(week: str, keyword: str, article_rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, row in enumerate(article_rows, start=1):
        title = _normalize_text(row.get("title") or "")
        body = _normalize_text(row.get("body") or "")
        source = _normalize_text(row.get("source") or "")
        snippet = body[:180]
        lines.append(f"{i}. [{source}] 제목: {title}\n   요약발췌: {snippet}")
    return (
        f"주차: {week}\n"
        f"키워드: {keyword}\n"
        "아래 기사 제목/요약발췌만 근거로, 사실 기반 1~2문장 한국어 요약문을 작성하세요.\n"
        "- 추측/확장 금지\n"
        "- 기사에 없는 숫자/고유명사 생성 금지\n"
        "- 문장은 간결하게 작성\n\n"
        "기사 목록:\n"
        + "\n".join(lines)
    )


def _collect_week_articles_from_excels(
    *,
    logger: Any,
    news_dir: Path,
    excel_pattern: str,
    target_weeks: set[str],
) -> dict[str, list[dict[str, Any]]]:
    week_articles: dict[str, list[dict[str, Any]]] = {w: [] for w in target_weeks}
    files = sorted(news_dir.glob(excel_pattern))
    if not files:
        logger.warning("keysentence_extractor: 뉴스 엑셀 파일이 없습니다. dir=%s pattern=%s", news_dir, excel_pattern)
        return week_articles
    for excel_path in files:
        try:
            df = pd.read_excel(excel_path)
        except Exception as exc:
            logger.warning("keysentence_extractor: 엑셀 읽기 실패 | file=%s | err=%s", excel_path.name, exc)
            continue
        if df.empty:
            continue
        cols = df.columns.tolist()
        date_col = _pick_column(cols, ["date", "날짜", "일자", "published_at", "등록일", "작성일"])
        source_col = _pick_column(cols, ["언론사", "source", "출처", "publisher"])
        title_col = _pick_column(cols, ["title", "제목"])
        body_col = _pick_column(cols, ["body", "summary", "본문", "content", "text", "article", "기사내용"])
        if date_col is None or source_col is None or (title_col is None and body_col is None):
            continue
        selected = [c for c in [date_col, source_col, title_col, body_col] if c is not None]
        tmp = df[selected].copy()
        tmp["__dt"] = _coerce_news_date_series(tmp[date_col])
        tmp = tmp.dropna(subset=["__dt"])
        if tmp.empty:
            continue
        tmp["week"] = _iso_week_label(tmp["__dt"])
        tmp = tmp[tmp["week"].isin(target_weeks)]
        if tmp.empty:
            continue
        for _, row in tmp.iterrows():
            week = str(row["week"])
            if week not in week_articles:
                continue
            week_articles[week].append(
                {
                    "source": str(row.get(source_col) or ""),
                    "title": str(row.get(title_col) or "") if title_col else "",
                    "body": str(row.get(body_col) or "") if body_col else "",
                }
            )
    return week_articles


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
            err_body[:1000],
        )
        return None
    except Exception as exc:
        logger.warning("%s | url=%s | err=%s", log_tag, url, exc)
        return None


def _collect_week_points(
    *,
    logger: Any,
    qdrant_url: str,
    collection: str,
    week: str,
    date_field: str,
    date_start_field: str,
    date_end_field: str,
    timeout_sec: float,
    scroll_limit: int,
    scroll_max_pages: int,
    include_file_overlap: bool,
) -> list[dict[str, Any]]:
    week_start, week_end = _week_to_date_range_yyyymmdd(week)
    qfilter = _week_server_filter(
        week_start,
        week_end,
        date_field=date_field,
        date_start_field=date_start_field,
        date_end_field=date_end_field,
        include_file_overlap=include_file_overlap,
    )
    points: list[dict[str, Any]] = []
    page_payload: dict[str, Any] = {
        "limit": int(max(1, scroll_limit)),
        "with_payload": True,
        "filter": qfilter,
    }
    url = f"{qdrant_url.rstrip('/')}/collections/{collection}/points/scroll"
    max_p = max(1, int(scroll_max_pages))
    for _ in range(max_p):
        data = _post_json_safe(url, page_payload, timeout_sec, logger, "KEYSENT_SCROLL_FAIL")
        if not data:
            break
        result = data.get("result") or {}
        page_points = result.get("points") or []
        if not isinstance(page_points, list):
            break
        points.extend(page_points)
        next_off = result.get("next_page_offset")
        if not next_off or not page_points:
            break
        page_payload = dict(page_payload)
        page_payload["offset"] = next_off
    dedup: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for p in points:
        pid = p.get("id")
        if pid in seen:
            continue
        seen.add(pid)
        payload = p.get("payload") or {}
        if _payload_hits_week_range(
            payload,
            week_start=week_start,
            week_end=week_end,
            date_field=date_field,
            date_start_field=date_start_field,
            date_end_field=date_end_field,
        ):
            dedup.append(p)
    return dedup


def _split_sentences(text: str) -> list[str]:
    raw = _normalize_text(text)
    if not raw:
        return []
    chunks = re.split(r"(?<=[\.\!\?。！？])\s+|\n+", raw)
    out: list[str] = []
    for c in chunks:
        c2 = _normalize_text(c)
        if len(c2) >= 12:
            out.append(c2)
    return out


def _build_extractive_summary(keyword: str, docs: list[dict[str, Any]]) -> tuple[str, list[str]]:
    kw_norm = _normalize_keyword(keyword)
    if not docs:
        return f"{keyword} 관련 핵심 기사 근거를 찾지 못했습니다.", []

    scored: list[tuple[int, int, str, str]] = []
    for idx, d in enumerate(docs):
        doc_id = str(d.get("doc_id") or "")
        title = _normalize_text(d.get("title") or "")
        body = _normalize_text(d.get("summary_or_body") or "")
        sentences = _split_sentences(f"{title}. {body}")
        for sent in sentences[:8]:
            s_norm = _normalize_keyword(sent)
            kw_hits = s_norm.count(kw_norm) if kw_norm else 0
            score = kw_hits * 3 + min(len(sent) // 40, 3)
            if title and sent == title:
                score += 1
            scored.append((score, -idx, sent, doc_id))

    if not scored:
        fallback = docs[0]
        title = _normalize_text(fallback.get("title") or "")
        body = _normalize_text(fallback.get("summary_or_body") or "")[:140]
        base = title if title else body
        return (base or f"{keyword} 관련 기사 요약 생성 실패"), [str(fallback.get("doc_id") or "")]

    scored.sort(key=lambda x: (-x[0], x[1], len(x[2])))
    chosen: list[str] = []
    used_docs: list[str] = []
    seen_text: set[str] = set()
    for _, _, sent, doc_id in scored:
        norm_sent = _normalize_keyword(sent)
        if not norm_sent or norm_sent in seen_text:
            continue
        seen_text.add(norm_sent)
        chosen.append(sent)
        if doc_id and doc_id not in used_docs:
            used_docs.append(doc_id)
        if len(chosen) >= 2:
            break
    summary = " ".join(chosen).strip()
    if not summary:
        summary = f"{keyword} 관련 주요 기사 요약을 생성하지 못했습니다."
    return summary, used_docs


def run_keysentence_extractor(
    high_z_score_csv: Path,
    *,
    weekly_keywords_csv: Path | None = None,
    base_start_week: str | None = None,
    base_end_week: str | None = None,
    test_week_offset: int = 0,
    test_max_weeks: int | None = None,
    test_mode: bool = False,
) -> Dict[str, Path]:
    logger = get_logger("steps.keysentence_extractor")
    with log_step(
        logger,
        5,
        "keysentence_extractor",
        high_z_score_csv=str(high_z_score_csv.resolve()),
        base_start_week=base_start_week or "",
        base_end_week=base_end_week or "",
    ):
        cfg = load_config()
        ks_cfg = cfg.get("keysentence_extractor", {})
        tr_cfg = cfg.get("trend_extractor", {})
        news_cfg = cfg.get("news", {})

        keywords_per_week = int(ks_cfg.get("keywords_per_week", 8))
        max_docs = int(ks_cfg.get("max_docs", 30))
        max_titles_for_prompt = int(ks_cfg.get("max_titles_for_prompt", 25))
        default_news_dir = "/Users/changhokang/Desktop/squadone_news_vectordb/data/news"
        news_excel_dir = Path(str(ks_cfg.get("news_excel_dir", default_news_dir))).expanduser()
        if not news_excel_dir.is_absolute():
            news_excel_dir = resolve_path(str(news_excel_dir))
        if not news_excel_dir.exists():
            parent_news = news_excel_dir.parent / "news"
            if parent_news.exists():
                news_excel_dir = parent_news
        excel_pattern = str(ks_cfg.get("excel_pattern", news_cfg.get("excel_pattern", "NewsResult_*.xlsx")))

        z_df = read_csv(high_z_score_csv)
        if weekly_keywords_csv is None:
            weekly_keywords_csv = high_z_score_csv.parent / "weekly_keywords.csv"
        wk_df = read_csv(weekly_keywords_csv) if weekly_keywords_csv.exists() else pd.DataFrame()
        if z_df.empty:
            raise ValueError("keysentence_extractor 입력(high_z_score_csv)이 비어 있습니다.")
        z_df["week"] = z_df["week"].astype(str)
        z_df["keyword"] = z_df["keyword"].astype(str)
        z_df["z_score"] = pd.to_numeric(z_df.get("z_score"), errors="coerce").fillna(0.0)
        if not wk_df.empty:
            wk_df["week"] = wk_df["week"].astype(str)
            wk_df["keyword"] = wk_df["keyword"].astype(str)
            wk_df["source"] = wk_df.get("source", "").astype(str)

        coverage = tr_cfg.get("vector_date_coverage", ["2016-W01", "2022-W52"])
        coverage_start = str(base_start_week or coverage[0])
        coverage_end = str(base_end_week or coverage[1])

        all_weeks = _sort_week_labels(sorted(z_df["week"].unique().tolist()))
        weeks = [w for w in all_weeks if _week_in_coverage(w, coverage_start, coverage_end)]
        if not weeks:
            raise ValueError(
                f"keysentence_extractor: 커버리지({coverage_start}~{coverage_end})에서 high_z 주차가 없습니다."
            )
        logger.info(
            "keysentence_extractor 주차 범위 | coverage=%s..%s | high_z_weeks=%d (전체 %d)",
            coverage_start,
            coverage_end,
            len(weeks),
            len(all_weeks),
        )

        offset = max(0, int(test_week_offset or 0))
        weeks = weeks[offset:]
        if test_mode and test_max_weeks is None:
            test_max_weeks = 10
        if test_max_weeks is not None:
            weeks = weeks[: max(1, int(test_max_weeks))]
        target_weeks = set(weeks)
        week_articles = _collect_week_articles_from_excels(
            logger=logger,
            news_dir=news_excel_dir,
            excel_pattern=excel_pattern,
            target_weeks=target_weeks,
        )
        llm = get_llm("individual_summary")

        rows: list[dict[str, Any]] = []
        for week in weeks:
            week_df = z_df[z_df["week"] == week].sort_values("z_score", ascending=False).head(keywords_per_week)
            for _, r in week_df.iterrows():
                keyword = str(r["keyword"])
                z_score = float(r.get("z_score", 0.0))
                source_hints: list[str] = []
                if not wk_df.empty:
                    wk_slice = wk_df[(wk_df["week"] == week) & (wk_df["keyword"] == keyword)]
                    source_hints = sorted(set(str(x) for x in wk_slice["source"].tolist() if str(x).strip()))
                src_set = set(source_hints)
                kw_norm = _normalize_keyword(keyword)
                raw_articles = week_articles.get(week, [])
                matched: list[dict[str, Any]] = []
                for art in raw_articles:
                    source = str(art.get("source") or "").strip()
                    if src_set and source not in src_set:
                        continue
                    title = str(art.get("title") or "")
                    body = str(art.get("body") or "")
                    if kw_norm and kw_norm not in _normalize_keyword(f"{title} {body}"):
                        continue
                    matched.append(art)
                if not matched and src_set:
                    matched = [art for art in raw_articles if str(art.get("source") or "").strip() in src_set]
                if not matched:
                    matched = raw_articles[:]
                dedup: list[dict[str, Any]] = []
                seen_key: set[str] = set()
                for art in matched:
                    k = _normalize_keyword(f"{art.get('source','')}|{art.get('title','')}")
                    if not k or k in seen_key:
                        continue
                    seen_key.add(k)
                    dedup.append(art)
                selected_articles = dedup[: max(1, max_docs)]

                prompt_articles = selected_articles[: max(1, max_titles_for_prompt)]
                prompt = _build_keyword_prompt(week, keyword, prompt_articles)
                llm_out = (llm.invoke(prompt) or "").strip()
                key_sentence = llm_out if llm_out else _build_fallback_summary(keyword, selected_articles)
                evidence_titles = [str(a.get("title") or "").strip() for a in selected_articles[:10] if str(a.get("title") or "").strip()]
                rows.append(
                    {
                        "week": week,
                        "keyword": keyword,
                        "z_score": z_score,
                        "query_text": key_sentence,
                        "key_sentence": key_sentence,
                        "evidence_doc_ids": "",
                        "evidence_titles": "|".join(evidence_titles),
                        "source_hints": "|".join(source_hints),
                        "evidence_count": len(selected_articles),
                    }
                )

        out_df = pd.DataFrame(rows)
        output_dir = ensure_output_dir()
        out_csv = write_csv(out_df, output_dir / "keysentence_summary.csv")
        out_json = write_dataframe_json_export(out_df, out_csv, step="keysentence_extractor")
        log_artifact(logger, "OUTPUT_KEYSENTENCE_CSV", out_csv)
        return {"csv": out_csv, "json": out_json}

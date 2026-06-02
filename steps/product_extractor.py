from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

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

from db import repository as repo


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _iso_week_tuple(label: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})-W(\d{2})", str(label).strip())
    if not m:
        raise ValueError(f"Invalid ISO week label: {label}")
    return int(m.group(1)), int(m.group(2))


def _collect_scope_rows(
    report_df: pd.DataFrame,
    *,
    news_keyword: str | None,
    test_mode: bool,
    test_week_offset: int,
    test_max_weeks: Optional[int],
    base_start_week: str | None = None,
    base_end_week: str | None = None,
) -> pd.DataFrame:
    df = report_df.copy()
    if "week" not in df.columns:
        raise ValueError("trend_timeseries_report.csv에 week 컬럼이 없습니다.")
    bs = (base_start_week or "").strip() or None
    be = (base_end_week or "").strip() or None
    if (bs is None) ^ (be is None):
        raise ValueError("base_start_week과 base_end_week는 함께 지정하거나 둘 다 비워 두세요.")
    if bs is not None and be is not None:
        lo, hi = _iso_week_tuple(bs), _iso_week_tuple(be)
        if hi < lo:
            raise ValueError(f"base_end_week({be})가 base_start_week({bs})보다 이전입니다.")
    if news_keyword:
        mask = df["keyword"].astype(str).str.contains(str(news_keyword), case=False, na=False)
        df = df[mask]
    if df.empty:
        return df
    weeks = sorted(df["week"].astype(str).unique().tolist())
    if bs is not None and be is not None:
        lo, hi = _iso_week_tuple(bs), _iso_week_tuple(be)
        weeks = [w for w in weeks if lo <= _iso_week_tuple(w) <= hi]
    else:
        offset = max(0, int(test_week_offset or 0))
        weeks = weeks[offset:]
        if test_mode and test_max_weeks is None:
            test_max_weeks = 10
        if test_max_weeks is not None:
            weeks = weeks[: max(1, int(test_max_weeks))]
    return df[df["week"].astype(str).isin(set(weeks))].copy()


def _build_context_prompt(week: str, items: list[dict[str, Any]]) -> str:
    evidence = []
    for item in items:
        kw = str(item.get("keyword", ""))
        summ = str(item.get("weekly_summary", ""))
        transition = str(item.get("transition_from_prev", ""))
        evidence.append(f"- 키워드:{kw} | 주차요약:{summ} | 변화:{transition}")
    body = "\n".join(evidence[:15])
    return (
        "Role: 당신은 이커머스 전략 수립을 위한 '소비자 행동 분석가'입니다.\n"
        "Task: 주어진 뉴스 트렌드를 바탕으로 소비자의 행동 변화, 심리적 동기, 물리적 제약을 분석하여 컨텍스트 리포트를 작성하세요.\n"
        "Constraints:\n"
        "1) 절대로 특정 상품명/아이템을 언급하지 마세요.\n"
        "2) 특정 인물명/구단명은 '주요 대상', '관련 단체' 등으로 추상화하세요.\n"
        "3) 아래 구조를 반드시 지키세요.\n"
        "[행동 분석]: 이 트렌드와 관련하여 사람들이 새롭게 하거나 더 많이 하는 행동 3가지\n"
        "[심리 분석]: 그 행동을 하는 내면의 동기\n"
        "[제약 분석]: 그 과정에서 겪는 물리적/환경적 불편함\n\n"
        f"주차: {week}\n"
        f"트렌드 근거:\n{body}\n"
    )


def _build_product_prompt(
    week: str,
    context_report: str,
    *,
    max_products: int,
    ip_clean: str,
    logistics: str,
    generic_naming: str,
) -> str:
    return (
        "Role: 당신은 1인 셀러를 위한 '데이터 기반 상품 기획자(MD)'입니다.\n"
        "Task: 전달받은 [컨텍스트 리포트]를 분석하여, 1인 셀러가 소싱/판매 가능한 일반 명사 상품군을 제안하세요.\n"
        "Business Constraints:\n"
        f"1) IP Clean: {ip_clean}\n"
        f"2) Logistics: {logistics}\n"
        f"3) Generic naming: {generic_naming}\n"
        f"Output Format: 추천 상품군 1: [명칭] / 선정 이유: [컨텍스트와의 연결고리] ... 최대 {max_products}개\n\n"
        f"주차: {week}\n"
        f"[컨텍스트 리포트]\n{context_report}\n"
    )


def _extract_products(text: str, max_products: int) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in str(text).splitlines():
        m = re.search(r"추천\s*상품군\s*\d+\s*:\s*(.+?)\s*/\s*선정\s*이유\s*:\s*(.+)", line.strip())
        if m:
            rows.append((m.group(1).strip(), m.group(2).strip()))
    if rows:
        return rows[:max_products]

    # Fallback: bullet-based parsing
    fallback = []
    for line in str(text).splitlines():
        s = line.strip("- ").strip()
        if not s:
            continue
        if ":" in s:
            name, reason = s.split(":", 1)
            fallback.append((name.strip(), reason.strip()))
    return fallback[:max_products]


def run_product_extractor(
    trend_timeseries_csv: Path,
    *,
    test_week_offset: int = 0,
    test_max_weeks: Optional[int] = None,
    test_mode: bool = False,
    news_keyword: str | None = None,
    mode: str = "full",
    context_report_input: Optional[str] = None,
    base_start_week: str | None = None,
    base_end_week: str | None = None,
) -> Dict[str, Path | str]:
    logger = get_logger("steps.product_extractor")
    with log_step(
        logger,
        6,
        "product_extractor",
        trend_timeseries_csv=str(trend_timeseries_csv.resolve()),
        mode=mode,
        test_mode=test_mode,
        test_week_offset=test_week_offset,
        test_max_weeks=test_max_weeks,
        base_start_week=base_start_week,
        base_end_week=base_end_week,
    ):
        cfg = load_config().get("product_extractor", {})
        max_products = int(cfg.get("max_products", 5))
        constraints = cfg.get("constraints", {})
        ip_clean = str(constraints.get("ip_clean", "특정 인물/브랜드/캐릭터 IP가 필요한 상품은 제외"))
        logistics = str(
            constraints.get(
                "logistics",
                "부피 과대/파손 위험/인증 부담이 큰 제품(가구, 유리, 전기/어린이 제품 등)은 배제",
            )
        )
        generic_naming = str(
            constraints.get(
                "generic_naming",
                "도매 사이트에서 검색 가능한 일반 명사 형태만 허용",
            )
        )

        report_df = read_csv(trend_timeseries_csv)
        if report_df.empty:
            raise ValueError("trend_timeseries_report.csv가 비어 있습니다.")
        scoped = _collect_scope_rows(
            report_df,
            news_keyword=news_keyword,
            test_mode=test_mode,
            test_week_offset=test_week_offset,
            test_max_weeks=test_max_weeks,
            base_start_week=base_start_week,
            base_end_week=base_end_week,
        )
        if scoped.empty:
            raise ValueError("조건에 맞는 trend_timeseries 데이터가 없습니다.")

        llm_context = get_llm("context_analyst")
        llm_product = get_llm("product_md")

        context_rows: list[dict[str, Any]] = []
        product_rows: list[dict[str, Any]] = []
        final_rows: list[dict[str, Any]] = []

        context_report_text = context_report_input or ""
        weeks = sorted(scoped["week"].astype(str).unique().tolist())
        for week in weeks:
            week_df = scoped[scoped["week"].astype(str) == week].copy()
            if week_df.empty:
                continue
            items = week_df.to_dict(orient="records")

            if mode in {"full", "context_only"}:
                context_prompt = _build_context_prompt(week, items)
                context_report = llm_context.invoke(context_prompt).strip()
                if not context_report:
                    context_report = "[행동 분석]\n분석 실패\n[심리 분석]\n분석 실패\n[제약 분석]\n분석 실패"
                context_report_text = context_report
                context_rows.append(
                    {
                        "week": week,
                        "news_keyword": news_keyword or "",
                        "context_report": context_report,
                    }
                )

            if mode in {"full", "products_only"}:
                if not context_report_text:
                    raise ValueError("products_only 모드에서는 context_report_input이 필요합니다.")
                product_prompt = _build_product_prompt(
                    week,
                    context_report_text,
                    max_products=max_products,
                    ip_clean=ip_clean,
                    logistics=logistics,
                    generic_naming=generic_naming,
                )
                product_text = llm_product.invoke(product_prompt).strip()
                pairs = _extract_products(product_text, max_products=max_products)
                if not pairs:
                    pairs = [("생활 소형 정리용품", "일상 불편 개선 니즈에 대응 가능한 일반 명사 상품군")]
                for idx, (name, reason) in enumerate(pairs, start=1):
                    product_rows.append(
                        {
                            "week": week,
                            "rank": idx,
                            "product_name": name,
                            "selection_reason": reason,
                            "raw_md_output": product_text,
                        }
                    )
                final_rows.append(
                    {
                        "week": week,
                        "news_keyword": news_keyword or "",
                        "context_report": context_report_text,
                        "final_products": " | ".join(name for name, _ in pairs),
                    }
                )

        output_dir = ensure_output_dir()
        context_df = pd.DataFrame(context_rows)
        product_df = pd.DataFrame(product_rows)
        final_df = pd.DataFrame(final_rows)

        context_csv = write_csv(context_df, output_dir / "product_context_report.csv")
        context_json = write_dataframe_json_export(context_df, context_csv, step="product_extractor_context")
        products_csv = write_csv(product_df, output_dir / "product_candidates.csv")
        products_json = write_dataframe_json_export(product_df, products_csv, step="product_extractor_products")
        final_csv = write_csv(final_df, output_dir / "product_extractor_report.csv")
        final_json = write_dataframe_json_export(final_df, final_csv, step="product_extractor_report")

        # ── DB 적재(주차 단위 replace) + 병행 CSV ──
        proc_weeks = sorted(set(weeks))
        n_prod = repo.write_product(product_df, proc_weeks) if not product_df.empty else 0
        report_rows = []
        for _, r in context_df.iterrows():
            report_rows.append((
                "product_context", str(r["week"]),
                {"news_keyword": str(r.get("news_keyword", "")), "context_report": str(r.get("context_report", ""))},
                None,
            ))
        for _, r in final_df.iterrows():
            report_rows.append((
                "product_report", str(r["week"]),
                {"news_keyword": str(r.get("news_keyword", "")),
                 "context_report": str(r.get("context_report", "")),
                 "final_products": str(r.get("final_products", ""))},
                None,
            ))
        n_rep = repo.write_reports(report_rows)
        logger.info("DB 적재 | product_candidates=%d | reports=%d", n_prod, n_rep)

        for label, p in [
            ("OUTPUT_PRODUCT_CONTEXT_CSV", context_csv),
            ("OUTPUT_PRODUCT_CANDIDATES_CSV", products_csv),
            ("OUTPUT_PRODUCT_REPORT_CSV", final_csv),
        ]:
            log_artifact(logger, label, p)

        return {
            "context_csv": context_csv,
            "context_json": context_json,
            "products_csv": products_csv,
            "products_json": products_json,
            "report_csv": final_csv,
            "report_json": final_json,
            "last_context_report": context_report_text,
        }

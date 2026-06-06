"""통합 오케스트레이터 — 7단계(클러스터링) · 8단계(사입 상품 추출, 다중 옵션).

본선 1~6(tool_news_trend.run_news_trend_pipeline: 1~4 + 6 trend; 5 노이즈분류는 전제)
이후 단계. 모든 입출력은 DB(newstrend 스키마). 각 하위 단계는 독립 try/except 로 graceful.

7단계 클러스터링(군집 구조 + 척추 cluster_keywords):
  7-1 clustering(해석 제외) → clusters, cluster_keywords
  7-2 trend_time_series_builder → trend_ts_cluster

8단계 사입 상품 추출(신호 → 결합 → 옵션 → 검증):
  (가) 신호  8-신호-1 long_term_trend_bridge → long_term_signals
            8-신호-2 period_trend → period_trend_signals
            8-신호-3 cluster_interpretation → cluster_interpretation
  (결합)    v_keyword_enriched / v_cluster_enriched (DB 뷰, 읽기시 자동 결합)
  (나) 옵션  8-1 product_extractor(←6 트렌드 DB) → product_candidates
            8-2 llm_questionarie(6-1/6-2/6-3) → llm_briefs/related_products/youtube_queries
            8-3 geo_query → youtube_signal(VERC) → geo_queries/youtube_signals
  (다) 검증  demand_forecast · market_competition · social_vibe · naver_grounding
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict

from db.config import load_shared_env
from steps.common import get_logger

load_shared_env()
logger = get_logger("enrichment_pipeline")


def _safe(name: str, fn: Callable[[], Any], results: Dict[str, Any]) -> None:
    t0 = time.perf_counter()
    try:
        out = fn()
        results[name] = {"ok": True, "result": out, "sec": round(time.perf_counter() - t0, 2)}
        logger.info("[enrich] %s 완료 (%.1fs): %s", name, time.perf_counter() - t0, out)
    except Exception as exc:  # noqa: BLE001
        results[name] = {"ok": False, "error": str(exc), "sec": round(time.perf_counter() - t0, 2)}
        logger.warning("[enrich] %s 실패: %s", name, exc)


# ── 7단계: 클러스터링 ─────────────────────────────────────────────

def run_stage7_clustering(week: str) -> Dict[str, Any]:
    """7단계: 기사 제목 군집화 + 군집 시계열. (해석은 8-신호-3으로 분리)"""
    from steps import clustering, trend_time_series_builder

    results: Dict[str, Any] = {}
    logger.info("[stage7] 클러스터링 시작 week=%s", week)
    # 7-1: 군집(해석 제외 — run_interpretation=False)
    _safe("7-1_clustering", lambda: clustering.run_clustering(week, run_interpretation=False), results)
    # 7-2: 군집 부피/강도
    _safe("7-2_trend_ts_cluster", lambda: trend_time_series_builder.run_trend_time_series_builder(week), results)
    ok = sum(1 for v in results.values() if v.get("ok"))
    logger.info("[stage7] 완료 week=%s | %d/%d", week, ok, len(results))
    return {"stage": 7, "week": week, "ok": ok, "total": len(results), "detail": results}


# ── 8단계: 사입 상품 추출(다중 옵션) ──────────────────────────────

def run_stage8_sourcing(
    week: str,
    *,
    skip_external: bool = False,
    top_n: int = 30,
    run_product: bool = True,
) -> Dict[str, Any]:
    """8단계: 신호 → 결합(뷰) → 추출 옵션(전부) → 외부 검증/그라운딩.

    skip_external=True 면 (다) demand/market/social/youtube_signal 등 외부 API 단계 생략.
    run_product=False 면 8-1 product_extractor 생략(본선 파이프라인이 이미 수행한 경우).
    """
    from steps import (
        long_term_trend_bridge, period_trend, cluster_interpretation,
        llm_questionarie, geo_query, naver_grounding,
    )
    from db import repository as repo

    results: Dict[str, Any] = {}
    logger.info("[stage8] 사입 추출 시작 week=%s skip_external=%s", week, skip_external)

    # (가) 신호
    _safe("8-신호-1_long_term", lambda: long_term_trend_bridge.run_long_term_trend_bridge(week), results)
    _safe("8-신호-2_period_trend", lambda: period_trend.run_period_trend(week), results)
    _safe("8-신호-3_cluster_interpretation", lambda: cluster_interpretation.run_cluster_interpretation(week), results)

    # (결합) 키워드↔클러스터 결합 뷰는 DB 뷰라 읽기 시 자동 결합. 건수만 확인/로그.
    def _combine_check() -> Dict[str, int]:
        ke = repo.read_keyword_enriched(week)
        ce = repo.read_cluster_enriched(week)
        return {"v_keyword_enriched": len(ke), "v_cluster_enriched": len(ce)}
    _safe("8-결합_enriched_views", _combine_check, results)

    # (나) 추출 옵션 (전부 구현)
    if run_product:
        from steps.product_extractor import run_product_extractor
        _safe("8-1_product_extractor", lambda: _product_summary(run_product_extractor()), results)
    _safe("8-2_llm_questionarie", lambda: llm_questionarie.run_all(week, top_n=top_n), results)
    _safe("8-3a_geo_query", lambda: geo_query.run_geo_query(week), results)
    if not skip_external:
        from steps import youtube_signal
        _safe("8-3b_youtube_signal", lambda: youtube_signal.run_youtube_signal(week), results)

    # (다) 외부 검증·보강
    if not skip_external:
        from steps import demand_forecast, market_competition, social_vibe
        _safe("8-검증-1_demand", lambda: demand_forecast.run_demand_forecast(week), results)
        _safe("8-검증-2_market", lambda: market_competition.run_market_competition(week), results)
        _safe("8-검증-3_social", lambda: social_vibe.run_social_vibe(week), results)
    _safe("8-그라운딩_naver", lambda: naver_grounding.run_naver_grounding(week), results)

    ok = sum(1 for v in results.values() if v.get("ok"))
    logger.info("[stage8] 완료 week=%s | %d/%d", week, ok, len(results))
    return {"stage": 8, "week": week, "ok": ok, "total": len(results), "detail": results}


def _product_summary(out: Dict[str, Any]) -> Dict[str, Any]:
    """product_extractor 반환(파일경로 다수)에서 핵심만 요약(JSON 직렬화 경량화)."""
    return {"products_csv": str(out.get("products_csv", "")), "report_csv": str(out.get("report_csv", ""))}


# ── 통합(7+8) ─────────────────────────────────────────────────────

def run_enrichment_pipeline(
    week: str,
    *,
    skip_external: bool = False,
    top_n: int = 30,
    run_product: bool = True,
) -> Dict[str, Any]:
    """7단계 → 8단계 전체 실행(주차 단위). 모든 입출력 DB."""
    logger.info("[enrich] 7+8 시작 week=%s", week)
    s7 = run_stage7_clustering(week)
    s8 = run_stage8_sourcing(week, skip_external=skip_external, top_n=top_n, run_product=run_product)
    ok = s7["ok"] + s8["ok"]
    total = s7["total"] + s8["total"]
    logger.info("[enrich] 완료 week=%s | 성공 %d/%d", week, ok, total)
    return {"week": week, "steps_ok": ok, "steps_total": total, "stage7": s7, "stage8": s8}


def main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="통합 오케스트레이터(7 클러스터링 + 8 사입추출)")
    p.add_argument("--week", required=True)
    p.add_argument("--stage", choices=["7", "8", "all"], default="all")
    p.add_argument("--skip-external", action="store_true", help="외부 API(YouTube/Naver) 단계 생략")
    p.add_argument("--no-product", action="store_true", help="8-1 product_extractor 생략")
    p.add_argument("--top-n", type=int, default=30)
    args = p.parse_args()
    if args.stage == "7":
        out = run_stage7_clustering(args.week)
    elif args.stage == "8":
        out = run_stage8_sourcing(args.week, skip_external=args.skip_external,
                                  top_n=args.top_n, run_product=not args.no_product)
    else:
        out = run_enrichment_pipeline(args.week, skip_external=args.skip_external,
                                      top_n=args.top_n, run_product=not args.no_product)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()

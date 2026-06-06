"""통합 인리치먼트 오케스트레이터 (Stage5B~7C, 주차 단위).

현행 1~7단계(tool_news_trend.run_news_trend_pipeline) 이후, 통합으로 추가된 고급 분석을
한 번에 실행한다. 각 단계는 독립 try/except 로 graceful — 일부 실패/스킵해도 나머지 진행.

순서:
  5B 클러스터링(+해석) → 군집 시계열 → 장기 지속성 → 주기 신호
  6  LLM 인텔(6-1 브리프 → 6-2 관련상품 → 6-3 유튜브질의)
  7B GEO질의 → YouTube VERC
  7C 수요/경쟁/소셜
  7D Naver 카테고리 그라운딩

전제: 해당 주차의 z_score_keywords 가 이미 적재되어 있어야 한다(1~4단계 또는 trend 실행 후).
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
        logger.info("[enrichment] %s 완료 (%.1fs): %s", name, time.perf_counter() - t0, out)
    except Exception as exc:  # noqa: BLE001
        results[name] = {"ok": False, "error": str(exc), "sec": round(time.perf_counter() - t0, 2)}
        logger.warning("[enrichment] %s 실패: %s", name, exc)


def run_enrichment_pipeline(
    week: str,
    *,
    skip_external: bool = False,
    top_n: int = 30,
) -> Dict[str, Any]:
    """주차 통합 인리치먼트 실행. skip_external=True 면 외부 API(YouTube/Naver) 단계 생략."""
    from steps import (
        clustering, trend_time_series_builder, long_term_trend_bridge, period_trend,
        llm_questionarie, geo_query, naver_grounding,
    )

    results: Dict[str, Any] = {}
    logger.info("[enrichment] 시작 week=%s skip_external=%s", week, skip_external)

    # Stage 5B
    _safe("clustering", lambda: clustering.run_clustering(week), results)
    _safe("trend_time_series", lambda: trend_time_series_builder.run_trend_time_series_builder(week), results)
    _safe("long_term", lambda: long_term_trend_bridge.run_long_term_trend_bridge(week), results)
    _safe("period_trend", lambda: period_trend.run_period_trend(week), results)

    # Stage 6
    _safe("llm_intel", lambda: llm_questionarie.run_all(week, top_n=top_n), results)

    # Stage 7B
    _safe("geo_query", lambda: geo_query.run_geo_query(week), results)
    if not skip_external:
        from steps import youtube_signal, demand_forecast, market_competition, social_vibe
        _safe("youtube_signal", lambda: youtube_signal.run_youtube_signal(week), results)
        _safe("demand_forecast", lambda: demand_forecast.run_demand_forecast(week), results)
        _safe("market_competition", lambda: market_competition.run_market_competition(week), results)
        _safe("social_vibe", lambda: social_vibe.run_social_vibe(week), results)

    # Stage 7D
    _safe("naver_grounding", lambda: naver_grounding.run_naver_grounding(week), results)

    ok = sum(1 for v in results.values() if v.get("ok"))
    logger.info("[enrichment] 완료 week=%s | 성공 %d/%d 단계", week, ok, len(results))
    return {"week": week, "steps_ok": ok, "steps_total": len(results), "detail": results}


def main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="통합 인리치먼트 오케스트레이터(Stage5B~7D)")
    p.add_argument("--week", required=True)
    p.add_argument("--skip-external", action="store_true", help="외부 API(YouTube/Naver) 단계 생략")
    p.add_argument("--top-n", type=int, default=30)
    args = p.parse_args()
    out = run_enrichment_pipeline(args.week, skip_external=args.skip_external, top_n=args.top_n)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()

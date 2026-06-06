from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from db import repository as repo
from db.config import load_shared_env
from steps.base_calculation import run_base_calculation
from steps.common import configure_logging, ensure_output_dir, get_logger, write_json
from steps.frequency_matrix import run_frequency_matrix
from steps.keyword_extractor import run_keyword_extractor
from steps.product_extractor import run_product_extractor
from steps.trend_extractor import run_trend_extractor
from steps.z_score_filtering import run_z_score_filtering

# 프로젝트 루트 .env를 1회 로딩(멱등) — DB/LLM/임베딩/로깅 env를 일관 적용.
load_shared_env()

try:
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover
    FastMCP = None


def run_news_trend_pipeline(
    target_week: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    test_week_offset: int = 0,
    test_max_weeks: Optional[int] = None,
    test_mode: bool = False,
    news_keyword: Optional[str] = None,
    base_start_week: Optional[str] = None,
    base_end_week: Optional[str] = None,
) -> Dict[str, Any]:
    """뉴스 트렌드 파이프라인 1~6단계(키워드 추출 → 빈도 행렬 → 베이스 계산 → z-score → trend_extractor → product_extractor)."""
    logger = get_logger("tool_news_trend")
    started = time.perf_counter()
    if target_week is not None:
        logger.info("target_week는 1~4 파이프라인에서 사용하지 않습니다(무시). | target_week=%s", target_week)
    logger.info(
        "뉴스 트렌드 파이프라인 시작 | start_date=%s | end_date=%s | test_mode=%s | test_week_offset=%s | test_max_weeks=%s | news_keyword=%s",
        start_date,
        end_date,
        test_mode,
        test_week_offset,
        test_max_weeks,
        news_keyword,
    )

    # 1~4단계는 DB(newstrend 스키마)를 단일 권위 소스로 전달한다: 1→weekly_keywords,
    # 2→weekly_keyword_freq, 3→base_calculation(dense), 4→z_score_keywords.
    # SQUADONE_WRITE_FILES=false 여도 전체 파이프라인이 동작한다(병행 CSV는 디버깅용).
    logger.info("[단계 1/4] keyword_extractor 호출")
    weekly_keywords = run_keyword_extractor(start_date=start_date, end_date=end_date)
    logger.info("[단계 1/4] 완료 | weeks=%d", len(weekly_keywords.get("weeks", [])))

    logger.info("[단계 2/4] frequency_matrix 호출 | weeks(in-memory)=%d", len(weekly_keywords.get("weeks", [])))
    frequency_matrix = run_frequency_matrix(weeks=weekly_keywords["weeks"])
    logger.info("[단계 2/4] 완료 | freq_table=%s | recomputed_weeks=%s",
                frequency_matrix["freq_table"], frequency_matrix["recomputed_weeks"])

    logger.info("[단계 3/4] base_calculation 호출 | input=newstrend.weekly_keyword_freq(DB)")
    base_calculation = run_base_calculation()
    logger.info("[단계 3/4] 완료 | db_rows=%s", base_calculation.get("db_rows"))

    logger.info("[단계 4/4] z_score_filtering 호출 | base=newstrend.base_calculation(DB)")
    z_score = run_z_score_filtering()
    logger.info("[단계 4/4] 완료 | rows=%d", len(z_score["frame"]))

    # P2: 5단계(keysentence)는 6단계(trend_extractor)에 흡수됨(2-phase 벡터검색). 별도 호출 없음.
    # weekly counts(week,keyword,count)는 step2가 갱신한 DB weekly_keyword_freq 에서 읽는다.
    weekly_freq_df = repo.read_weekly_freq()
    logger.info("[단계 5+6] trend_extractor 호출 (keysentence 통합) | weekly_freq_rows=%d", len(weekly_freq_df))
    trend_outputs = run_trend_extractor(
        z_score_df=z_score["frame"],
        weekly_df=weekly_freq_df,
        base_start_week=base_start_week,
        base_end_week=base_end_week,
        test_week_offset=test_week_offset,
        test_max_weeks=test_max_weeks,
        test_mode=test_mode,
    )
    keysentence_outputs = {
        "csv": Path(trend_outputs["keysentence_csv"]),
        "json": Path(trend_outputs["keysentence_json"]),
    }
    logger.info("[단계 5+6] 완료 | report_csv=%s | keysentence_csv=%s",
                trend_outputs["report_csv"], trend_outputs["keysentence_csv"])

    logger.info("[단계 7/7] product_extractor 호출 | trend=in-memory frame")
    product_outputs = run_product_extractor(
        report_df=trend_outputs["report_frame"],
        test_week_offset=test_week_offset,
        test_max_weeks=test_max_weeks,
        test_mode=test_mode,
        news_keyword=news_keyword,
        base_start_week=base_start_week,
        base_end_week=base_end_week,
    )
    logger.info("[단계 7/7] 완료 | report_csv=%s", product_outputs["report_csv"])

    output_dir = ensure_output_dir()
    summary_path = output_dir / "pipeline_summary.json"
    summary_payload: Dict[str, Any] = {
        "status": "success",
        "target_week": target_week,
        "start_date": start_date,
        "end_date": end_date,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "outputs": {
            "weekly_keywords": {"csv": str(weekly_keywords["csv"]), "json": str(weekly_keywords["json"])},
            "frequency_matrix": {"table": frequency_matrix["freq_table"], "recomputed_weeks": frequency_matrix["recomputed_weeks"]},
            "base_calculation": {"csv": str(base_calculation["csv"]), "json": str(base_calculation["json"])},
            "z_score_keywords": {"csv": str(z_score["csv"]), "json": str(z_score["json"])},
            "z_score_keywords_high": {
                "csv": str(z_score["high_csv"]),
                "json": str(z_score["high_json"]),
            },
            "keysentence_summary": {
                "csv": str(keysentence_outputs["csv"]),
                "json": str(keysentence_outputs["json"]),
            },
            "trend_anchors": {"csv": str(trend_outputs["anchors_csv"]), "json": str(trend_outputs["anchors_json"])},
            "trend_contexts": {
                "csv": str(trend_outputs["contexts_csv"]),
                "json": str(trend_outputs["contexts_json"]),
            },
            "trend_timeseries_report": {
                "csv": str(trend_outputs["report_csv"]),
                "json": str(trend_outputs["report_json"]),
            },
            "trend_group_report": {
                "csv": str(trend_outputs["group_report_csv"]),
                "json": str(trend_outputs["group_report_json"]),
            },
            "trend_dashboard_timeseries": {
                "csv": str(trend_outputs["dashboard_csv"]),
                "json": str(trend_outputs["dashboard_json"]),
            },
            "product_context_report": {
                "csv": str(product_outputs["context_csv"]),
                "json": str(product_outputs["context_json"]),
            },
            "product_candidates": {
                "csv": str(product_outputs["products_csv"]),
                "json": str(product_outputs["products_json"]),
            },
            "product_extractor_report": {
                "csv": str(product_outputs["report_csv"]),
                "json": str(product_outputs["report_json"]),
            },
        },
    }
    elapsed = round(time.perf_counter() - started, 3)
    summary_payload["elapsed_seconds"] = elapsed
    write_json(summary_payload, summary_path, indent=2)

    result = {
        "status": "success",
        "outputs": {
            "weekly_keywords_csv": str(weekly_keywords["csv"]),
            "weekly_keywords_json": str(weekly_keywords["json"]),
            "frequency_matrix_table": frequency_matrix["freq_table"],
            "base_calculation_csv": str(base_calculation["csv"]),
            "base_calculation_json": str(base_calculation["json"]),
            "z_score_keywords_csv": str(z_score["csv"]),
            "z_score_keywords_json": str(z_score["json"]),
            "z_score_keywords_high_csv": str(z_score["high_csv"]),
            "z_score_keywords_high_json": str(z_score["high_json"]),
            "keysentence_summary_csv": str(keysentence_outputs["csv"]),
            "keysentence_summary_json": str(keysentence_outputs["json"]),
            "trend_anchors_csv": str(trend_outputs["anchors_csv"]),
            "trend_anchors_json": str(trend_outputs["anchors_json"]),
            "trend_contexts_csv": str(trend_outputs["contexts_csv"]),
            "trend_contexts_json": str(trend_outputs["contexts_json"]),
            "trend_timeseries_report_csv": str(trend_outputs["report_csv"]),
            "trend_timeseries_report_json": str(trend_outputs["report_json"]),
            "trend_group_report_csv": str(trend_outputs["group_report_csv"]),
            "trend_group_report_json": str(trend_outputs["group_report_json"]),
            "trend_dashboard_timeseries_csv": str(trend_outputs["dashboard_csv"]),
            "trend_dashboard_timeseries_json": str(trend_outputs["dashboard_json"]),
            "product_context_report_csv": str(product_outputs["context_csv"]),
            "product_context_report_json": str(product_outputs["context_json"]),
            "product_candidates_csv": str(product_outputs["products_csv"]),
            "product_candidates_json": str(product_outputs["products_json"]),
            "product_extractor_report_csv": str(product_outputs["report_csv"]),
            "product_extractor_report_json": str(product_outputs["report_json"]),
            "pipeline_summary_json": str(summary_path),
        },
        "params": {
            "target_week": target_week,
            "start_date": start_date,
            "end_date": end_date,
            "test_week_offset": test_week_offset,
            "test_max_weeks": test_max_weeks,
            "test_mode": test_mode,
            "news_keyword": news_keyword,
            "base_start_week": base_start_week,
            "base_end_week": base_end_week,
        },
    }
    logger.info("뉴스 트렌드 파이프라인 완료 | %.2fs", elapsed)
    return result


if FastMCP is not None:
    configure_logging(
        os.environ.get("MCP_LOG_LEVEL", "INFO"),
        os.environ.get("MCP_LOG_FILE") or None,
        stream=os.environ.get("MCP_LOG_STREAM", "stderr"),
        replace_root_handlers=os.environ.get("MCP_LOG_REPLACE_ROOT", "1").lower() in ("1", "true", "yes"),
    )
    mcp = FastMCP("tool_news_trend")
    _mcp_logger = get_logger("tool_news_trend.mcp")

    @mcp.tool(name="run_news_trend")
    def run_news_trend(
        target_week: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        test_week_offset: int = 0,
        test_max_weeks: Optional[int] = None,
        test_mode: bool = False,
        news_keyword: Optional[str] = None,
        base_start_week: Optional[str] = None,
        base_end_week: Optional[str] = None,
    ) -> Dict[str, Any]:
        """뉴스 트렌드 분석 파이프라인(1~6단계) 실행."""
        _mcp_logger.info(
            "MCP 도구 run_news_trend | START | target_week=%s | start_date=%s | end_date=%s | test_mode=%s | test_week_offset=%s | test_max_weeks=%s | news_keyword=%s",
            target_week,
            start_date,
            end_date,
            test_mode,
            test_week_offset,
            test_max_weeks,
            news_keyword,
        )
        t0 = time.perf_counter()
        try:
            out = run_news_trend_pipeline(
                target_week=target_week,
                start_date=start_date,
                end_date=end_date,
                test_week_offset=test_week_offset,
                test_max_weeks=test_max_weeks,
                test_mode=test_mode,
                news_keyword=news_keyword,
                base_start_week=base_start_week,
                base_end_week=base_end_week,
            )
            _mcp_logger.info(
                "MCP 도구 run_news_trend | END | elapsed=%.2fs | status=%s",
                time.perf_counter() - t0,
                out.get("status"),
            )
            return out
        except Exception:
            _mcp_logger.exception(
                "MCP 도구 run_news_trend | FAIL | elapsed=%.2fs",
                time.perf_counter() - t0,
            )
            raise

    @mcp.tool(name="run_product_extractor")
    def run_product_extractor_tool(
        trend_timeseries_csv: Optional[str] = None,
        news_keyword: Optional[str] = None,
        base_start_week: Optional[str] = None,
        base_end_week: Optional[str] = None,
        test_week_offset: int = 0,
        test_max_weeks: Optional[int] = None,
        test_mode: bool = False,
        mode: str = "full",
        context_report_input: Optional[str] = None,
    ) -> Dict[str, Any]:
        """상품 추출 도구 단독 실행(context 분석 + 상품군 추출)."""
        _mcp_logger.info(
            "MCP 도구 run_product_extractor | START | trend_timeseries_csv=%s | news_keyword=%s | base_start_week=%s | base_end_week=%s | mode=%s | test_mode=%s | test_week_offset=%s | test_max_weeks=%s",
            trend_timeseries_csv,
            news_keyword,
            base_start_week,
            base_end_week,
            mode,
            test_mode,
            test_week_offset,
            test_max_weeks,
        )
        t0 = time.perf_counter()
        try:
            csv_path = Path(trend_timeseries_csv) if trend_timeseries_csv else ensure_output_dir() / "trend_timeseries_report.csv"
            if not csv_path.is_absolute():
                csv_path = (Path(__file__).resolve().parent / csv_path).resolve()
            out = run_product_extractor(
                csv_path,
                news_keyword=news_keyword,
                test_week_offset=test_week_offset,
                test_max_weeks=test_max_weeks,
                test_mode=test_mode,
                mode=mode,
                context_report_input=context_report_input,
                base_start_week=base_start_week,
                base_end_week=base_end_week,
            )
            _mcp_logger.info(
                "MCP 도구 run_product_extractor | END | elapsed=%.2fs | report_csv=%s",
                time.perf_counter() - t0,
                out.get("report_csv"),
            )
            return {
                "status": "success",
                "outputs": {
                    "product_context_report_csv": str(out["context_csv"]),
                    "product_context_report_json": str(out["context_json"]),
                    "product_candidates_csv": str(out["products_csv"]),
                    "product_candidates_json": str(out["products_json"]),
                    "product_extractor_report_csv": str(out["report_csv"]),
                    "product_extractor_report_json": str(out["report_json"]),
                },
            }
        except Exception:
            _mcp_logger.exception(
                "MCP 도구 run_product_extractor | FAIL | elapsed=%.2fs",
                time.perf_counter() - t0,
            )
            raise


if FastMCP is not None:

    @mcp.tool(name="run_enrichment")
    def run_enrichment_tool(
        week: str,
        skip_external: bool = False,
        top_n: int = 30,
    ) -> Dict[str, Any]:
        """통합 인리치먼트(Stage5B~7D) 실행: 군집·해석·장기/주기 신호·LLM 인텔(6-1/2/3)·
        GEO/유튜브 VERC·수요/경쟁/소셜·Naver 그라운딩. 전제: 해당 주차 z_score 적재 완료."""
        from steps.enrichment_pipeline import run_enrichment_pipeline

        _mcp_logger.info("MCP run_enrichment | START | week=%s | skip_external=%s", week, skip_external)
        t0 = time.perf_counter()
        try:
            out = run_enrichment_pipeline(week, skip_external=skip_external, top_n=top_n)
            _mcp_logger.info("MCP run_enrichment | END | %.2fs | ok=%s/%s",
                             time.perf_counter() - t0, out.get("steps_ok"), out.get("steps_total"))
            return out
        except Exception:
            _mcp_logger.exception("MCP run_enrichment | FAIL | %.2fs", time.perf_counter() - t0)
            raise


if __name__ == "__main__":
    if FastMCP is None:
        raise RuntimeError("fastmcp 패키지가 설치되어 있지 않습니다.")
    mcp.run()

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from steps.base_calculation import run_base_calculation
from steps.clustering import run_clustering
from steps.common import configure_logging, get_logger
from steps.frequency_matrix import run_frequency_matrix
from steps.keyword_extractor import run_keyword_extractor
from steps.z_score_filtering import run_z_score_filtering

try:
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover
    FastMCP = None


def run_news_trend_pipeline(target_week: Optional[str] = None) -> Dict[str, Any]:
    logger = get_logger("tool_news_trend")
    started = time.perf_counter()
    logger.info("뉴스 트렌드 파이프라인 시작 | target_week=%s", target_week)

    weekly_keywords_path = run_keyword_extractor()
    frequency_matrix_path = run_frequency_matrix(weekly_keywords_path)
    base_calculation_path = run_base_calculation(frequency_matrix_path)
    z_score_path = run_z_score_filtering(base_calculation_path, weekly_keywords_path)
    clustered_path = run_clustering(z_score_path, target_week=target_week)

    result = {
        "status": "success",
        "outputs": {
            "weekly_keywords_csv": str(weekly_keywords_path),
            "frequency_matrix_csv": str(frequency_matrix_path),
            "base_calculation_csv": str(base_calculation_path),
            "z_score_keywords_csv": str(z_score_path),
            "clustered_keywords_csv": str(clustered_path),
        },
    }
    logger.info("뉴스 트렌드 파이프라인 완료 | %.2fs", time.perf_counter() - started)
    return result


if FastMCP is not None:
    configure_logging("INFO")
    mcp = FastMCP("tool_news_trend")

    @mcp.tool(name="run_news_trend")
    def run_news_trend(target_week: Optional[str] = None) -> Dict[str, Any]:
        """10년 뉴스 트렌드 분석 파이프라인 실행."""
        return run_news_trend_pipeline(target_week=target_week)


if __name__ == "__main__":
    if FastMCP is None:
        raise RuntimeError("fastmcp 패키지가 설치되어 있지 않습니다.")
    mcp.run()

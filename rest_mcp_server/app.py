from __future__ import annotations

import os
import sys
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

# Ensure repo root is on path (uvicorn may cwd elsewhere)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from db.config import load_shared_env

# 프로젝트 루트 .env를 1회 로딩(멱등) — DB/LLM/임베딩/로깅 env를 일관 적용.
load_shared_env()

from steps.common import configure_logging, ensure_output_dir, get_logger

_LOG = get_logger("rest_mcp_server")


@lru_cache(maxsize=1)
def _step_fns() -> dict[str, Any]:
    """
    Heavy deps(sklearn/scipy 등)를 앱 시작 시점이 아니라 첫 요청 시점에 로드한다.
    """
    return {
        "run_keyword_extractor": import_module("steps.keyword_extractor").run_keyword_extractor,
        "run_frequency_matrix": import_module("steps.frequency_matrix").run_frequency_matrix,
        "run_base_calculation": import_module("steps.base_calculation").run_base_calculation,
        "run_z_score_filtering": import_module("steps.z_score_filtering").run_z_score_filtering,
        "run_trend_extractor": import_module("steps.trend_extractor").run_trend_extractor,
        "run_product_extractor": import_module("steps.product_extractor").run_product_extractor,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """uvicorn 루트 로거를 건드리지 않고(steps 로그 전파), 필요 시 콘솔 핸들러만 보강합니다."""
    level = os.environ.get("REST_LOG_LEVEL", "INFO")
    log_file = os.environ.get("REST_LOG_FILE") or None
    stream = os.environ.get("REST_LOG_STREAM", "stderr")
    configure_logging(level, log_file, stream=stream, replace_root_handlers=False)
    _LOG.info(
        "NewsTrend REST 준비 완료 | project_root=%s | REST_LOG_LEVEL=%s",
        _ROOT,
        level,
    )
    yield
    _LOG.info("NewsTrend REST 종료")


app = FastAPI(title="NewsTrend REST Tools", version="1.0.0", lifespan=lifespan)


def _tool_result(tool_name: str, ok: bool, payload: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> Dict[str, Any]:
    return {
        "tool_name": tool_name,
        "ok": ok,
        "payload": payload or {},
        "error": error,
    }


def verify_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    expected = os.environ.get("REST_MCP_API_KEY", "").strip()
    if not expected:
        return
    if not x_api_key or x_api_key.strip() != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


class KeywordExtractorBody(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    use_feature_column_mode: Optional[bool] = None
    category_filter_values: Optional[list[str]] = None


class PathInput(BaseModel):
    csv_path: Optional[str] = Field(default=None, description="Absolute or project-relative path to input CSV")


class ZScoreBody(BaseModel):
    base_calculation_csv: Optional[str] = None
    weekly_keywords_csv: Optional[str] = None


class TrendExtractorBody(BaseModel):
    z_score_csv: Optional[str] = None
    high_z_score_csv: Optional[str] = None
    weekly_keywords_csv: Optional[str] = None
    base_start_week: Optional[str] = None
    base_end_week: Optional[str] = None
    test_week_offset: int = 0
    test_max_weeks: Optional[int] = None
    test_mode: bool = False


class ProductExtractorBody(BaseModel):
    trend_timeseries_csv: Optional[str] = None
    news_keyword: Optional[str] = None
    base_start_week: Optional[str] = None
    base_end_week: Optional[str] = None
    test_week_offset: int = 0
    test_max_weeks: Optional[int] = None
    test_mode: bool = False
    mode: str = "full"
    context_report_input: Optional[str] = None


@app.get("/health")
def health() -> Dict[str, str]:
    _LOG.debug("GET /health")
    return {"status": "ok"}


@app.post("/v1/tools/news_trend.keyword_extractor")
def tool_keyword_extractor(body: KeywordExtractorBody, _: None = Depends(verify_api_key)) -> Dict[str, Any]:
    name = "news_trend.keyword_extractor"
    params = body.model_dump(exclude_none=True)
    cats = params.get("category_filter_values")
    if isinstance(cats, list) and len(cats) > 8:
        params = {**params, "category_filter_values": cats[:8] + [f"...(+{len(cats) - 8})"]}
    _LOG.info("TOOL %s | START | params=%s", name, params)
    t0 = time.perf_counter()
    try:
        out = _step_fns()["run_keyword_extractor"](
            start_date=body.start_date,
            end_date=body.end_date,
            use_feature_column_mode=body.use_feature_column_mode,
            category_filter_values=body.category_filter_values,
        )
        elapsed = time.perf_counter() - t0
        payload = {
            "weekly_keywords_csv": str(out["csv"]),
            "weekly_keywords_json": str(out["json"]),
        }
        _LOG.info(
            "TOOL %s | OK | elapsed=%.2fs | weekly_keywords_csv=%s",
            name,
            elapsed,
            payload["weekly_keywords_csv"],
        )
        return _tool_result(name, True, payload)
    except Exception as exc:
        _LOG.exception("TOOL %s | FAIL | elapsed=%.2fs | error=%s", name, time.perf_counter() - t0, exc)
        return _tool_result(name, False, {}, str(exc))


@app.post("/v1/tools/news_trend.frequency_matrix")
def tool_frequency_matrix(body: PathInput, _: None = Depends(verify_api_key)) -> Dict[str, Any]:
    name = "news_trend.frequency_matrix"
    _LOG.info("TOOL %s | START | csv_path=%s", name, body.csv_path)
    t0 = time.perf_counter()
    try:
        p = Path(body.csv_path) if body.csv_path else ensure_output_dir() / "weekly_keywords.csv"
        if not p.is_absolute():
            p = _ROOT / p
        if not p.exists():
            _LOG.warning("TOOL %s | FAIL | file not found | path=%s", name, p)
            return _tool_result(name, False, {}, f"File not found: {p}")
        out = _step_fns()["run_frequency_matrix"](p)
        elapsed = time.perf_counter() - t0
        payload = {
            "frequency_matrix_table": out["freq_table"],
            "recomputed_weeks": out["recomputed_weeks"],
        }
        _LOG.info("TOOL %s | OK | elapsed=%.2fs | table=%s", name, elapsed, payload["frequency_matrix_table"])
        return _tool_result(name, True, payload)
    except Exception as exc:
        _LOG.exception("TOOL %s | FAIL | elapsed=%.2fs", name, time.perf_counter() - t0)
        return _tool_result(name, False, {}, str(exc))


@app.post("/v1/tools/news_trend.base_calculation")
def tool_base_calculation(body: PathInput, _: None = Depends(verify_api_key)) -> Dict[str, Any]:
    name = "news_trend.base_calculation"
    _LOG.info("TOOL %s | START | csv_path=%s", name, body.csv_path)
    t0 = time.perf_counter()
    try:
        p = Path(body.csv_path) if body.csv_path else ensure_output_dir() / "frequency_matrix.csv"
        if not p.is_absolute():
            p = _ROOT / p
        if not p.exists():
            _LOG.warning("TOOL %s | FAIL | file not found | path=%s", name, p)
            return _tool_result(name, False, {}, f"File not found: {p}")
        out = _step_fns()["run_base_calculation"](p)
        elapsed = time.perf_counter() - t0
        payload = {
            "base_calculation_csv": str(out["csv"]),
            "base_calculation_json": str(out["json"]),
        }
        _LOG.info("TOOL %s | OK | elapsed=%.2fs | out_csv=%s", name, elapsed, payload["base_calculation_csv"])
        return _tool_result(name, True, payload)
    except Exception as exc:
        _LOG.exception("TOOL %s | FAIL | elapsed=%.2fs", name, time.perf_counter() - t0)
        return _tool_result(name, False, {}, str(exc))


@app.post("/v1/tools/news_trend.z_score_filtering")
def tool_z_score(body: ZScoreBody, _: None = Depends(verify_api_key)) -> Dict[str, Any]:
    name = "news_trend.z_score_filtering"
    _LOG.info(
        "TOOL %s | START | base_calculation_csv=%s | weekly_keywords_csv=%s",
        name,
        body.base_calculation_csv,
        body.weekly_keywords_csv,
    )
    t0 = time.perf_counter()
    try:
        out_dir = ensure_output_dir()
        base_p = Path(body.base_calculation_csv) if body.base_calculation_csv else out_dir / "base_calculation.csv"
        weekly_p = Path(body.weekly_keywords_csv) if body.weekly_keywords_csv else out_dir / "weekly_keywords.csv"
        if not base_p.is_absolute():
            base_p = _ROOT / base_p
        if not weekly_p.is_absolute():
            weekly_p = _ROOT / weekly_p
        if not base_p.exists():
            _LOG.warning("TOOL %s | FAIL | file not found | path=%s", name, base_p)
            return _tool_result(name, False, {}, f"File not found: {base_p}")
        if not weekly_p.exists():
            _LOG.warning("TOOL %s | FAIL | file not found | path=%s", name, weekly_p)
            return _tool_result(name, False, {}, f"File not found: {weekly_p}")
        bundle = {"csv": weekly_p, "json": weekly_p.with_suffix(".json")}
        out = _step_fns()["run_z_score_filtering"](base_p, bundle)
        elapsed = time.perf_counter() - t0
        payload = {
            "z_score_keywords_csv": str(out["csv"]),
            "z_score_keywords_json": str(out["json"]),
            "z_score_keywords_high_csv": str(out["high_csv"]),
            "z_score_keywords_high_json": str(out["high_json"]),
        }
        _LOG.info(
            "TOOL %s | OK | elapsed=%.2fs | z_score_keywords_csv=%s",
            name,
            elapsed,
            payload["z_score_keywords_csv"],
        )
        return _tool_result(name, True, payload)
    except Exception as exc:
        _LOG.exception("TOOL %s | FAIL | elapsed=%.2fs", name, time.perf_counter() - t0)
        return _tool_result(name, False, {}, str(exc))


@app.post("/v1/tools/news_trend.trend_extractor")
def tool_trend_extractor(body: TrendExtractorBody, _: None = Depends(verify_api_key)) -> Dict[str, Any]:
    name = "news_trend.trend_extractor"
    _LOG.info(
        "TOOL %s | START | z_score_csv=%s | high_z_score_csv=%s | weekly_keywords_csv=%s | test_mode=%s | test_week_offset=%s | test_max_weeks=%s",
        name,
        body.z_score_csv,
        body.high_z_score_csv,
        body.weekly_keywords_csv,
        body.test_mode,
        body.test_week_offset,
        body.test_max_weeks,
    )
    t0 = time.perf_counter()
    try:
        out_dir = ensure_output_dir()
        z_score_p = Path(body.z_score_csv) if body.z_score_csv else out_dir / "z_score_keywords.csv"
        high_p = Path(body.high_z_score_csv) if body.high_z_score_csv else out_dir / "z_score_keywords_2.0.csv"
        weekly_p = Path(body.weekly_keywords_csv) if body.weekly_keywords_csv else out_dir / "weekly_keywords.csv"
        if not z_score_p.is_absolute():
            z_score_p = _ROOT / z_score_p
        if not high_p.is_absolute():
            high_p = _ROOT / high_p
        if not weekly_p.is_absolute():
            weekly_p = _ROOT / weekly_p
        for p in [z_score_p, high_p, weekly_p]:
            if not p.exists():
                _LOG.warning("TOOL %s | FAIL | file not found | path=%s", name, p)
                return _tool_result(name, False, {}, f"File not found: {p}")

        out = _step_fns()["run_trend_extractor"](
            z_score_p,
            high_p,
            weekly_p,
            base_start_week=body.base_start_week,
            base_end_week=body.base_end_week,
            test_week_offset=body.test_week_offset,
            test_max_weeks=body.test_max_weeks,
            test_mode=body.test_mode,
        )
        elapsed = time.perf_counter() - t0
        payload = {
            "trend_anchors_csv": str(out["anchors_csv"]),
            "trend_anchors_json": str(out["anchors_json"]),
            "trend_contexts_csv": str(out["contexts_csv"]),
            "trend_contexts_json": str(out["contexts_json"]),
            "trend_timeseries_report_csv": str(out["report_csv"]),
            "trend_timeseries_report_json": str(out["report_json"]),
            "trend_group_report_csv": str(out.get("group_report_csv", "")),
            "trend_group_report_json": str(out.get("group_report_json", "")),
            "trend_dashboard_timeseries_csv": str(out["dashboard_csv"]),
            "trend_dashboard_timeseries_json": str(out["dashboard_json"]),
        }
        _LOG.info(
            "TOOL %s | OK | elapsed=%.2fs | trend_timeseries_report_csv=%s",
            name,
            elapsed,
            payload["trend_timeseries_report_csv"],
        )
        return _tool_result(name, True, payload)
    except Exception as exc:
        _LOG.exception("TOOL %s | FAIL | elapsed=%.2fs", name, time.perf_counter() - t0)
        return _tool_result(name, False, {}, str(exc))


@app.post("/v1/tools/news_trend.product_extractor")
def tool_product_extractor(body: ProductExtractorBody, _: None = Depends(verify_api_key)) -> Dict[str, Any]:
    name = "news_trend.product_extractor"
    _LOG.info(
        "TOOL %s | START | trend_timeseries_csv=%s | news_keyword=%s | base_start_week=%s | base_end_week=%s | mode=%s | test_mode=%s | test_week_offset=%s | test_max_weeks=%s",
        name,
        body.trend_timeseries_csv,
        body.news_keyword,
        body.base_start_week,
        body.base_end_week,
        body.mode,
        body.test_mode,
        body.test_week_offset,
        body.test_max_weeks,
    )
    t0 = time.perf_counter()
    try:
        out_dir = ensure_output_dir()
        trend_p = Path(body.trend_timeseries_csv) if body.trend_timeseries_csv else out_dir / "trend_timeseries_report.csv"
        if not trend_p.is_absolute():
            trend_p = _ROOT / trend_p
        if not trend_p.exists():
            _LOG.warning("TOOL %s | FAIL | file not found | path=%s", name, trend_p)
            return _tool_result(name, False, {}, f"File not found: {trend_p}")

        out = _step_fns()["run_product_extractor"](
            trend_p,
            test_week_offset=body.test_week_offset,
            test_max_weeks=body.test_max_weeks,
            test_mode=body.test_mode,
            news_keyword=body.news_keyword,
            mode=body.mode,
            context_report_input=body.context_report_input,
            base_start_week=body.base_start_week,
            base_end_week=body.base_end_week,
        )
        elapsed = time.perf_counter() - t0
        payload = {
            "product_context_report_csv": str(out["context_csv"]),
            "product_context_report_json": str(out["context_json"]),
            "product_candidates_csv": str(out["products_csv"]),
            "product_candidates_json": str(out["products_json"]),
            "product_extractor_report_csv": str(out["report_csv"]),
            "product_extractor_report_json": str(out["report_json"]),
            "context_report": str(out.get("last_context_report", "")),
        }
        _LOG.info(
            "TOOL %s | OK | elapsed=%.2fs | product_extractor_report_csv=%s",
            name,
            elapsed,
            payload["product_extractor_report_csv"],
        )
        return _tool_result(name, True, payload)
    except Exception as exc:
        _LOG.exception("TOOL %s | FAIL | elapsed=%.2fs", name, time.perf_counter() - t0)
        return _tool_result(name, False, {}, str(exc))


@app.get("/v1/tools/manifest")
def manifest() -> Dict[str, Any]:
    _LOG.debug("GET /v1/tools/manifest")
    manifest_path = Path(__file__).resolve().parent / "tools_manifest.json"
    import json

    return json.loads(manifest_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)

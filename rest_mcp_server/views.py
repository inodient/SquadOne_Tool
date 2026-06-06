"""대시보드 조회 전용 REST 라우터 (/v1/view/*).

읽기 전용 — newstrend 스키마의 이미 적재된 산출물만 조회한다.
파이프라인 실행(POST /v1/tools/*)과 완전히 분리: Qdrant/Ollama 등 외부 의존성 불필요.
프론트(frontend/, React+Recharts)가 소비. 응답은 순수 데이터 JSON.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from importlib import import_module
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from db import repository as repo


def verify_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    """app.py 와 동일 정책: env REST_MCP_API_KEY 설정 시에만 강제(미설정이면 오픈)."""
    expected = os.environ.get("REST_MCP_API_KEY", "").strip()
    if not expected:
        return
    if not x_api_key or x_api_key.strip() != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


router = APIRouter(prefix="/v1/view", tags=["dashboard"], dependencies=[Depends(verify_api_key)])


def _resolve_week(week: Optional[str]) -> str:
    """week 미지정 시 최신 주차로 폴백. 데이터 자체가 없으면 404."""
    if week:
        return week
    latest = repo.latest_week()
    if not latest:
        raise HTTPException(status_code=404, detail="적재된 주차 데이터가 없습니다.")
    return latest


@router.get("/weeks")
def get_weeks() -> Dict[str, Any]:
    """분석 가능한 주차 목록(오름차순)과 최신 주차."""
    weeks = repo.read_distinct_weeks()
    return {"weeks": weeks, "latest": weeks[-1] if weeks else None}


@router.get("/summary")
def get_summary(week: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """KPI: 주차 · status별 트렌드 수 · 분석 키워드 수 · 추천 상품 수."""
    wk = _resolve_week(week)
    status_counts = repo.read_status_counts(wk)
    return {
        "week": wk,
        "keyword_count": repo.read_keyword_count(wk),
        "status_counts": status_counts,
        "emerging": status_counts.get("Emerging", 0),
        "active": status_counts.get("Active", 0),
        "fading": status_counts.get("Fading", 0),
        "archived": status_counts.get("Archived", 0),
        "product_count": len(repo.read_product_candidates(wk)),
    }


@router.get("/recommendations")
def get_recommendations(week: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """상품 추천 Top-N + 해당 주차 LLM 리포트(컨텍스트/최종)."""
    wk = _resolve_week(week)
    return {
        "week": wk,
        "products": repo.read_product_candidates(wk),
        "reports": repo.read_reports(wk),
    }


@router.get("/trends")
def get_trends(
    week: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """트렌드 시계열(주차 단위). status로 생명주기 필터."""
    wk = _resolve_week(week)
    return {"week": wk, "trends": repo.read_trends(wk, status)}


@router.get("/trends/excluded")
def get_trends_excluded(week: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """review 버킷: 고z(≥2.0)지만 노이즈 라벨로 anchor에서 제외된 후보(상품성 계절어 회수용)."""
    wk = _resolve_week(week)
    return {"week": wk, "excluded": repo.read_trend_excluded(wk)}


@router.get("/groups")
def get_groups(week: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """시맨틱 그룹(버블/트리맵용)."""
    wk = _resolve_week(week)
    return {"week": wk, "groups": repo.read_trend_groups(wk)}


@router.get("/trend")
def get_trend_detail(
    keyword: str = Query(...),
    week: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """단일 트렌드 상세: 시계열 + 근거 뉴스 + 핵심문장."""
    wk = _resolve_week(week)
    detail = repo.read_trend_detail(wk, keyword)
    return {"week": wk, "keyword": keyword, **detail}


@router.get("/zscore-series")
def get_zscore_series(
    keyword: str = Query(...),
    limit: int = Query(default=200, ge=1, le=600),
) -> Dict[str, Any]:
    """단일 키워드의 주차별 z-score 추이(라인차트)."""
    return {"keyword": keyword, "series": repo.read_zscore_series(keyword, limit=limit)}


@router.get("/sources")
def get_sources(
    keyword: str = Query(...),
    week: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """해당 (week, keyword)의 언론사 분포(도넛)."""
    wk = _resolve_week(week)
    return {"week": wk, "keyword": keyword, "sources": repo.read_source_distribution(wk, keyword)}


# ── 기간(범위) 추적 조회 (PeriodTracker 화면) ────────────────────
# start~end 주차를 한 번에 읽어 5~7단계 변화를 차트로 보여준다(읽기 전용).
# from/to 는 필수(단일 주차 폴백 없음). ISO 주차 'YYYY-Www' 형식.


def _validate_range(week_from: str, week_to: str) -> tuple[str, str]:
    """from/to 정규화·검증. from<=to 보장(뒤집혀 오면 스왑). 빈 값은 400."""
    f = (week_from or "").strip()
    t = (week_to or "").strip()
    if not f or not t:
        raise HTTPException(status_code=400, detail="from, to 주차가 모두 필요합니다.")
    if f > t:  # ISO 주차는 사전식=시간순
        f, t = t, f
    return f, t


@router.get("/range/lifecycle")
def get_range_lifecycle(
    week_from: str = Query(..., alias="from"),
    week_to: str = Query(..., alias="to"),
) -> Dict[str, Any]:
    """[6단계] 기간 내 주차별 status 카운트(생명주기 추이 Stacked Area)."""
    f, t = _validate_range(week_from, week_to)
    return {"from": f, "to": t, "rows": repo.read_lifecycle_range(f, t)}


@router.get("/range/zscore")
def get_range_zscore(
    week_from: str = Query(..., alias="from"),
    week_to: str = Query(..., alias="to"),
    top: int = Query(default=8, ge=1, le=20),
) -> Dict[str, Any]:
    """[6단계] 기간 내 상위 트렌드 키워드의 주차별 z-score(멀티라인)."""
    f, t = _validate_range(week_from, week_to)
    keywords = repo.read_top_keywords_range(f, t, limit=top)
    return {
        "from": f,
        "to": t,
        "keywords": keywords,
        "rows": repo.read_zscore_matrix_range(f, t, keywords),
    }


@router.get("/range/products")
def get_range_products(
    week_from: str = Query(..., alias="from"),
    week_to: str = Query(..., alias="to"),
) -> Dict[str, Any]:
    """[7단계] 기간 내 주차별 상품 추천(heatmap + 인접주 신규/유지/탈락 diff)."""
    f, t = _validate_range(week_from, week_to)
    return {"from": f, "to": t, "rows": repo.read_products_range(f, t)}


@router.get("/range/keysentence")
def get_range_keysentence(
    week_from: str = Query(..., alias="from"),
    week_to: str = Query(..., alias="to"),
) -> Dict[str, Any]:
    """[5단계] 기간 내 주차별 핵심문장 수·근거 수(근거 추이 라인)."""
    f, t = _validate_range(week_from, week_to)
    return {"from": f, "to": t, "rows": repo.read_keysentence_range(f, t)}


# ── 통합 인리치먼트 조회(Stage5B~7D) ──────────────────────────────

@router.get("/enrichment")
def get_enrichment(week: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """해당 주차 통합 인리치먼트 산출 전체(군집·해석·관련상품·GEO/유튜브질의·VERC신호·
    수요/경쟁/소셜·장기/주기 신호·Naver 그라운딩)를 단일 응답으로."""
    return repo.read_enrichment(_resolve_week(week))


@router.get("/clusters")
def get_clusters(week: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """해당 주차 군집 메타(키워드 수 내림차순)."""
    w = _resolve_week(week)
    return {"week": w, "clusters": repo.read_clusters(w)}


@router.get("/related-products")
def get_related_products(week: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """해당 주차 사입 관련상품(8-2)."""
    w = _resolve_week(week)
    return {"week": w, "products": repo.read_related_products(w)}


@router.get("/cluster-enriched")
def get_cluster_enriched(week: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """[8-결합] 군집 단위 결합 뷰(테마·부피/강도·해석 + 멤버 키워드 신호 롤업)."""
    w = _resolve_week(week)
    return {"week": w, "clusters": repo.read_cluster_enriched(w)}


@router.get("/keyword-enriched")
def get_keyword_enriched(week: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """[8-결합] 키워드 단위 결합 뷰(소속 군집 + 장기 지속성 + 현재 움직임)."""
    w = _resolve_week(week)
    return {"week": w, "keywords": repo.read_keyword_enriched(w)}


# ── 트렌드·상품 생성(5~7단계) 온디맨드 실행 ──────────────────────
# 상품이 없는 주차에서 trend_extractor(6) → product_extractor(7)를 단일 주차로 실행한다.
# 무겁고 외부 의존성(Qdrant/Ollama)이 필요하므로 백그라운드 스레드 + 인메모리 잡 상태로 처리한다.
# 잡은 프로세스 메모리에만 존재(서버 재시작 시 소멸). 동시성: 주차당 1잡, 전역 1잡.

_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_job(week: str, **fields: Any) -> Dict[str, Any]:
    with _JOBS_LOCK:
        job = _JOBS.setdefault(week, {"week": week})
        job.update(fields)
        return dict(job)


def _running_week() -> Optional[str]:
    with _JOBS_LOCK:
        for w, j in _JOBS.items():
            if j.get("status") == "running":
                return w
    return None


def _run_generate(week: str) -> None:
    """백그라운드: 단일 주차 trend → product 실행. base_start/end_week=week 로 해당 주차만 처리."""
    t0 = time.perf_counter()
    try:
        _set_job(week, step="trend_extractor", message="트렌드 추출 중 (Qdrant 벡터검색·LLM)")
        run_trend = import_module("steps.trend_extractor").run_trend_extractor
        run_trend(None, None, None, base_start_week=week, base_end_week=week)

        _set_job(week, step="product_extractor", message="상품군 추출 중 (LLM)")
        run_product = import_module("steps.product_extractor").run_product_extractor
        run_product(None, base_start_week=week, base_end_week=week, mode="full")

        n = len(repo.read_product_candidates(week))
        _set_job(
            week,
            status="success",
            step="done",
            product_count=n,
            elapsed=round(time.perf_counter() - t0, 1),
            message=f"완료 · 상품 {n}건 적재",
            finished_at=_now(),
        )
    except Exception as exc:  # noqa: BLE001 — 사용자에게 원인 전달
        _set_job(
            week,
            status="failed",
            error=str(exc),
            elapsed=round(time.perf_counter() - t0, 1),
            message="실행 실패 (외부 의존성/데이터 확인 필요)",
            finished_at=_now(),
        )


class GenerateBody(BaseModel):
    week: str


@router.post("/generate")
def post_generate(body: GenerateBody) -> Dict[str, Any]:
    """해당 주차의 트렌드·상품(6~7단계)을 백그라운드로 생성 시작."""
    week = body.week.strip()
    if not week:
        raise HTTPException(status_code=400, detail="week가 필요합니다.")

    busy = _running_week()
    if busy:
        if busy == week:
            return {"started": False, "job": _set_job(week)}
        raise HTTPException(status_code=409, detail=f"다른 주차({busy}) 생성이 진행 중입니다.")

    job = _set_job(
        week,
        status="running",
        step="queued",
        error=None,
        product_count=None,
        message="실행 시작",
        started_at=_now(),
        finished_at=None,
    )
    threading.Thread(target=_run_generate, args=(week,), daemon=True).start()
    return {"started": True, "job": job}


@router.get("/generate-status")
def get_generate_status(week: str = Query(...)) -> Dict[str, Any]:
    """주차별 생성 잡 상태. 없으면 idle."""
    with _JOBS_LOCK:
        job = _JOBS.get(week)
        return dict(job) if job else {"week": week, "status": "idle"}

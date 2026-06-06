"""Stage7C-1 — 수요 예측 (네이버 데이터랩 + Google Trends + LLM).

related_products(6-2)의 각 상품명을 키워드로 네이버 데이터랩 검색트렌드 + PyTrends 를 모아
LLM(llm_factory)으로 수요 신호를 해석해 demand_forecast 적재.
출처: SquadOne_Tool_Prototype/agents/demand_forecast. 키 없으면 graceful skip.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from db import repository as repo
from db.config import load_shared_env
from steps.common import get_logger, load_config
from steps.llm_util import invoke_json

load_shared_env()  # NAVER_*/LLM 키를 .env 에서 1회 로딩(멱등)
logger = get_logger("demand_forecast")
_DATALAB_URL = "https://openapi.naver.com/v1/datalab/search"


def _cfg() -> dict:
    return load_config().get("demand_forecast", {})


def _naver_keys() -> tuple[str, str]:
    return os.getenv("NAVER_CLIENT_ID", "").strip(), os.getenv("NAVER_CLIENT_SECRET", "").strip()


def _collect_datalab(keywords: List[str], cid: str, csec: str) -> Dict[str, Any]:
    import requests

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    body = {
        "startDate": start, "endDate": end, "timeUnit": "week",
        "keywordGroups": [{"groupName": kw, "keywords": [kw]} for kw in keywords[:5]],
        "device": "", "ages": [], "gender": "",
    }
    headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec, "Content-Type": "application/json"}
    r = requests.post(_DATALAB_URL, headers=headers, data=json.dumps(body), timeout=60)
    r.raise_for_status()
    return r.json()


def _collect_pytrends(keywords: List[str]) -> Dict[str, Any]:
    try:
        from pytrends.request import TrendReq

        pt = TrendReq(hl="ko-KR", tz=540)
        pt.build_payload(keywords[:5], timeframe="today 3-m", geo="KR")
        interest = pt.interest_over_time()
        if interest.empty:
            return {"series": {}, "note": "empty_or_rate_limited"}
        out = {}
        for c in interest.columns:
            if c == "isPartial":
                continue
            out[c] = {str(i.date()): int(v) for i, v in interest[c].dropna().items()}
        return {"series": out}
    except Exception as exc:  # noqa: BLE001
        return {"series": {}, "error": str(exc)}


def run_demand_forecast(week: str, *, max_products: Optional[int] = None) -> Dict[str, Any]:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        logger.info("demand_forecast 비활성화 — skip")
        return {"week": week, "skipped": True}
    cid, csec = _naver_keys()
    products = repo.read_related_products(week)
    if not products:
        logger.warning("[%s] related_products 없음 — demand_forecast skip", week)
        return {"week": week, "rows": 0}
    if max_products:
        products = products[:max_products]
    role = cfg.get("llm_role", "demand_analyst")
    use_naver = bool(cfg.get("naver_datalab", True)) and bool(cid and csec)
    use_pt = bool(cfg.get("pytrends", True))
    if not use_naver:
        logger.warning("[%s] NAVER 키 없음 — 데이터랩 생략(PyTrends/LLM 만)", week)

    rows = []
    for p in products:
        name = p["product_name"]
        if not name:
            continue
        kws = [name]
        naver_raw = {}
        if use_naver:
            try:
                naver_raw = _collect_datalab(kws, cid, csec)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] '%s' 데이터랩 실패: %s", week, name, exc)
        google_raw = _collect_pytrends(kws) if use_pt else {}
        prompt = (
            f"키워드(대표): {name}\n"
            f"네이버 데이터랩 응답(일부): {json.dumps(naver_raw, ensure_ascii=False)[:6000]}\n"
            f"구글 트렌드 시계열(일부): {json.dumps(google_raw, ensure_ascii=False)[:4000]}\n\n"
            "다음 JSON만 출력: {\"demand_summary\": 한국어요약, "
            "\"growth_signal\": \"strong_up|up|flat|down\", \"seasonality_hint\": 한국어, "
            "\"recommended_monitoring\": [모니터링 키워드들]}"
        )
        js = invoke_json(role, prompt) or {}
        rows.append((name, kws, js.get("demand_summary", ""), js.get("growth_signal", ""),
                     js.get("seasonality_hint", ""), js.get("recommended_monitoring", [])))
    n = repo.write_demand_forecast(week, rows)
    logger.info("[%s] demand_forecast %d행", week, n)
    return {"week": week, "rows": n}


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="수요 예측(Stage7C-1)")
    p.add_argument("--week", required=True)
    p.add_argument("--max-products", type=int, default=None)
    args = p.parse_args()
    print(json.dumps(run_demand_forecast(args.week, max_products=args.max_products), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

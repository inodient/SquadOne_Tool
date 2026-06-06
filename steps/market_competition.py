"""Stage7C-2 — 시장 경쟁 분석 (네이버 쇼핑 검색 API + LLM).

related_products(6-2)의 각 상품명을 네이버 쇼핑 API로 조회해 상품수·가격분포를 집계하고
LLM(llm_factory)으로 경쟁강도·마진여력을 해석해 market_competition 적재.
출처: SquadOne_Tool_Prototype/agents/market_competition. 네이버 키 없으면 graceful skip.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from statistics import mean
from typing import Any, Dict, List, Optional

from db import repository as repo
from db.config import load_shared_env
from steps.common import get_logger, load_config
from steps.llm_util import invoke_json

load_shared_env()  # NAVER_*/LLM 키를 .env 에서 1회 로딩(멱등)
logger = get_logger("market_competition")
_SHOP_URL = "https://openapi.naver.com/v1/search/shop.json"


def _cfg() -> dict:
    return load_config().get("market_competition", {})


def _naver_keys() -> tuple[str, str]:
    return os.getenv("NAVER_CLIENT_ID", "").strip(), os.getenv("NAVER_CLIENT_SECRET", "").strip()


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").replace("&quot;", '"').replace("&amp;", "&").strip()


def _collect(query: str, cid: str, csec: str, display: int) -> Dict[str, Any]:
    import requests

    headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec}
    url = f"{_SHOP_URL}?query={urllib.parse.quote(query)}&display={display}&sort=sim"
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    items = data.get("items") or []
    prices = []
    for it in items:
        try:
            prices.append(int(it.get("lprice", 0)))
        except (TypeError, ValueError):
            pass
    return {
        "total_estimated": int(data.get("total", 0)),
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "price_mean": round(mean(prices), 0) if prices else None,
        "sample_titles": [_strip_html(it.get("title", "")) for it in items[:10]],
    }


def run_market_competition(week: str, *, max_products: Optional[int] = None) -> Dict[str, Any]:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        logger.info("market_competition 비활성화 — skip")
        return {"week": week, "skipped": True}
    cid, csec = _naver_keys()
    if not (cid and csec):
        logger.warning("[%s] NAVER 키 없음 — market_competition skip", week)
        return {"week": week, "skipped": "no_api_key"}
    products = repo.read_related_products(week)
    if not products:
        logger.warning("[%s] related_products 없음 — market_competition skip", week)
        return {"week": week, "rows": 0}
    if max_products:
        products = products[:max_products]
    role = cfg.get("llm_role", "market_analyst")
    display = int(cfg.get("sample_size", 40))

    rows = []
    for p in products:
        name = p["product_name"]
        if not name:
            continue
        try:
            m = _collect(name, cid, csec, display)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] '%s' 쇼핑 조회 실패: %s", week, name, exc)
            continue
        prompt = (
            f"검색 키워드: {name}\n네이버 쇼핑 집계: {m}\n\n"
            "다음 JSON만 출력: {\"competition_summary\": 경쟁·가격대 한국어 해석, "
            "\"estimated_margin_room\": \"high|medium|low\"}"
        )
        js = invoke_json(role, prompt) or {}
        rows.append((name, name, m["total_estimated"], m["price_min"], m["price_mean"], m["price_max"],
                     js.get("competition_summary", ""), js.get("estimated_margin_room", ""), m["sample_titles"]))
    n = repo.write_market_competition(week, rows)
    logger.info("[%s] market_competition %d행", week, n)
    return {"week": week, "rows": n}


def main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="시장 경쟁(Stage7C-2)")
    p.add_argument("--week", required=True)
    p.add_argument("--max-products", type=int, default=None)
    args = p.parse_args()
    print(json.dumps(run_market_competition(args.week, max_products=args.max_products), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

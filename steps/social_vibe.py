"""Stage7C-3 — 소셜 바이브 (YouTube + LLM).

related_products(6-2)의 각 상품명을 YouTube 로 검색해 영상 메타를 모으고
LLM(llm_factory)으로 디자인/감성/오디언스 페인포인트/바이럴 잠재력을 해석해 social_vibe 적재.
출처: SquadOne_Tool_Prototype/agents/social_vibe. YouTube 키 없으면 graceful skip.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from db import repository as repo
from db.config import load_shared_env
from steps.common import get_logger, load_config
from steps.llm_util import invoke_json

load_shared_env()  # YOUTUBE/LLM 키를 .env 에서 1회 로딩(멱등)
logger = get_logger("social_vibe")
_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


def _cfg() -> dict:
    return load_config().get("social_vibe", {})


def _api_key() -> Optional[str]:
    for env_name in ("YOUTUBE_API_KEY", "GOOGLE_API_KEY", "YT_API_KEY"):
        v = os.getenv(env_name, "").strip()
        if v:
            return v
    return None


def _collect(query: str, key: str, max_results: int = 15) -> List[dict]:
    import requests

    sr = requests.get(_SEARCH_URL, params={
        "key": key, "q": query, "part": "id", "type": "video",
        "maxResults": max_results, "regionCode": "KR", "relevanceLanguage": "ko",
    }, timeout=30)
    sr.raise_for_status()
    ids = [it["id"]["videoId"] for it in sr.json().get("items", []) if it.get("id", {}).get("videoId")]
    if not ids:
        return []
    vr = requests.get(_VIDEOS_URL, params={"key": key, "id": ",".join(ids[:50]), "part": "snippet,statistics"}, timeout=30)
    vr.raise_for_status()
    return vr.json().get("items", [])


def run_social_vibe(week: str, *, max_products: Optional[int] = None) -> Dict[str, Any]:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        logger.info("social_vibe 비활성화 — skip")
        return {"week": week, "skipped": True}
    key = _api_key()
    if not key:
        logger.warning("[%s] YouTube 키 없음 — social_vibe skip", week)
        return {"week": week, "skipped": "no_api_key"}
    products = repo.read_related_products(week)
    if not products:
        logger.warning("[%s] related_products 없음 — social_vibe skip", week)
        return {"week": week, "rows": 0}
    if max_products:
        products = products[:max_products]
    role = cfg.get("llm_role", "social_analyst")
    min_videos = int(cfg.get("min_videos", 8))

    rows = []
    for p in products:
        name = p["product_name"]
        if not name:
            continue
        query = f"{name} 리뷰"
        try:
            videos = _collect(query, key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] '%s' 유튜브 조회 실패: %s", week, name, exc)
            continue
        titles = [v.get("snippet", {}).get("title", "") for v in videos]
        sample = [{"title": v.get("snippet", {}).get("title", ""),
                   "views": v.get("statistics", {}).get("viewCount")} for v in videos[:10]]
        prompt = (
            f"상품: {name}\n관련 유튜브 영상(제목·조회수): {sample}\n영상수: {len(videos)}\n\n"
            "다음 JSON만 출력: {\"vibe_summary\": 한국어요약, \"design_aesthetics\": [키워드], "
            "\"audience_pain_mentions\": [페인포인트], \"viral_potential_0_1\": 0~1실수, "
            "\"instagram_note\": 한국어}"
        )
        js = invoke_json(role, prompt) or {}
        try:
            vp = float(js.get("viral_potential_0_1", 0.0))
        except (TypeError, ValueError):
            vp = 0.0
        rows.append((name, query, len(videos), js.get("vibe_summary", ""),
                     js.get("design_aesthetics", []), js.get("audience_pain_mentions", []),
                     vp, js.get("instagram_note", "")))
    n = repo.write_social_vibe(week, rows)
    logger.info("[%s] social_vibe %d행", week, n)
    return {"week": week, "rows": n}


def main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="소셜 바이브(Stage7C-3)")
    p.add_argument("--week", required=True)
    p.add_argument("--max-products", type=int, default=None)
    args = p.parse_args()
    print(json.dumps(run_social_vibe(args.week, max_products=args.max_products), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

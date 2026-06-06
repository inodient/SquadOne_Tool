"""Stage7B-2 — YouTube VERC 수요검증 스코어링.

youtube_queries(주차)를 YouTube Data API v3 로 검색해 영상 통계를 모으고
V(조회수)·E(참여)·R(최신성)·C(맥락) 4요소 + 종합 P 점수를 계산해 youtube_signals 적재.
출처: SquadOne_Tool_Product_Extraction/steps/youtube_signal_pipeline.py.
키(YOUTUBE_API_KEY 등) 없으면 graceful skip.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db import repository as repo
from db.config import load_shared_env
from steps.common import get_logger, load_config

load_shared_env()  # YOUTUBE 키를 .env 에서 1회 로딩(멱등)
logger = get_logger("youtube_signal")

_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


def _cfg() -> dict:
    return load_config().get("youtube_signal", {})


def _api_key(cfg: dict) -> Optional[str]:
    for env_name in cfg.get("api_key_env", ["YOUTUBE_API_KEY", "GOOGLE_API_KEY", "YT_API_KEY"]):
        v = os.getenv(env_name, "").strip()
        if v:
            return v
    return None


def _search_video_ids(query: str, key: str, cfg: dict) -> List[str]:
    import requests

    params = {
        "key": key, "q": query, "part": "id", "type": "video",
        "maxResults": min(50, int(cfg.get("max_results", 20))),
        "regionCode": cfg.get("region_code", "KR"),
        "relevanceLanguage": cfg.get("relevance_language", "ko"),
    }
    r = requests.get(_SEARCH_URL, params=params, timeout=30)
    r.raise_for_status()
    return [it["id"]["videoId"] for it in r.json().get("items", []) if it.get("id", {}).get("videoId")]


def _fetch_videos(ids: List[str], key: str) -> List[dict]:
    import requests

    if not ids:
        return []
    params = {"key": key, "id": ",".join(ids[:50]), "part": "snippet,statistics"}
    r = requests.get(_VIDEOS_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("items", [])


def _week_to_dt(week: str) -> Optional[datetime]:
    try:
        return datetime.strptime(week + "-1", "%G-W%V-%u").replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _score_query(videos: List[dict], cfg: dict, as_of: Optional[datetime]) -> Dict[str, Any]:
    ctx_kw = cfg.get("context_keywords", [])
    recency_weeks = int(cfg.get("recency_weeks", 4))
    n = len(videos)
    if n == 0:
        return {"video_count": 0, "total_views": 0, "avg_views": 0.0,
                "V": 0.0, "E": 0.0, "R": 0.0, "C": 0.0, "P": 0.0,
                "engagement_ratio_raw": 0.0, "context_ratio": 0.0}
    total_views = 0
    eng_num = 0
    ctx_hits = 0
    recent_hits = 0
    for v in videos:
        stats = v.get("statistics", {})
        views = int(stats.get("viewCount", 0) or 0)
        likes = int(stats.get("likeCount", 0) or 0)
        comments = int(stats.get("commentCount", 0) or 0)
        total_views += views
        eng_num += likes + 2 * comments
        title = (v.get("snippet", {}).get("title") or "")
        if any(k in title for k in ctx_kw):
            ctx_hits += 1
        pub = v.get("snippet", {}).get("publishedAt")
        if pub and as_of:
            try:
                pdt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if (as_of - pdt).days <= recency_weeks * 7 and (as_of - pdt).days >= -7:
                    recent_hits += 1
            except Exception:  # noqa: BLE001
                pass
    avg_views = total_views / n
    # V (global log)
    cap = float(cfg.get("volume_global_log10_cap", 8.0))
    V = min(1.0, math.log10(max(1, total_views)) / cap) if cap > 0 else 0.0
    # E (engagement ratio, absolute mode)
    eng_ratio = eng_num / max(1, total_views)
    e_cap = float(cfg.get("engagement_absolute_ratio_cap", 0.08))
    E = min(1.0, eng_ratio / e_cap) if e_cap > 0 else 0.0
    # R (recency fraction)
    R = recent_hits / n
    # C (context fraction)
    C = ctx_hits / n
    w = cfg.get("weights", {})
    P = (float(w.get("w1", 0.25)) * V + float(w.get("w2", 0.25)) * E
         + float(w.get("w3", 0.25)) * R + float(w.get("w4", 0.25)) * C)
    return {"video_count": n, "total_views": total_views, "avg_views": avg_views,
            "V": V, "E": E, "R": R, "C": C, "P": P,
            "engagement_ratio_raw": eng_ratio, "context_ratio": C}


def run_youtube_signal(week: str, *, max_queries: Optional[int] = None) -> Dict[str, Any]:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        logger.info("youtube_signal 비활성화 — skip")
        return {"week": week, "skipped": True}
    key = _api_key(cfg)
    if not key:
        logger.warning("[%s] YouTube API 키 없음 — youtube_signal skip", week)
        return {"week": week, "skipped": "no_api_key"}

    queries = repo.read_youtube_queries(week)
    if not queries:
        logger.warning("[%s] youtube_queries 없음 — (먼저 6-3/geo_query 실행)", week)
        return {"week": week, "rows": 0}
    if max_queries:
        queries = queries[:max_queries]

    as_of = _week_to_dt(week)
    rows = []
    for q in queries:
        query = q["search_query"]
        if not query:
            continue
        try:
            ids = _search_video_ids(query, key, cfg)
            videos = _fetch_videos(ids, key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] '%s' YouTube 조회 실패: %s", week, query, exc)
            continue
        sc = _score_query(videos, cfg, as_of)
        meta = {"weights": cfg.get("weights", {}), "region": cfg.get("region_code", "KR")}
        rows.append((int(q["cluster_id"]), query, q.get("search_type"), sc["video_count"],
                     sc["total_views"], sc["avg_views"], sc["V"], sc["E"], sc["R"], sc["C"], sc["P"],
                     sc["engagement_ratio_raw"], sc["context_ratio"], meta))
    n = repo.write_youtube_signals(week, rows)
    logger.info("[%s] youtube_signals %d행", week, n)
    return {"week": week, "rows": n}


def main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="YouTube VERC 수요검증(Stage7B-2)")
    p.add_argument("--week", required=True)
    p.add_argument("--max-queries", type=int, default=None)
    args = p.parse_args()
    print(json.dumps(run_youtube_signal(args.week, max_queries=args.max_queries), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

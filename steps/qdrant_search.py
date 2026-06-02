"""공유 Qdrant 벡터검색 래퍼 (P2).

5·6단계 통합용. 기존 trend_extractor._query_qdrant_contexts(검증된 벡터검색+scroll 폴백)를
config 기반 kwargs로 호출하고, 런 스코프 캐시로 동일 (week,keyword,seed) 재검색을 제거한다.

- doc_id는 payload news_id로 통일됨(_map_hit_to_context, docs/QDRANT_CONTRACT.md).
- 서버 필터는 date-only(file_overlap off): `date` payload 인덱스만 존재(P1에서 생성),
  file_date_* 는 미인덱스이므로 range 필터를 피한다.
- 순환 import 방지를 위해 _query_qdrant_contexts 는 함수 내부에서 지연 import.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List

_CACHE: Dict[str, List[Dict[str, Any]]] = {}


def clear_cache() -> None:
    _CACHE.clear()


def cache_stats() -> Dict[str, int]:
    return {"entries": len(_CACHE)}


def search_contexts(
    week: str,
    keyword: str,
    seed_text: str | None,
    *,
    logger: Any,
    config: dict,
    top_k: int | None = None,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    """(week, keyword)에 대해 seed_text를 임베딩해 근거 컨텍스트를 검색한다.

    반환 각 dict: doc_id(news_id), score, title, summary_or_body, source(press), published_at.
    """
    from steps.trend_extractor import _query_qdrant_contexts  # 지연 import(순환 방지)
    from db.config import qdrant_url as _qurl

    vcfg = config.get("vector_db", {})
    # .env(QDRANT_HOST/PORT)를 단일 출처로 사용. config의 qdrant_url(=localhost)은 env 미설정 시 폴백만.
    # (config가 truthy면 무조건 우선하던 기존 코드는 원격 .env를 무시해 localhost 접속→Connection refused 였음.)
    q_url = _qurl() or str(vcfg.get("qdrant_url", "http://localhost:6333"))
    collection = str(vcfg.get("collection", "news_10y_ko_v1"))
    tk = int(top_k if top_k is not None else vcfg.get("top_k", 10))
    seed = (seed_text or keyword or "").strip()

    cache_key = f"{week}|{keyword}|{tk}|" + hashlib.md5(seed.encode("utf-8")).hexdigest()
    if use_cache and cache_key in _CACHE:
        logger.info("QDRANT_SEARCH_CACHE_HIT | week=%s | keyword=%s", week, str(keyword)[:60])
        return _CACHE[cache_key]

    contexts = _query_qdrant_contexts(
        logger=logger,
        qdrant_url=q_url,
        collection=collection,
        date_field=str(vcfg.get("date_field", "date")),
        date_start_field=str(vcfg.get("date_start_field", "file_date_start")),
        date_end_field=str(vcfg.get("date_end_field", "file_date_end")),
        week=week,
        keyword=keyword,
        query_text=seed,
        top_k=tk,
        timeout_sec=float(vcfg.get("timeout_sec", 20)),
        enable_query_embedding=bool(vcfg.get("enable_query_embedding", True)),
        embed_model=(str(vcfg.get("embed_model")).strip() if vcfg.get("embed_model") else None),
        embed_device=(str(vcfg.get("embed_device")).strip() if vcfg.get("embed_device") else None),
        query_vector_name=str(vcfg.get("query_vector_name", "body")),
        use_server_date_filter=bool(vcfg.get("use_server_date_filter", True)),
        vector_search_limit_multiplier=int(vcfg.get("vector_search_limit_multiplier", 3)),
        vector_search_min_limit=int(vcfg.get("vector_search_min_limit", 30)),
        scroll_limit_multiplier=int(vcfg.get("scroll_limit_multiplier", 12)),
        scroll_min_limit=int(vcfg.get("scroll_min_limit", 100)),
        scroll_max_pages=int(vcfg.get("scroll_max_pages", 12)),
        week_scroll_mode=str(vcfg.get("week_scroll_mode", "default")),
        scroll_full_week_max_pages=int(vcfg.get("scroll_full_week_max_pages", 0)),
        server_week_filter_file_overlap=False,  # date-only(인덱스된 date만 사용)
    )
    if use_cache:
        _CACHE[cache_key] = contexts
    return contexts

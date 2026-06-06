"""Stage7D — 상품 후보 ↔ 네이버 쇼핑 카테고리 그라운딩.

related_products(6-2) + product_candidates(7A)의 상품명을 newstrend.naver_categories
분류체계에 fuzzy 매칭해 카테고리/풀패스/cid 를 붙인다. 결과는 reports(step='product_grounding')
에 jsonb 로 적재(대시보드/소싱에 카테고리 컨텍스트 제공).
출처 데이터: collectors/naver_categories.py 가 적재한 naver_categories.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from db import repository as repo
from steps.common import get_logger, load_config

logger = get_logger("naver_grounding")
_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]{2,}")


def _cfg() -> dict:
    return load_config().get("naver_categories", {})


def _match(product: str, cats: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """상품명에 대해 최적 카테고리 1건(토큰 중첩 점수). 없으면 None."""
    p_tokens = set(_TOKEN_RE.findall(product))
    if not p_tokens:
        return None
    best = None
    best_score = 0.0
    for c in cats:
        name = c.get("name") or ""
        cat4 = c.get("cat4") or ""
        target = (name + " " + cat4 + " " + (c.get("full_path") or "")).strip()
        c_tokens = set(_TOKEN_RE.findall(target))
        if not c_tokens:
            continue
        # 직접 포함 가산 + 토큰 자카드
        direct = 1.0 if (name and (name in product or product in name)) else 0.0
        inter = len(p_tokens & c_tokens)
        union = len(p_tokens | c_tokens) or 1
        score = direct + inter / union
        if score > best_score:
            best_score = score
            best = c
    if best is None or best_score <= 0:
        return None
    return {"category_name": best.get("name"), "full_path": best.get("full_path"),
            "cid": best.get("cid"), "score": round(best_score, 3)}


def run_naver_grounding(week: str) -> Dict[str, Any]:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        logger.info("naver_categories 비활성화 — skip")
        return {"week": week, "skipped": True}
    cats_df = repo.read_naver_categories()
    if cats_df.empty:
        logger.warning("[%s] naver_categories 비어있음 — 먼저 collectors.naver_categories 실행", week)
        return {"week": week, "skipped": "no_categories"}
    cats = cats_df.to_dict("records")

    products: List[Dict[str, Any]] = []
    for r in repo.read_related_products(week):
        if r.get("product_name"):
            products.append({"product_name": r["product_name"], "source": "related_products", "rank": r.get("rank")})
    for r in repo.read_product_candidates(week):
        if r.get("product_name"):
            products.append({"product_name": r["product_name"], "source": "product_candidates", "rank": r.get("rank")})

    if not products:
        logger.warning("[%s] 그라운딩할 상품 없음", week)
        return {"week": week, "matched": 0}

    mapping = []
    matched = 0
    for p in products:
        m = _match(p["product_name"], cats)
        entry = {"product_name": p["product_name"], "source": p["source"], "rank": p.get("rank"), "match": m}
        if m:
            matched += 1
        mapping.append(entry)

    repo.write_report("product_grounding", week, {"mappings": mapping},
                      meta={"total": len(products), "matched": matched})
    logger.info("[%s] product_grounding: %d/%d 매칭 → reports 적재", week, matched, len(products))
    return {"week": week, "total": len(products), "matched": matched}


def main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="Naver 카테고리 그라운딩(Stage7D)")
    p.add_argument("--week", required=True)
    args = p.parse_args()
    print(json.dumps(run_naver_grounding(args.week), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

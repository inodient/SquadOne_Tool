"""Stage5B-2 — 군집별 1인 셀러 인사이트(LLM 해석).

각 군집(테마+대표어+키워드)에 대해 4구획 해석을 생성:
1) 군집 의미 2) 핵심 고객/수요 신호 3) 상품/콘텐츠 아이디어 4) 리스크/주의.
출처: SquadOne_Tool_NewsTrend/steps/cluster_interpretation.py. LLM 은 llm_factory 경유.
결과는 newstrend.cluster_interpretation 에 적재.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from db import repository as repo
from steps.common import get_logger, load_config
from steps.llm_factory import get_llm

logger = get_logger("cluster_interpretation")


def _cfg() -> dict:
    return load_config().get("clustering", {})


def _build_prompt(c: Dict[str, Any]) -> str:
    return (
        "너는 한국 뉴스 트렌드를 1인 셀러(이커머스) 관점에서 해석하는 전문가다.\n"
        f"[군집 테마] {c.get('cluster_theme') or '(미지정)'}\n"
        f"[대표어] {c.get('representative_terms') or ''}\n"
        f"[키워드] {', '.join(c.get('keywords', [])[:20])}\n"
        f"[평균 z] {c.get('avg_z_score')}\n\n"
        "다음 4구획으로 간결히 작성하라(각 구획 머리말 포함):\n"
        "1. 군집 의미 (2문장)\n"
        "2. 핵심 고객/수요 신호 (2~3개 불릿)\n"
        "3. 상품/콘텐츠 아이디어 (3개 불릿, 사입/위탁 가능한 일반명사 위주)\n"
        "4. 리스크/주의 포인트 (1~2개 불릿)\n"
    )


def _model_label() -> str:
    provider = os.getenv("LLM_PROVIDER", "ollama").lower()
    if provider == "gemini":
        return os.getenv("GEMINI_MODEL_DEFAULT", "gemini-1.5-flash")
    if provider == "mock":
        return "mock"
    return os.getenv("OLLAMA_MODEL_DEFAULT", "llama3.1:8b")


def run_cluster_interpretation(week: str, clusters: Optional[List[Dict[str, Any]]] = None) -> int:
    """군집 해석 생성 → DB 적재. clusters 미지정 시 DB에서 로드.

    clusters 항목: {cluster_id, cluster_theme, keyword_count, avg_z_score,
                    representative_terms, keywords:[...]}
    """
    role = _cfg().get("interpretation_llm_role", "cluster_interpretation")
    include_noise = bool(_cfg().get("include_noise_cluster", False))

    if clusters is None:
        meta = repo.read_clusters(week)
        ck = repo.read_cluster_keywords(week)
        kw_by_cluster: Dict[int, List[str]] = {}
        for cid, grp in ck.groupby("cluster_id"):
            kw_by_cluster[int(cid)] = grp["keyword"].tolist()
        clusters = [{
            "cluster_id": int(m["cluster_id"]),
            "cluster_theme": m.get("cluster_theme"),
            "keyword_count": m.get("keyword_count"),
            "avg_z_score": m.get("avg_z_score"),
            "representative_terms": m.get("representative_terms"),
            "keywords": kw_by_cluster.get(int(m["cluster_id"]), []),
        } for m in meta]

    if not clusters:
        logger.info("[%s] 해석할 군집 없음", week)
        return 0

    client = get_llm(role)
    model_label = _model_label()
    rows = []
    for c in clusters:
        cid = int(c["cluster_id"])
        if cid == -1 and not include_noise:
            continue
        try:
            interp = client.invoke(_build_prompt(c)).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] cluster %d 해석 실패: %s", week, cid, exc)
            interp = ""
        rows.append((
            cid, c.get("cluster_theme"), c.get("keyword_count"),
            c.get("avg_z_score"), c.get("representative_terms"), interp, model_label,
        ))
    n = repo.replace_cluster_interpretation(week, rows)
    logger.info("[%s] cluster_interpretation %d행 적재", week, n)
    return n


def main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="군집 해석(Stage5B-2)")
    p.add_argument("--week", required=True)
    args = p.parse_args()
    print(json.dumps({"week": args.week, "rows": run_cluster_interpretation(args.week)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

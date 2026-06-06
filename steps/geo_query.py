"""Stage7B-1 — 군집 → GEO 상품발굴 질의 + 군집별 유튜브 검색질의.

출처: SquadOne_Tool_Product_Extraction/steps/generate_product_queries.py + Prompts.
clusters + cluster_interpretation(DB) 을 입력으로 군집별 페르소나 GEO 질의와
유튜브 검색셋을 LLM(llm_factory)으로 생성해 geo_queries / youtube_queries(실 cluster_id) 적재.
graceful: LLM JSON 파싱 실패 시 템플릿 폴백.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from db import repository as repo
from db.config import load_shared_env
from steps.common import get_logger, load_config
from steps.llm_util import invoke_json

load_shared_env()  # LLM env 를 .env 에서 1회 로딩(멱등)
logger = get_logger("geo_query")
_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts" / "product"


def _cfg() -> dict:
    return load_config().get("geo_query", {})


def _load(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _fill(tmpl: str, **kw) -> str:
    out = tmpl
    for k, v in kw.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def _fallback_geo(theme: str) -> List[dict]:
    return [{
        "target_audience": "1인 셀러/소상공인",
        "query": f"'{theme}' 트렌드 맥락에서 초기 자본 100만원 내 사입 가능한 일반명사 상품은?",
        "expected_insight": "도매 검색 가능한 구체 상품 아이템",
    }]


def _fallback_youtube(theme: str) -> List[dict]:
    return [
        {"search_query": f"{theme} 추천", "search_type": "리뷰/추천", "reasoning": "구매전환 신호 확인"},
        {"search_query": f"{theme} 후기", "search_type": "리뷰", "reasoning": "실사용 반응"},
    ]


def run_geo_query(week: str) -> Dict[str, Any]:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        logger.info("geo_query 비활성화 — skip")
        return {"week": week, "skipped": True}

    drop_noise = bool(cfg.get("drop_noise_cluster", True))
    role = cfg.get("llm_role", "geo_query")
    clusters = repo.read_clusters(week)
    if not clusters:
        logger.warning("[%s] 군집 없음 — geo_query skip (먼저 clustering 실행)", week)
        return {"week": week, "geo": 0, "youtube": 0}

    interp = {int(r["cluster_id"]): r for r in []}
    try:
        idf = repo.read_cluster_keywords(week)  # for keywords per cluster
    except Exception:  # noqa: BLE001
        idf = None
    kw_by_cluster: Dict[int, List[str]] = {}
    if idf is not None and not idf.empty:
        for cid, grp in idf.groupby("cluster_id"):
            kw_by_cluster[int(cid)] = grp["keyword"].tolist()

    # cluster_interpretation 의 final_interpretation 매핑
    final_interp: Dict[int, str] = {}
    try:
        from db.engine import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cluster_id, final_interpretation FROM newstrend.cluster_interpretation WHERE week=%s",
                    (week,),
                )
                final_interp = {int(c): (t or "") for c, t in cur.fetchall()}
    except Exception:  # noqa: BLE001
        pass

    geo_tmpl = _load("geo_question_prompt.txt")
    yt_tmpl = _load("youtube_search_prompt.txt")
    geo_total = 0
    yt_total = 0
    for c in clusters:
        cid = int(c["cluster_id"])
        if cid == -1 and drop_noise:
            continue
        theme = c.get("cluster_theme") or ""
        rep = c.get("representative_terms") or ""
        kws = "|".join(kw_by_cluster.get(cid, []))
        # GEO
        geo_prompt = _fill(geo_tmpl, cluster_id=cid, cluster_theme=theme,
                           representative_terms=rep, final_interpretation=final_interp.get(cid, "")) + \
            "\n\n반드시 JSON 배열로만 출력: [{\"target_audience\":..., \"query\":..., \"expected_insight\":...}]"
        geo_json = invoke_json(role, geo_prompt)
        geo_items = geo_json if isinstance(geo_json, list) else _fallback_geo(theme)
        geo_rows = [(i + 1, it.get("target_audience", ""), it.get("query", ""), it.get("expected_insight", ""))
                    for i, it in enumerate(geo_items) if isinstance(it, dict)]
        geo_total += repo.write_geo_queries(week, cid, geo_rows)
        # YouTube
        yt_prompt = _fill(yt_tmpl, cluster_id=cid, cluster_theme=theme, keywords=kws) + \
            "\n\n반드시 JSON 배열로만 출력: [{\"search_query\":..., \"search_type\":..., \"reasoning\":...}]"
        yt_json = invoke_json(role, yt_prompt)
        yt_items = yt_json if isinstance(yt_json, list) else _fallback_youtube(theme)
        yt_rows = [(i + 1, it.get("search_query", ""), it.get("search_type", ""), it.get("reasoning", ""))
                   for i, it in enumerate(yt_items) if isinstance(it, dict)]
        yt_total += repo.write_youtube_queries(week, cid, yt_rows)

    logger.info("[%s] geo_queries=%d, youtube_queries(군집)=%d", week, geo_total, yt_total)
    return {"week": week, "geo": geo_total, "youtube": yt_total}


def main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="GEO/유튜브 질의 생성(Stage7B-1)")
    p.add_argument("--week", required=True)
    args = p.parse_args()
    print(json.dumps(run_geo_query(args.week), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

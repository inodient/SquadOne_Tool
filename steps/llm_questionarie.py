"""Stage6 — LLM 트렌드 인텔리전스 (6-1 브리프 → 6-2 관련상품 → 6-3 유튜브질의).

출처: SquadOne_Tool_NewsTrend/steps/llm_questionarie.py + prompts/llm_questionarie/*.
CSV/Excel→DB/Qdrant/llm_factory 로 재작성.
- 6-1: 고z 키워드별 Qdrant 기사근거 → '트렌드 인텔리전스 브리프' → newstrend.llm_briefs
- 6-2: 주차 브리프 종합 → 사입 가능 관련상품 10개 → newstrend.related_products
- 6-3: 주차 브리프 종합 → 유튜브 검색질의 12개 → newstrend.youtube_queries(cluster_id=-1)
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from db import repository as repo
from db.config import qdrant_url
from steps.common import get_logger, load_config
from steps.llm_factory import get_llm
from steps.qdrant_news import iter_week_articles

logger = get_logger("llm_questionarie")

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts" / "llm_questionarie"


def _cfg() -> dict:
    return load_config().get("llm_questionarie", {})


def _vcfg() -> dict:
    return load_config().get("vector_db", {})


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _model_label() -> str:
    provider = os.getenv("LLM_PROVIDER", "ollama").lower()
    if provider == "gemini":
        return os.getenv("GEMINI_MODEL_DEFAULT", "gemini-1.5-flash")
    if provider == "mock":
        return "mock"
    return os.getenv("OLLAMA_MODEL_DEFAULT", "llama3.1:8b")


def _high_z(week: str, top_n: int):
    zdf = repo.read_zscore(start_week=week, end_week=week)
    thr = float(load_config().get("z_score", {}).get("high_z_score_threshold", 2.0))
    zdf = zdf[zdf["z_score"] >= thr].sort_values("z_score", ascending=False).head(top_n)
    return zdf


# ── 6-1: 트렌드 인텔리전스 브리프 ─────────────────────────────────

def run_llm_step_61(week: str, *, top_n: int = 30) -> int:
    cfg = _cfg().get("step_61", {})
    max_chars = int(cfg.get("max_article_chars", 20000))
    role = cfg.get("llm_role", "trend_brief")
    zdf = _high_z(week, top_n)
    if zdf.empty:
        logger.warning("[%s] 6-1 고z 키워드 없음", week)
        return 0
    keywords = zdf["keyword"].tolist()
    src_map = {k: (s or "") for k, s in zip(zdf["keyword"], zdf.get("sources", [""] * len(zdf)))}

    # 주차 기사 1회 scroll → 키워드별 컨텍스트 버킷
    ctx: Dict[str, List[str]] = {k: [] for k in keywords}
    ctx_len: Dict[str, int] = {k: 0 for k in keywords}
    cnt: Dict[str, int] = {k: 0 for k in keywords}
    vcfg = _vcfg()
    for art in iter_week_articles(
        week, logger=logger,
        qdrant_url=qdrant_url() or vcfg.get("qdrant_url", "http://localhost:6333"),
        collection=vcfg.get("collection", "news_10y_ko_v1"),
        timeout_sec=float(vcfg.get("timeout_sec", 30)),
    ):
        title = art.get("title") or ""
        body = art.get("body") or ""
        hay = title + "\n" + body
        for k in keywords:
            if ctx_len[k] >= max_chars:
                continue
            if k and k in hay:
                piece = (title + " " + body).strip()
                ctx[k].append(piece[: max_chars - ctx_len[k]])
                ctx_len[k] += len(piece)
                cnt[k] += 1

    prompt_tmpl = _load_prompt("01_trend_intelligence_brief_prompt.txt")
    client = get_llm(role)
    model_label = _model_label()
    rows = []
    for k in keywords:
        news_context = "\n---\n".join(ctx[k])[:max_chars]
        prompt = (prompt_tmpl
                  .replace("{week}", week)
                  .replace("{keyword_text}", k)
                  .replace("{source_text}", src_map.get(k, ""))
                  .replace("{news_context}", news_context))
        try:
            brief = client.invoke(prompt).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] 6-1 '%s' 실패: %s", week, k, exc)
            brief = ""
        rows.append((k, brief, cnt[k], model_label))
    n = repo.write_llm_briefs(week, rows)
    logger.info("[%s] 6-1 llm_briefs %d행", week, n)
    return n


def _aggregate_briefs(week: str) -> str:
    bdf = repo.read_llm_briefs(week)
    if bdf.empty:
        return ""
    parts = [f"[{r.keyword}] {r.brief_text}" for r in bdf.itertuples() if (r.brief_text or "").strip()]
    return "\n\n".join(parts)


# ── 6-2: 사입 가능 관련상품 ───────────────────────────────────────

_LINE_NUM_RE = re.compile(r"^\s*\d+[\).\.]?\s*")


def run_llm_step_62(week: str) -> int:
    cfg = _cfg().get("step_62", {})
    role = cfg.get("llm_role", "related_products")
    briefs = _aggregate_briefs(week)
    if not briefs:
        logger.warning("[%s] 6-2 브리프 없음 (6-1 먼저 실행)", week)
        return 0
    prompt = (_load_prompt("02_related_products_prompt.txt")
              .replace("{week}", week)
              .replace("{keyword_text}", "(주차 종합)")
              .replace("{source_text}", "")
              .replace("{trend_intelligence_brief}", briefs[:20000]))
    out = get_llm(role).invoke(prompt).strip()
    rows = []
    rank = 0
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        line = _LINE_NUM_RE.sub("", line).strip()
        if not line:
            continue
        # "상품명 - 근거"
        if " - " in line:
            name, _, rationale = line.partition(" - ")
        elif "-" in line:
            name, _, rationale = line.partition("-")
        else:
            name, rationale = line, ""
        rank += 1
        rows.append((rank, name.strip()[:200], rationale.strip(), None))
        if rank >= int(cfg.get("num_products", 10)):
            break
    n = repo.write_related_products(week, rows)
    logger.info("[%s] 6-2 related_products %d행", week, n)
    return n


# ── 6-3: 유튜브 검색질의 ──────────────────────────────────────────

def run_llm_step_63(week: str) -> int:
    cfg = _cfg().get("step_63", {})
    role = cfg.get("llm_role", "youtube_queries")
    briefs = _aggregate_briefs(week)
    if not briefs:
        logger.warning("[%s] 6-3 브리프 없음 (6-1 먼저 실행)", week)
        return 0
    prompt = (_load_prompt("03_youtube_queries_prompt.txt")
              .replace("{week}", week)
              .replace("{keyword_text}", "(주차 종합)")
              .replace("{source_text}", "")
              .replace("{trend_intelligence_brief}", briefs[:20000]))
    out = get_llm(role).invoke(prompt).strip()
    rows = []
    seq = 0
    for line in out.splitlines():
        q = _LINE_NUM_RE.sub("", line.strip()).strip().strip('"').strip("'")
        if not q:
            continue
        seq += 1
        rows.append((seq, q[:300], "brief", None))
        if seq >= int(cfg.get("num_queries", 12)):
            break
    n = repo.write_youtube_queries(week, -1, rows)
    logger.info("[%s] 6-3 youtube_queries %d행 (cluster_id=-1)", week, n)
    return n


def run_all(week: str, *, top_n: int = 30) -> Dict[str, Any]:
    if not _cfg().get("enabled", True):
        logger.info("llm_questionarie 비활성화 — skip")
        return {"week": week, "skipped": True}
    n61 = run_llm_step_61(week, top_n=top_n)
    n62 = run_llm_step_62(week)
    n63 = run_llm_step_63(week)
    return {"week": week, "briefs": n61, "related_products": n62, "youtube_queries": n63}


def main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="LLM 인텔(Stage6)")
    p.add_argument("--week", required=True)
    p.add_argument("--step", choices=["61", "62", "63", "all"], default="all")
    p.add_argument("--top-n", type=int, default=30)
    args = p.parse_args()
    if args.step == "61":
        out = {"briefs": run_llm_step_61(args.week, top_n=args.top_n)}
    elif args.step == "62":
        out = {"related_products": run_llm_step_62(args.week)}
    elif args.step == "63":
        out = {"youtube_queries": run_llm_step_63(args.week)}
    else:
        out = run_all(args.week, top_n=args.top_n)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

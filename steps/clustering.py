"""Stage5B-1 — 주차별 군집화 (현행 trend_extractor 벡터검색과 병행).

고z 키워드가 등장하는 기사 제목을 Qdrant 에서 모아 임베딩→UMAP→HDBSCAN 으로 군집화하고,
c-TF-IDF 대표어 + LLM 테마 라벨을 붙인다. 결과는 DB(clusters/cluster_keywords)에 적재.
출처 알고리즘: SquadOne_Tool_NewsTrend/steps/clustering.py (CSV/Excel→DB/Qdrant 재작성).

graceful: umap/hdbscan 미설치·기사 부족 시 keyword_direct 모드(키워드 임베딩)로 폴백.
LLM 테마는 llm_factory.get_llm(role) 경유(genai 직접호출 금지).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from db import repository as repo
from db.config import qdrant_url
from steps.common import get_logger, load_config
from steps.llm_factory import get_llm
from steps.qdrant_embed import embed_texts
from steps.qdrant_news import iter_week_articles

logger = get_logger("clustering")

_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]{2,}")


def _cfg() -> dict:
    return load_config().get("clustering", {})


def _vcfg() -> dict:
    return load_config().get("vector_db", {})


def _high_z_keywords(week: str, top_n: int) -> pd.DataFrame:
    """해당 주차 고z 키워드 상위 top_n (z 내림차순). exclude 노이즈 클래스 반영."""
    zdf = repo.read_zscore(start_week=week, end_week=week)
    thr = float(load_config().get("z_score", {}).get("high_z_score_threshold", 2.0))
    zdf = zdf[zdf["z_score"] >= thr].copy()
    # 노이즈 클래스 제외(trend_extractor 정책과 동일)
    te = load_config().get("trend_extractor", {})
    if te.get("exclude_noise_classes", True):
        try:
            kc = repo.read_keyword_class()
            excl = set(te.get("exclude_classes", []))
            noisy = set(kc[kc["class"].isin(excl)]["keyword"].tolist()) if not kc.empty else set()
            if noisy:
                zdf = zdf[~zdf["keyword"].isin(noisy)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("노이즈 클래스 제외 skip: %s", exc)
    return zdf.sort_values("z_score", ascending=False).head(top_n).reset_index(drop=True)


def _collect_titles(week: str, keywords: List[str], cfg: dict) -> List[Tuple[str, str, float]]:
    """주차 기사 제목 중 고z 키워드를 포함하는 제목 수집. 반환 (title, keyword, z)."""
    vcfg = _vcfg()
    max_per_kw = int(cfg.get("max_titles_per_keyword", 15))
    max_total = int(cfg.get("max_total_titles", 8000))
    kw_set = list(keywords)
    per_kw_count: Dict[str, int] = defaultdict(int)
    collected: List[Tuple[str, str, float]] = []
    z_by_kw = {k: z for k, z in keywords_z}  # set below

    seen_titles: set[str] = set()
    for art in iter_week_articles(
        week,
        logger=logger,
        qdrant_url=qdrant_url() or vcfg.get("qdrant_url", "http://localhost:6333"),
        collection=vcfg.get("collection", "news_10y_ko_v1"),
        timeout_sec=float(vcfg.get("timeout_sec", 30)),
    ):
        title = (art.get("title") or "").strip()
        if not title or title in seen_titles:
            continue
        for kw in kw_set:
            if per_kw_count[kw] >= max_per_kw:
                continue
            if kw and kw in title:
                collected.append((title, kw, float(z_by_kw.get(kw, 0.0))))
                per_kw_count[kw] += 1
                seen_titles.add(title)
                break
        if len(collected) >= max_total:
            break
    return collected


# module-level holder set by run_clustering (avoids threading z map through calls)
keywords_z: List[Tuple[str, float]] = []


def _ctfidf_terms(cluster_titles: Dict[int, List[str]], top_terms: int) -> Dict[int, List[str]]:
    """class-TF-IDF: 각 군집을 한 문서로 보고 군집 간 TF-IDF 상위 토큰 추출."""
    cluster_ids = sorted(cluster_titles.keys())
    docs_tokens: Dict[int, Counter] = {}
    df_count: Counter = Counter()
    for cid in cluster_ids:
        toks: Counter = Counter()
        for t in cluster_titles[cid]:
            toks.update(_TOKEN_RE.findall(t))
        docs_tokens[cid] = toks
        for term in toks:
            df_count[term] += 1
    n_docs = max(1, len(cluster_ids))
    out: Dict[int, List[str]] = {}
    for cid in cluster_ids:
        toks = docs_tokens[cid]
        total = sum(toks.values()) or 1
        scored = []
        for term, cnt in toks.items():
            if len(term) < 2:
                continue
            tf = cnt / total
            idf = np.log((1 + n_docs) / (1 + df_count[term])) + 1.0
            scored.append((term, tf * idf))
        scored.sort(key=lambda x: x[1], reverse=True)
        out[cid] = [t for t, _ in scored[:top_terms]]
    return out


def _llm_theme(terms: List[str], sample_titles: List[str]) -> str:
    """대표어+샘플제목으로 4~12자 한국어 카테고리 테마 라벨 생성(llm_factory)."""
    if not terms and not sample_titles:
        return ""
    role = _cfg().get("theme_llm_role", "cluster_theme")
    prompt = (
        "다음은 한 뉴스 군집의 대표 키워드와 기사 제목 예시다. "
        "이 군집을 1인 셀러 관점의 짧은 카테고리명(4~12자, 한국어, 1개만)으로 요약하라.\n"
        f"대표어: {', '.join(terms[:8])}\n"
        f"제목예시: {' / '.join(sample_titles[:5])}\n"
        "카테고리명만 출력:"
    )
    try:
        out = get_llm(role).invoke(prompt).strip().splitlines()
        return (out[0].strip().strip('"').strip("'")[:30]) if out else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("테마 LLM 실패: %s", exc)
        return ""


def _reduce_and_cluster(vectors: np.ndarray, cfg: dict) -> Tuple[np.ndarray, int]:
    """UMAP 축소 + HDBSCAN 군집. 실패 시 (라벨 0, reduced_dim 0)."""
    n = vectors.shape[0]
    reduced = vectors
    reduced_dim = vectors.shape[1] if vectors.ndim == 2 else 0
    try:
        import umap  # noqa

        um = cfg.get("umap", {})
        n_comp = min(int(um.get("n_components", 5)), max(2, n - 2))
        reducer = umap.UMAP(
            n_neighbors=min(int(um.get("n_neighbors", 15)), max(2, n - 1)),
            n_components=n_comp,
            min_dist=float(um.get("min_dist", 0.0)),
            metric=um.get("metric", "cosine"),
        )
        reduced = reducer.fit_transform(vectors)
        reduced_dim = reduced.shape[1]
    except Exception as exc:  # noqa: BLE001
        logger.warning("UMAP 축소 skip(원본 임베딩 사용): %s", exc)
    try:
        import hdbscan

        hp = cfg.get("hdbscan", {})
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=int(hp.get("min_cluster_size", 5)),
            min_samples=int(hp.get("min_samples", 1)),
            metric=hp.get("metric", "euclidean"),
        )
        labels = clusterer.fit_predict(reduced)
    except Exception as exc:  # noqa: BLE001
        logger.warning("HDBSCAN 실패 → 단일 군집 폴백: %s", exc)
        labels = np.zeros(n, dtype=int)
    return labels, reduced_dim


def run_clustering(
    target_week: str,
    *,
    mode: Optional[str] = None,
    run_interpretation: bool = True,
) -> Dict[str, Any]:
    """주차 군집화 실행 → clusters/cluster_keywords 적재. 반환 요약."""
    cfg = _cfg()
    if not cfg.get("enabled", True):
        logger.info("clustering 비활성화(config) — skip")
        return {"week": target_week, "clusters": 0, "skipped": True}

    mode = mode or cfg.get("mode", "article_title")
    top_n = int(cfg.get("top_z_keywords", 30))
    kw_df = _high_z_keywords(target_week, top_n)
    if kw_df.empty:
        logger.warning("[%s] 고z 키워드 없음 — 군집화 skip", target_week)
        return {"week": target_week, "clusters": 0}

    global keywords_z
    keywords_z = list(zip(kw_df["keyword"].tolist(), kw_df["z_score"].tolist()))
    z_map = dict(keywords_z)

    # 1) 군집 대상 텍스트 구성
    items: List[Tuple[str, str, float]] = []  # (text, keyword, z)
    if mode == "article_title":
        try:
            items = _collect_titles(target_week, kw_df["keyword"].tolist(), cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] 기사제목 수집 실패 → keyword_direct 폴백: %s", target_week, exc)
            items = []
    if not items:
        mode = "keyword_direct"
        items = [(kw, kw, z) for kw, z in keywords_z]
    logger.info("[%s] 군집 대상=%d (mode=%s)", target_week, len(items), mode)

    texts = [it[0] for it in items]
    if len(texts) < 3:
        # 너무 적으면 단일 군집 처리
        labels = np.zeros(len(texts), dtype=int)
        reduced_dim = 0
        emb_dim = 0
    else:
        vectors = embed_texts(texts, normalize=True)
        emb_dim = int(vectors.shape[1]) if vectors.size else 0
        labels, reduced_dim = _reduce_and_cluster(vectors, cfg)

    include_noise = bool(cfg.get("include_noise_cluster", False))

    # 2) 군집별 집계
    cluster_titles: Dict[int, List[str]] = defaultdict(list)
    cluster_kw_votes: Dict[str, Counter] = defaultdict(Counter)  # keyword -> {cluster: count}
    for (text, kw, _z), lab in zip(items, labels):
        lab = int(lab)
        if lab == -1 and not include_noise:
            continue
        cluster_titles[lab].append(text)
        cluster_kw_votes[kw][lab] += 1

    if not cluster_titles:
        logger.warning("[%s] 유효 군집 없음", target_week)
        return {"week": target_week, "clusters": 0}

    terms_by_cluster = _ctfidf_terms(cluster_titles, int(cfg.get("ctfidf_top_terms", 8)))

    # 키워드 → 대표 군집(다수결)
    kw_cluster: Dict[str, int] = {}
    for kw, votes in cluster_kw_votes.items():
        kw_cluster[kw] = votes.most_common(1)[0][0]

    cluster_members: Dict[int, List[str]] = defaultdict(list)
    for kw, cid in kw_cluster.items():
        cluster_members[cid].append(kw)

    # 3) DB 행 구성
    cluster_rows = []
    ck_rows = []
    interp_inputs = []
    for cid in sorted(cluster_titles.keys()):
        members = cluster_members.get(cid, [])
        zs = [z_map.get(k, 0.0) for k in members] or [0.0]
        terms = terms_by_cluster.get(cid, [])
        theme = _llm_theme(terms, cluster_titles[cid])
        rep_terms = "|".join(terms)
        avg_z = float(np.mean(zs))
        max_z = float(np.max(zs))
        cluster_rows.append((cid, theme, rep_terms, len(members), avg_z, max_z, emb_dim, reduced_dim))
        for k in members:
            ck_rows.append((cid, k, float(z_map.get(k, 0.0))))
        interp_inputs.append({
            "cluster_id": cid, "cluster_theme": theme, "keyword_count": len(members),
            "avg_z_score": avg_z, "representative_terms": rep_terms, "keywords": members,
        })

    repo.replace_clusters(target_week, cluster_rows)
    repo.replace_cluster_keywords(target_week, ck_rows)
    logger.info("[%s] clusters=%d, cluster_keywords=%d 적재", target_week, len(cluster_rows), len(ck_rows))

    interp_count = 0
    if run_interpretation:
        try:
            from steps.cluster_interpretation import run_cluster_interpretation

            interp_count = run_cluster_interpretation(target_week, interp_inputs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] cluster_interpretation 실패: %s", target_week, exc)

    return {
        "week": target_week, "mode": mode, "clusters": len(cluster_rows),
        "cluster_keywords": len(ck_rows), "interpretations": interp_count,
    }


def main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="주차 군집화 (Stage5B)")
    p.add_argument("--week", required=True, help="대상 주차 YYYY-Www")
    p.add_argument("--mode", default=None, choices=["article_title", "keyword_direct"])
    p.add_argument("--no-interpretation", action="store_true")
    args = p.parse_args()
    out = run_clustering(args.week, mode=args.mode, run_interpretation=not args.no_interpretation)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

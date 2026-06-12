"""군집 K(트렌드 입도) 오프라인 스윕 — 임베딩 캐시 재사용으로 초 단위 반복.

배경: 점수의 병목(coherence·single_rate·label_div)은 전부 군집 산물이라 linker 파라미터로는
한계. 그런데 매주 재군집은 임베딩(4분/주) 때문에 느림. 그래서 SQUADONE_SENSE_CACHE_DIR 로
1회 덤프한 (sentences·vectors·sent_weight·sent_kws·top_kws) 를 로드해, 같은
compute_week_trend_senses 를 캐시된 embed_fn 으로 재호출 → K 만 바꿔 군집/link/eval 반복.

캐시 생성(1회):
  SQUADONE_SENSE_CACHE_DIR=data/sense_cache python -m scripts.run_step 1 \
      --start-date 2025-05-26 --end-date 2025-06-29

스윕:
  python -m scripts.sweep_k --cache-dir data/sense_cache [--write]
"""
from __future__ import annotations

import argparse
import glob
import os
import pickle
from typing import Dict, List

import numpy as np

from db.config import load_shared_env
from steps.keyword_sense import compute_week_trend_senses
from steps.trend_linker import link_threads
from scripts.tune_linker import score_linking

# K+linker 동시 튜닝 최적(top-2 라벨, 인물 포함). overall 0.7121
# (coh 0.52 / anc 0.909 / div 0.86 / single 0.66). 목표 0.65 초과.
BEST_LINK = dict(
    alpha=0.15, beta=0.85, theta_near=0.42, theta_gap=0.47,
    min_ent_jaccard_near=0.10, min_ent_jaccard=0.25, max_gap_weeks=30,
)

# (target_size, kmin, kmax) 스윕 후보 — 최적 K~100/주 부근 + 비교용 양끝.
GRID = [
    (180, 10, 90),
    (150, 10, 100),   # ★ 최적(overall 0.7121)
    (135, 10, 110),
    (80, 15, 250),
    (40, 20, 600),
    (20, 20, 1200),   # 구 운영값(과granular, 비교용)
]


def _load_caches(cache_dir: str) -> List[dict]:
    paths = sorted(glob.glob(os.path.join(cache_dir, "sense_cache_*.pkl")))
    caches = []
    for p in paths:
        with open(p, "rb") as fh:
            caches.append(pickle.load(fh))
    return caches


def _cached_embed_fn(cache: dict):
    s2v = {s: v for s, v in zip(cache["sentences"], cache["vectors"])}
    dim = cache["vectors"].shape[1] if len(cache["vectors"]) else 0

    def embed(sentences):
        return np.asarray(
            [s2v.get(s, np.zeros(dim, dtype=np.float32)) for s in sentences],
            dtype=np.float32,
        )

    return embed


def _clusters_for_combo(caches: List[dict], target_size: int, kmin: int, kmax: int):
    """모든 주차에 대해 주어진 K 로 군집 → link_threads 입력용 cluster dict 리스트."""
    clusters: List[Dict[str, object]] = []
    per_week_counts = {}
    for cache in caches:
        week = cache["week"]
        from collections import Counter as _C
        sent_kws = {s: _C(d) for s, d in cache["sent_kws"].items()}
        _sense, trend_rows = compute_week_trend_senses(
            week,
            cache["top_kws"],
            cache["sent_weight"],
            sent_kws,
            embed_fn=_cached_embed_fn(cache),
            cluster_target_size=target_size,
            cluster_kmin=kmin,
            cluster_kmax=kmax,
        )
        per_week_counts[week] = len(trend_rows)
        # trend_rows: (week, cid, size, weight, label, rep, top_keywords, centroid)
        for r in trend_rows:
            clusters.append({
                "week": r[0], "cluster_id": int(r[1]), "size": int(r[2]),
                "weight": int(r[3]), "label": r[4] or "", "rep_sentence": r[5] or "",
                "top_keywords": r[6] or "", "centroid": list(r[7] or []),
            })
    return clusters, per_week_counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default="data/sense_cache")
    ap.add_argument("--write", action="store_true", help="최고 K 결과를 DB(clusters+threads)에 적재")
    a = ap.parse_args()
    load_shared_env()

    caches = _load_caches(a.cache_dir)
    if not caches:
        print(f"캐시 없음: {a.cache_dir} (먼저 SQUADONE_SENSE_CACHE_DIR 로 step1 1회 실행)")
        return 1
    weeks = [c["week"] for c in caches]
    print(f"캐시 {len(caches)}주 로드: {weeks}  (문장수={[len(c['sentences']) for c in caches]})")
    print(f"링크 파라미터(tune_linker best): {BEST_LINK}\n")

    best = None
    for target_size, kmin, kmax in GRID:
        clusters, counts = _clusters_for_combo(caches, target_size, kmin, kmax)
        tr, mr = link_threads(clusters, **BEST_LINK)
        s = score_linking(tr, clusters)
        avg_k = sum(counts.values()) / max(1, len(counts))
        tag = f"ts={target_size} kmin={kmin} kmax={kmax}"
        print(f"  [{tag:32}] K~{avg_k:5.0f}/주 | overall={s['overall']} "
              f"coh={s['coherence']} anc={s['anchor_purity']} "
              f"div={s['label_div']} sgl={s['single_rate']} multi={s['multi']}")
        if best is None or s["overall"] > best[0]["overall"]:
            best = (s, (target_size, kmin, kmax), clusters, tr, mr)

    s, combo, clusters, tr, mr = best
    print("\n=== BEST K ===")
    print(f"  (target_size, kmin, kmax) = {combo}")
    print(f"  score: {s}")

    if a.write:
        from db import repository as repo
        from collections import defaultdict
        by_week = defaultdict(list)
        for c in clusters:
            by_week[c["week"]].append(c)
        for week, cs in by_week.items():
            rows = [
                (c["week"], c["cluster_id"], c["size"], c["weight"],
                 c["label"], c["rep_sentence"], c["top_keywords"], c["centroid"])
                for c in cs
            ]
            repo.replace_trend_clusters(week, rows)
        repo.replace_trend_threads(min(weeks), max(weeks), tr, mr)
        print(f"  → DB 적재 완료: clusters {len(clusters)}행 + threads {len(mr)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

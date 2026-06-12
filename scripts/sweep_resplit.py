"""군집 입도/재분할 실험 — 현재 운영 베이스(FINAL_NOISE + 동사 액션라벨)에서
kmax 상한을 올려 군집 과대화를 풀었을 때 점수·응집도가 어떻게 변하는지 측정.

diag_cohesion 진단 결과: target_size=135 를 원하지만 kmax=110 상한 때문에 군집이
의도의 4배(~520문장)로 과대 → 한 군집에 여러 하위주제. kmax 를 올리면 군집이
target 크기에 수렴(응집도↑)하지만 주간 재현성(coherence/single)이 어떻게 되는지는
메트릭으로 확인해야 한다. 캐시라 초 단위.

사용: .venv/bin/python -m scripts.sweep_resplit --cache-dir data/sense_cache
"""
from __future__ import annotations

import argparse
from collections import Counter

import numpy as np

from db.config import load_shared_env
from steps.keyword_sense import compute_week_trend_senses, _cluster_sentences_global
from steps.trend_linker import link_threads
from scripts.tune_linker import score_linking
from scripts.sweep_k import _load_caches, _cached_embed_fn, BEST_LINK
from scripts.sweep_noise import _apply_noise, FINAL_NOISE

# (target_size, kmin, kmax) — baseline(현재 운영) + kmax 상향.
GRID = [
    (135, 10, 110),   # ★ 현재 운영(baseline)
    (135, 10, 160),
    (135, 10, 220),
    (135, 10, 320),
    (135, 10, 500),
    (100, 10, 500),
]


def _build(caches, ts, kmin, kmax):
    """FINAL_NOISE + action_words 로 군집(운영과 동일). 응집도도 함께 계산."""
    clusters = []
    cohs = []
    sizes = []
    for cache in caches:
        sw, sk, top_kws = _apply_noise(cache, FINAL_NOISE)
        actions = set(cache.get("action_words", [])) - FINAL_NOISE
        sents = list(sw.keys())
        embed = _cached_embed_fn(cache)
        _sense, trend_rows = compute_week_trend_senses(
            cache["week"], top_kws, sw, sk,
            embed_fn=embed, action_words=actions,
            cluster_target_size=ts, cluster_kmin=kmin, cluster_kmax=kmax,
        )
        for r in trend_rows:
            clusters.append({
                "week": r[0], "cluster_id": int(r[1]), "size": int(r[2]),
                "weight": int(r[3]), "label": r[4] or "", "rep_sentence": r[5] or "",
                "top_keywords": r[6] or "", "centroid": list(r[7] or []),
            })
            sizes.append(int(r[2]))
        # 응집도: centroid(trend_rows 에 저장됨) ↔ 멤버. 재군집 없이 라벨로 재현 불가하니
        # 동일 K 로 한 번 더 군집(결정적, random_state=0)해 멤버-응집도 계산.
        vecs = embed(sents)
        labels, centers = _cluster_sentences_global(vecs, target_size=ts, kmin=kmin, kmax=kmax)
        labels = np.asarray(labels)
        for c in range(len(centers)):
            mask = labels == c
            if not mask.any():
                continue
            cen = centers[c] / (np.linalg.norm(centers[c]) + 1e-9)
            cohs.append(float((vecs[mask] @ cen).mean()))
    return clusters, np.array(cohs), np.array(sizes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default="data/sense_cache")
    a = ap.parse_args()
    load_shared_env()
    caches = _load_caches(a.cache_dir)
    print(f"캐시 {len(caches)}주 · noise={len(FINAL_NOISE)} · linker=BEST_LINK\n")
    print(f"{'(ts,kmin,kmax)':18} {'overall':>7} {'coh':>6} {'anc':>6} {'div':>6} "
          f"{'sgl':>6} {'cls':>5} {'cohₘ':>6} {'sizeₘ':>6}")
    for ts, kmin, kmax in GRID:
        clusters, cohs, sizes = _build(caches, ts, kmin, kmax)
        tr, mr = link_threads(clusters, **BEST_LINK)
        s = score_linking(tr, clusters)
        tag = f"({ts},{kmin},{kmax})"
        star = " ★" if (ts, kmin, kmax) == (135, 10, 110) else ""
        print(f"{tag:18} {s['overall']:7.4f} {s['coherence']:6.3f} {s['anchor_purity']:6.3f} "
              f"{s['label_div']:6.3f} {s['single_rate']:6.3f} {len(clusters):5} "
              f"{cohs.mean():6.3f} {int(np.median(sizes)):6}{star}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

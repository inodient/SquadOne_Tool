"""꼬리 이탈문장 trim 스윕 — 운영 베이스(FINAL_NOISE+동사라벨, K=FINAL_K)에서
trim_sim 임계값만 바꿔 점수·군집수·라벨을 비교. 군집은 1회(빠름).

진단(diag_cohesion): 군집은 "응집 코어 + 이탈 꼬리" 구조. trim 은 코어를 건드리지 않고
꼬리만 떼어 라벨 정화 + 응집도↑ 를 노린다(군집 수 불변, single_rate 영향 최소 기대).

사용: .venv/bin/python -m scripts.sweep_trim --cache-dir data/sense_cache
"""
from __future__ import annotations

import argparse

from db.config import load_shared_env
from steps.keyword_sense import compute_week_trend_senses
from steps.trend_linker import link_threads
from scripts.tune_linker import score_linking
from scripts.sweep_k import _load_caches, _cached_embed_fn, BEST_LINK
from scripts.sweep_noise import _apply_noise, FINAL_NOISE, FINAL_K

TRIMS = [0.0, 0.25, 0.30, 0.35, 0.40, 0.45]


def _build(caches, trim_sim):
    ts, kmin, kmax = FINAL_K
    clusters = []
    for cache in caches:
        sw, sk, top_kws = _apply_noise(cache, FINAL_NOISE)
        actions = set(cache.get("action_words", [])) - FINAL_NOISE
        _sense, trend_rows = compute_week_trend_senses(
            cache["week"], top_kws, sw, sk,
            embed_fn=_cached_embed_fn(cache), action_words=actions,
            cluster_target_size=ts, cluster_kmin=kmin, cluster_kmax=kmax,
            trim_sim=trim_sim,
        )
        for r in trend_rows:
            clusters.append({
                "week": r[0], "cluster_id": int(r[1]), "size": int(r[2]),
                "weight": int(r[3]), "label": r[4] or "", "rep_sentence": r[5] or "",
                "top_keywords": r[6] or "", "centroid": list(r[7] or []),
            })
    return clusters


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default="data/sense_cache")
    a = ap.parse_args()
    load_shared_env()
    caches = _load_caches(a.cache_dir)
    print(f"캐시 {len(caches)}주 · K={FINAL_K} · noise={len(FINAL_NOISE)}\n")
    print(f"{'trim_sim':>9} {'overall':>7} {'coh':>6} {'anc':>6} {'div':>6} {'sgl':>6} {'cls':>5}")
    base_w24 = None
    for t in TRIMS:
        clusters = _build(caches, t)
        tr, mr = link_threads(clusters, **BEST_LINK)
        s = score_linking(tr, clusters)
        star = " ★" if t == 0.0 else ""
        print(f"{t:9.2f} {s['overall']:7.4f} {s['coherence']:6.3f} {s['anchor_purity']:6.3f} "
              f"{s['label_div']:6.3f} {s['single_rate']:6.3f} {len(clusters):5}{star}")
        w24 = sorted([c for c in clusters if c["week"] == "2025-W24"], key=lambda c: -c["weight"])[:6]
        labels = " / ".join(c["label"] for c in w24)
        if t == 0.0:
            base_w24 = labels
        print(f"          W24상위: {labels}")
    print(f"\n(baseline W24: {base_w24})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

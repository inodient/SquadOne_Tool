"""군집 응집도 진단 — 현재 운영 설정(FINAL_NOISE@FINAL_K)에서 어떤 군집이
이질주제를 섞고 있는지 정량 측정. 재분할 전략을 데이터로 결정하기 위한 사전 진단.

응집도 = 군집 멤버 벡터와 centroid 의 평균 코사인 유사도(높을수록 단일주제).
캐시 벡터는 정규화돼 있고 KMeans centroid 는 여기서 재정규화한다.

사용: .venv/bin/python -m scripts.diag_cohesion --cache-dir data/sense_cache [--top 12]
"""
from __future__ import annotations

import argparse
from collections import Counter

import numpy as np

from db.config import load_shared_env
from steps.keyword_sense import _cluster_sentences_global
from scripts.sweep_k import _load_caches, _cached_embed_fn
from scripts.sweep_noise import _apply_noise, FINAL_NOISE, FINAL_K


def _cohesion(vecs: np.ndarray, center: np.ndarray) -> float:
    """멤버 벡터(정규화 가정) ↔ 정규화 centroid 평균 코사인."""
    c = center / (np.linalg.norm(center) + 1e-9)
    sims = vecs @ c
    return float(sims.mean())


def _farthest(vecs: np.ndarray, center: np.ndarray, sents, k: int):
    """centroid 에서 가장 먼 멤버 문장 k개(이질혼합 증거)."""
    c = center / (np.linalg.norm(center) + 1e-9)
    sims = vecs @ c
    idx = np.argsort(sims)[:k]
    return [(sents[i], float(sims[i])) for i in idx]


def _nearest(vecs: np.ndarray, center: np.ndarray, sents):
    c = center / (np.linalg.norm(center) + 1e-9)
    sims = vecs @ c
    i = int(np.argmax(sims))
    return sents[i], float(sims[i])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default="data/sense_cache")
    ap.add_argument("--top", type=int, default=12, help="저응집 군집 표시 개수(주당)")
    ap.add_argument("--week", default=None, help="특정 주만(미지정=전체)")
    a = ap.parse_args()
    load_shared_env()

    caches = _load_caches(a.cache_dir)
    ts, kmin, kmax = FINAL_K
    print(f"진단: FINAL_K=(ts={ts}, kmin={kmin}, kmax={kmax}) · noise={len(FINAL_NOISE)}단어\n")

    all_coh = []
    for cache in caches:
        week = cache["week"]
        if a.week and week != a.week:
            continue
        sw, sk, top_kws = _apply_noise(cache, FINAL_NOISE)
        sents = list(sw.keys())
        vecs = _cached_embed_fn(cache)(sents)
        labels, centers = _cluster_sentences_global(
            vecs, target_size=ts, kmin=kmin, kmax=kmax
        )
        labels = np.asarray(labels)

        # 군집별 응집도/크기/weight
        rows = []
        for c in range(len(centers)):
            mask = labels == c
            cnt = int(mask.sum())
            if cnt == 0:
                continue
            mvecs = vecs[mask]
            msents = [sents[i] for i, m in enumerate(mask) if m]
            weight = sum(sw[s] for s in msents)
            coh = _cohesion(mvecs, centers[c])
            rows.append((c, cnt, weight, coh, mvecs, msents))
            all_coh.append(coh)

        cohs = np.array([r[3] for r in rows])
        sizes = np.array([r[1] for r in rows])
        print(f"=== {week} | 군집={len(rows)} 문장={len(sents)} | "
              f"응집도 median={np.median(cohs):.3f} p25={np.percentile(cohs,25):.3f} "
              f"min={cohs.min():.3f} | size median={int(np.median(sizes))} max={int(sizes.max())} ===")

        # 저응집 우선 표시(weight 가중: 크고 혼탁한 게 가장 문제)
        worst = sorted(rows, key=lambda r: r[3])[:a.top]
        for c, cnt, weight, coh, mvecs, msents in worst:
            nrep, nsim = _nearest(mvecs, centers[c], msents)
            far = _farthest(mvecs, centers[c], msents, 2)
            kwc: Counter = Counter()
            for s in msents:
                for k, v in sk.get(s, {}).items():
                    kwc[k] += v
            topk = ", ".join(k for k, _ in kwc.most_common(8))
            print(f"  [c{c:3} coh={coh:.3f} n={cnt:4} w={int(weight):5}] kw: {topk}")
            print(f"      중심: {nrep[:70]}  (sim={nsim:.2f})")
            for s, sim in far:
                print(f"      이탈: {s[:70]}  (sim={sim:.2f})")
        print()

    if all_coh:
        ac = np.array(all_coh)
        print(f"── 전체 군집 {len(ac)}개 응집도: mean={ac.mean():.3f} median={np.median(ac):.3f} "
              f"p10={np.percentile(ac,10):.3f} p25={np.percentile(ac,25):.3f} min={ac.min():.3f}")
        for thr in (0.35, 0.40, 0.45, 0.50):
            print(f"   coh<{thr}: {int((ac<thr).sum())}개 ({100*(ac<thr).mean():.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

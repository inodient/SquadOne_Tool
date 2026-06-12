"""describability 정책 결정용 — 명사판 vs 동사판 라벨을 같은 군집 위에서 나란히 비교.

군집(임베딩/labels)은 action_words 와 무관하게 동일하고, weight 도 동일 → cluster_id
remap 도 동일하므로 cluster_id 로 1:1 매칭된다. action_words 만 바꿔(빈 set=명사판,
캐시값=동사판) 두 번 군집해 라벨만 비교한다.

describability 프록시:
  - action_rate : 라벨에 액션(동사/동작명사)이 1개 이상 들어간 트렌드 비율
                  = "이 트렌드가 무슨 '사건/동작'인지" 라벨이 드러내는 비율.
  - avg_tokens  : 라벨 평균 토큰 수(서술 풍부도).
  - 정성: 상위 트렌드 라벨 + 다주 thread 라벨 궤적을 명사/동사 나란히.

사용: .venv/bin/python -m scripts.compare_describability --cache-dir data/sense_cache
"""
from __future__ import annotations

import argparse

import numpy as np

from db.config import load_shared_env
from steps.keyword_sense import compute_week_trend_senses
from steps.trend_linker import link_threads
from scripts.sweep_k import _load_caches, _cached_embed_fn, BEST_LINK
from scripts.sweep_noise import _apply_noise, FINAL_NOISE, FINAL_K


def _build(caches, with_actions: bool):
    ts, kmin, kmax = FINAL_K
    clusters = []
    for cache in caches:
        sw, sk, top_kws = _apply_noise(cache, FINAL_NOISE)
        actions = (set(cache.get("action_words", [])) - FINAL_NOISE) if with_actions else set()
        _sense, trend_rows = compute_week_trend_senses(
            cache["week"], top_kws, sw, sk,
            embed_fn=_cached_embed_fn(cache), action_words=actions,
            cluster_target_size=ts, cluster_kmin=kmin, cluster_kmax=kmax,
        )
        for r in trend_rows:
            clusters.append({
                "week": r[0], "cluster_id": int(r[1]), "size": int(r[2]),
                "weight": int(r[3]), "label": r[4] or "", "rep_sentence": r[5] or "",
                "top_keywords": r[6] or "", "centroid": list(r[7] or []),
            })
    return clusters


def _action_set(caches):
    s = set()
    for c in caches:
        s |= set(c.get("action_words", []))
    return s - FINAL_NOISE


def _describability(clusters, actions):
    """라벨 기준 describability 프록시."""
    n = len(clusters)
    act_hit = 0
    tok_total = 0
    for c in clusters:
        toks = (c["label"] or "").split()
        tok_total += len(toks)
        if any(t in actions for t in toks):
            act_hit += 1
    return {
        "action_rate": round(act_hit / n, 3) if n else 0.0,
        "avg_tokens": round(tok_total / n, 2) if n else 0.0,
        "distinct": round(len({c["label"] for c in clusters}) / n, 3) if n else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default="data/sense_cache")
    ap.add_argument("--week", default="2025-W24")
    a = ap.parse_args()
    load_shared_env()
    caches = _load_caches(a.cache_dir)
    actions = _action_set(caches)

    noun = _build(caches, with_actions=False)
    verb = _build(caches, with_actions=True)

    dn = _describability(noun, actions)
    dv = _describability(verb, actions)
    print("=== describability 프록시 (명사판 vs 동사판) ===")
    print(f"  {'':10} {'action_rate':>12} {'avg_tokens':>11} {'distinct':>9}")
    print(f"  {'명사판':10} {dn['action_rate']:12} {dn['avg_tokens']:11} {dn['distinct']:9}")
    print(f"  {'동사판':10} {dv['action_rate']:12} {dv['avg_tokens']:11} {dv['distinct']:9}")

    # 같은 cluster_id 매칭 — 상위 트렌드 라벨 나란히
    nmap = {(c["week"], c["cluster_id"]): c for c in noun}
    vrows = sorted([c for c in verb if c["week"] == a.week], key=lambda c: -c["weight"])[:12]
    print(f"\n=== {a.week} 상위 트렌드: [명사판] → [동사판] ===")
    for v in vrows:
        nlab = nmap.get((v["week"], v["cluster_id"]), {}).get("label", "")
        print(f"  w={v['weight']:5} [{nlab:18}] → [{v['label']:22}]  (kw: {v['top_keywords'][:40]})")

    # 다주 thread 라벨 궤적 — 동사판 thread 기준, 같은 멤버의 명사 라벨도 표시
    tr_v, _ = link_threads(verb, **BEST_LINK)
    by_thread = {}
    for tid, week, cid, seq in [(r[0], r[1], r[2], r[3]) for r in tr_v]:
        by_thread.setdefault(tid, []).append((seq, week, cid))
    multi = [(tid, ms) for tid, ms in by_thread.items() if len({m[1] for m in ms}) >= 3]
    vlab = {(c["week"], c["cluster_id"]): c["label"] for c in verb}
    print(f"\n=== 다주(≥3주) thread 라벨 궤적 (상위 5개): 동사판 / 명사판 ===")
    multi.sort(key=lambda x: -max(nmap.get((m[1], m[2]), {}).get("weight", 0) for m in x[1]))
    for tid, ms in multi[:5]:
        ms.sort()
        vseq = " → ".join(vlab.get((w, c), "?") for _, w, c in ms)
        nseq = " → ".join(nmap.get((w, c), {}).get("label", "?") for _, w, c in ms)
        print(f"  동사: {vseq}")
        print(f"  명사: {nseq}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

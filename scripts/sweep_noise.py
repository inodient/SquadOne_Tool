"""노이즈 키워드(필러어 등) 빠른 실험 — 재추출 없이 캐시 후처리로 초 단위 평가.

키워드 단위 노이즈는 임베딩에 영향이 없다(문장 임베딩은 고정). 캐시의 sent_kws 에서
노이즈 키만 빼고 키워드가 0개가 된 문장은 드롭하면, 실제 step1 재추출(noise_keywords 추가)과
'동일한' 군집/라벨을 만든다 — embed_fn 은 남은 문장만 임베딩하므로. 30분 재추출 대신 즉시 비교.

사용: python -m scripts.sweep_noise --cache-dir data/sense_cache
  (EXTRA_NOISE_SETS 의 각 후보를 적용해 점수+W24 상위 라벨 비교)
"""
from __future__ import annotations

import argparse
from collections import Counter
from typing import Dict, List, Set

import numpy as np

from db.config import load_shared_env
from steps.keyword_sense import compute_week_trend_senses
from steps.trend_linker import link_threads
from scripts.tune_linker import score_linking
from scripts.sweep_k import _load_caches, _cached_embed_fn, BEST_LINK

# 기준 K(최적). 노이즈 비교는 동일 K 고정.
K = (150, 10, 100)

# 노이즈 카테고리(상품어 출시/사업/제품 등은 보존 — 프로젝트 목표가 상품추천).
_FILLER = {"사람", "자신", "좋다", "문장", "이야기", "작가", "생각", "정도", "경우", "모습", "대로", "이번", "관련", "지난"}
_POLI = {"의원", "대표", "대통령", "국민", "원내", "위원장", "정부", "시민", "여당", "야당"}
_LOC = {"서울", "경기", "경기도", "서울시", "인천", "백운산", "지역", "세계", "전국"}

# 빈 set = baseline. 누적 비교로 각 카테고리 기여 측정.
EXTRA_NOISE_SETS: Dict[str, Set[str]] = {
    "baseline": set(),
    "필러": set(_FILLER),
    "필러+정치": _FILLER | _POLI,
    "필러+정치+지역": _FILLER | _POLI | _LOC,
}


def _apply_noise(cache: dict, extra: Set[str]):
    """캐시 sent_weight/sent_kws 에서 extra 노이즈 키 제거 + 키워드0 문장 드롭.

    동사반영 캐시는 추출 단계에서 이미 VA 제외 + VV 동사 포함이라, 여기서 다-종결을 drop 하면
    안 된다(동사가 사라짐). extra(필러/정치/지역/경동사)만 제거 → 실제 재추출과 동일 결과.
    """
    def _ok(k: str) -> bool:
        return k not in extra
    sw: Dict[str, float] = {}
    sk: Dict[str, Counter] = {}
    for sent, w in cache["sent_weight"].items():
        kws = cache["sent_kws"].get(sent, {})
        kept = {k: v for k, v in kws.items() if _ok(k)}
        if not kept:
            continue  # 노이즈만 남은 문장 제외(실제 파이프라인과 동일)
        sw[sent] = w
        sk[sent] = Counter(kept)
    top_kws = [k for k in cache["top_kws"] if _ok(k)]
    return sw, sk, top_kws


def _clusters(caches: List[dict], extra: Set[str]):
    out = []
    for cache in caches:
        sw, sk, top_kws = _apply_noise(cache, extra)
        # 라벨 [엔티티]+[액션] 조합용 action_words(캐시 보존). extra 로 뺀 경동사는 액션에서도 제외.
        actions = set(cache.get("action_words", [])) - extra
        _sense, trend_rows = compute_week_trend_senses(
            cache["week"], top_kws, sw, sk,
            embed_fn=_cached_embed_fn(cache), action_words=actions,
            cluster_target_size=K[0], cluster_kmin=K[1], cluster_kmax=K[2],
        )
        for r in trend_rows:
            out.append({
                "week": r[0], "cluster_id": int(r[1]), "size": int(r[2]),
                "weight": int(r[3]), "label": r[4] or "", "rep_sentence": r[5] or "",
                "top_keywords": r[6] or "", "centroid": list(r[7] or []),
            })
    return out


# 누설 경동사(라벨 노이즈: "여름 나오다", "배우 지나다"). _GENERIC_VERBS 보강과 동일 의도.
_VERB_NOISE = {
    "나오다", "지나다", "만나다", "나타나다", "지내다", "들어가다", "나가다", "들어오다",
    "나서다", "보내다", "다니다", "지키다", "이루다", "이르다",
}
# 최종 채택: 필러+정치+지역+경동사 @ K=110. 캐시 기반 적재(재추출 불필요).
FINAL_NOISE = _FILLER | _POLI | _LOC | _VERB_NOISE
FINAL_K = (135, 10, 110)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default="data/sense_cache")
    ap.add_argument("--write", action="store_true", help="FINAL_NOISE@FINAL_K 결과를 DB 적재")
    a = ap.parse_args()
    load_shared_env()
    caches = _load_caches(a.cache_dir)

    if a.write:
        global K
        K = FINAL_K
        clusters = _clusters(caches, FINAL_NOISE)
        tr, mr = link_threads(clusters, **BEST_LINK)
        s = score_linking(tr, clusters)
        from db import repository as repo
        from collections import defaultdict
        by_week = defaultdict(list)
        for c in clusters:
            by_week[c["week"]].append(c)
        for week, cs in by_week.items():
            rows = [(c["week"], c["cluster_id"], c["size"], c["weight"],
                     c["label"], c["rep_sentence"], c["top_keywords"], c["centroid"]) for c in cs]
            repo.replace_trend_clusters(week, rows)
        weeks = [c["week"] for c in caches]
        repo.replace_trend_threads(min(weeks), max(weeks), tr, mr)
        print(f"→ DB 적재: clusters {len(clusters)} + threads {len(mr)} | score={s}")
        return 0

    print(f"캐시 {len(caches)}주 · K={K} · linker={BEST_LINK}\n")
    for name, extra in EXTRA_NOISE_SETS.items():
        clusters = _clusters(caches, extra)
        tr, mr = link_threads(clusters, **BEST_LINK)
        s = score_linking(tr, clusters)
        w24 = sorted([c for c in clusters if c["week"] == "2025-W24"],
                     key=lambda c: -c["weight"])[:6]
        print(f"=== {name} (+{len(extra)}단어) ===")
        print(f"  overall={s['overall']} coh={s['coherence']} anc={s['anchor_purity']} "
              f"div={s['label_div']} sgl={s['single_rate']} | clusters={len(clusters)}")
        print("  W24 상위6 라벨:", " / ".join(c["label"] for c in w24))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

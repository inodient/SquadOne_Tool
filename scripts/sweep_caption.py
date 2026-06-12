"""캡션/부고/단문 정형구 필터 검증 — 거대 잡탕 군집(짧은 명사구 캡션이 임베딩상 뭉친 것)을
임베딩 전 제거하면 군집이 정상화되는지 캐시로 즉시 검증(재추출 30분 회피).

진단: 각 주 weight 1위 거대군집(sz 3000+)은 "특검 정치"가 아니라 "[부고]·X 회장.·…제공."
같은 짧은 캡션/단신 정형 단문이 문체로 뭉친 잡탕. 응집도(코사인) 0.95+ 라 코사인 진단으론
못 잡고, sub-KMeans 로도 안 갈림(문체 동일). 문장 자체를 빼야 한다.

필터를 캐시의 sent_weight/sent_kws 에서 사후 제거(키워드0 문장 드롭과 동일 메커니즘)하면
실제 재추출과 같은 군집을 만든다(embed_fn 은 남은 문장만 임베딩).

사용: .venv/bin/python -m scripts.sweep_caption --cache-dir data/sense_cache
"""
from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict

import numpy as np

from db.config import load_shared_env
from steps.keyword_sense import compute_week_trend_senses, _cluster_sentences_global
from steps.trend_linker import link_threads
from scripts.tune_linker import score_linking
from scripts.sweep_k import _load_caches, _cached_embed_fn, BEST_LINK
from scripts.sweep_noise import _apply_noise, FINAL_NOISE, FINAL_K

# 직책/호칭으로 끝나는 인물 캡션("…홍길동 회장.") · 사진/자료 캡션("…제공.") · 부고/인사 태그.
_TITLE = r"(회장|부회장|사장|대표|대표이사|의원|장관|차관|위원장|위원|교수|소방서|경찰서|본부장|이사장|청장|시장|지사|총장|원장|국장|과장|부장|팀장|단장|감독|작가|배우|가수|선수)"
_CAPTION = re.compile(
    r"^\[(부고|인사|동정|포토|화보|사진|그래픽|표|일정|날씨|증시|환율|라운지|게시판|경제계 인사|동정|모집)\]"  # 태그형
    r"|" + _TITLE + r"\s*[.·]?\s*$"                                          # 직책 끝 단문
    r"|(제공|조감도|자료사진|갈무리|캡처|모습|연합뉴스|뉴시스|게티이미지)\s*[.·]?\s*$"  # 캡션 끝
)


def is_caption(s: str, max_len: int) -> bool:
    s = s.strip()
    if len(s) <= max_len:           # 아주 짧은 명사구(주어만, 서술 없음)
        return True
    return bool(_CAPTION.search(s))


def _apply(cache, extra_noise, drop_caption, cap_len, no_verb_len=0, actions=None):
    """FINAL_NOISE 적용 + (옵션) 캡션 문장 드롭 + (옵션) 동사없는 짧은 명사구 드롭.

    no_verb_len>0: 길이 < no_verb_len 이면서 action_words(동사/동작명사)가 하나도 없는
    문장을 제외(= 서술어 없는 캡션/단신 명사구). 의미있는 짧은 제목은 동사가 있어 보존.
    """
    actions = actions or set()
    sw, sk = {}, {}
    for sent, w in cache["sent_weight"].items():
        if drop_caption and is_caption(sent, cap_len):
            continue
        kws = cache["sent_kws"].get(sent, {})
        kept = {k: v for k, v in kws.items() if k not in extra_noise}
        if not kept:
            continue
        if no_verb_len and len(sent.strip()) < no_verb_len and not (set(kept) & actions):
            continue
        sw[sent] = w
        sk[sent] = Counter(kept)
    top_kws = [k for k in cache["top_kws"] if k not in extra_noise]
    return sw, sk, top_kws


def _run(caches, drop_caption, cap_len, no_verb_len=0):
    ts, kmin, kmax = FINAL_K
    clusters = []
    max_sizes = []
    dropped = 0
    for cache in caches:
        actions = set(cache.get("action_words", [])) - FINAL_NOISE
        sw, sk, top_kws = _apply(cache, FINAL_NOISE, drop_caption, cap_len, no_verb_len, actions)
        dropped += len(cache["sent_weight"]) - len(sw)
        actions = set(cache.get("action_words", [])) - FINAL_NOISE
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
        max_sizes.append(max((r[2] for r in trend_rows), default=0))
    return clusters, max_sizes, dropped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default="data/sense_cache")
    a = ap.parse_args()
    load_shared_env()
    caches = _load_caches(a.cache_dir)
    total = sum(len(c["sent_weight"]) for c in caches)
    print(f"캐시 {len(caches)}주 · 총문장={total} · K={FINAL_K}\n")

    configs = [
        ("baseline(필터없음)",        False, 0, 0),
        ("캡션필터 only",              True, 0, 0),
        ("캡션+동사없는단문<25자",      True, 0, 25),
        ("캡션+동사없는단문<30자",      True, 0, 30),
    ]
    for name, drop, clen, nvl in configs:
        clusters, max_sizes, dropped = _run(caches, drop, clen, nvl)
        tr, mr = link_threads(clusters, **BEST_LINK)
        s = score_linking(tr, clusters)
        w26 = sorted([c for c in clusters if c["week"] == "2025-W26"], key=lambda c: -c["weight"])
        top = w26[0] if w26 else {}
        print(f"=== {name} | 드롭={dropped}문장({100*dropped/total:.0f}%) ===")
        print(f"  overall={s['overall']:.4f} coh={s['coherence']:.3f} anc={s['anchor_purity']:.3f} "
              f"div={s['label_div']:.3f} sgl={s['single_rate']:.3f} | 주별최대군집={max_sizes}")
        print(f"  W26 1위군집: '{top.get('label','')}' sz={top.get('size')} w={top.get('weight')} "
              f"kw={top.get('top_keywords','')[:42]}")
        print(f"     rep: {top.get('rep_sentence','')[:60]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

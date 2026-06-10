"""키워드 "의미 분화"(sense) — 방법 B: 문맥 임베딩 군집.

다의어("가격"=아파트/생리대/농산물…)를 맥락 클러스터로 갈라 의미 그룹으로 나눈다.
입력은 1단계가 모은 `ctx_counter[kw]` = Counter{(앞2어절, 뒤2어절): occ_count}.

설계 — 임베딩 호출(무거움, torch 필요)과 군집 로직(가벼움, 순수)을 분리한다:
  - `cluster_vectors` / `build_keyword_senses`: 임베딩 벡터를 "주입"받는 순수 함수.
    → torch 없는 환경에서도 mock 벡터로 단위검증 가능.
  - `default_embed_fn`: 실제 임베딩(steps.qdrant_embed.embed_texts 재사용). 맥미니에서 동작.
  - `compute_week_senses`: 주차 단위 오케스트레이션(평탄화 → 배치 임베딩 → 키워드별 군집).
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from typing import Callable, Dict, List, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# (week, keyword, sense_id, count, label, rep_before, rep_after, top_neighbors, rep_sentence)
# rep_sentence: 문장 모드의 대표 문장(전체). 어절 모드는 "" 로 둔다.
SenseRow = Tuple[str, str, int, int, str, str, str, str, str]

# 임베딩 함수 시그니처: List[str] -> np.ndarray (n, dim), 정규화 벡터.
EmbedFn = Callable[[List[str]], np.ndarray]


def context_sentence(keyword: str, before: str, after: str) -> str:
    """임베딩 입력 문장. 앞어절 + 키워드 + 뒤어절(공백 정규화)."""
    return " ".join(p for p in (before, keyword, after) if p).strip()


def _neighbor_counter(items: Sequence[Tuple[Tuple[str, str], int]]) -> Counter:
    """클러스터 내 (before, after) 맥락들의 어절을 occ 가중으로 집계."""
    c: Counter = Counter()
    for (before, after), occ in items:
        for tok in (before + " " + after).split():
            c[tok] += occ
    return c


def cluster_vectors(
    vectors: np.ndarray,
    *,
    distance_threshold: float,
) -> List[int]:
    """코사인 거리 응집군집(agglomerative). 각 샘플의 클러스터 라벨 반환.

    vectors 는 정규화(normalize_embeddings=True) 가정. 샘플 0/1개는 단일 클러스터.
    """
    n = len(vectors)
    if n <= 1:
        return [0] * n
    from sklearn.cluster import AgglomerativeClustering

    model = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=distance_threshold,
        metric="cosine",
        linkage="average",
    )
    return model.fit_predict(np.asarray(vectors, dtype=np.float64)).tolist()


def build_keyword_senses(
    week: str,
    keyword: str,
    ctx_items: Sequence[Tuple[Tuple[str, str], int]],
    vectors: np.ndarray | None,
    *,
    distance_threshold: float,
    max_senses: int,
    neighbor_top_k: int,
) -> List[SenseRow]:
    """한 키워드의 맥락들을 군집해 sense 행으로 변환.

    ctx_items: [((before, after), occ_count), ...] — vectors 와 동일 순서.
    vectors:   각 맥락 문장의 임베딩 (len == len(ctx_items)). None 이면 단일 sense.
    """
    if not ctx_items:
        return []

    if vectors is None or len(ctx_items) <= 1:
        labels = [0] * len(ctx_items)
    else:
        labels = cluster_vectors(vectors, distance_threshold=distance_threshold)

    # 클러스터별 멤버 묶기
    groups: Dict[int, List[Tuple[Tuple[str, str], int]]] = {}
    for item, lab in zip(ctx_items, labels):
        groups.setdefault(lab, []).append(item)

    # count(occ 합) 내림차순으로 정렬 → 상위 max_senses 만 채택
    ordered = sorted(
        groups.values(),
        key=lambda items: sum(occ for _, occ in items),
        reverse=True,
    )[:max_senses]

    rows: List[SenseRow] = []
    for sense_id, items in enumerate(ordered):
        total = sum(occ for _, occ in items)
        # 대표 예문 = occ 최다 맥락
        (rep_before, rep_after), _ = max(items, key=lambda kv: kv[1])
        nb = _neighbor_counter(items)
        top_terms = [t for t, _ in nb.most_common(neighbor_top_k)]
        label = top_terms[0] if top_terms else ""
        rows.append(
            (
                week,
                keyword,
                sense_id,
                int(total),
                label,
                rep_before or "",
                rep_after or "",
                ", ".join(top_terms),
                "",  # rep_sentence — 어절 모드는 비움
            )
        )
    return rows


def default_embed_fn(sentences: List[str]) -> np.ndarray:
    """실제 임베딩 — 프로젝트 표준 모델 재사용(맥미니에서 동작)."""
    from steps.qdrant_embed import embed_texts

    return embed_texts(sentences, normalize=True)


def compute_week_senses(
    week: str,
    top_kws: Sequence[str],
    ctx_counter: Dict[str, Counter],
    *,
    embed_fn: EmbedFn,
    distance_threshold: float = 0.35,
    max_senses: int = 6,
    neighbor_top_k: int = 5,
    min_occ_for_split: int = 5,
    min_contexts_for_split: int = 3,
    max_contexts_per_kw: int = 80,
) -> List[SenseRow]:
    """주차 단위 sense 산출.

    1) 분화 후보 게이트: 총 occ >= min_occ_for_split AND unique 맥락수 >= min_contexts_for_split.
       미달 키워드는 임베딩 없이 단일 sense(전체 묶음)로 처리(비용 절약).
    2) 후보 키워드의 unique 맥락 문장을 평탄화 → 한 번에 배치 임베딩(효율).
    3) 키워드별로 다시 묶어 군집 → sense 행.

    max_contexts_per_kw: 키워드별 임베딩할 맥락 상한(occ 내림차순 상위 K). occ=1 롱테일은
    지배적 의미군에 거의 기여하지 않으므로 잘라 임베딩 비용을 크게 줄인다(품질 영향 미미).
    0/None 이면 무제한(설계 원안).
    """
    rows: List[SenseRow] = []

    # 키워드별 맥락 아이템 정리 — occ 상위 max_contexts_per_kw 개만(롱테일 절단)
    cap = max_contexts_per_kw if max_contexts_per_kw and max_contexts_per_kw > 0 else None
    kw_items: Dict[str, List[Tuple[Tuple[str, str], int]]] = {}
    for kw in top_kws:
        counter = ctx_counter.get(kw)
        if not counter:
            continue
        items = counter.most_common(cap) if cap else list(counter.items())
        if items:
            kw_items[kw] = items

    # 분화 후보 / 단일 분리
    split_kws: List[str] = []
    for kw, items in kw_items.items():
        total = sum(occ for _, occ in items)
        if total >= min_occ_for_split and len(items) >= min_contexts_for_split:
            split_kws.append(kw)

    # 단일 sense 키워드(게이트 미달) — 임베딩 불필요
    for kw, items in kw_items.items():
        if kw in split_kws:
            continue
        rows.extend(
            build_keyword_senses(
                week, kw, items, None,
                distance_threshold=distance_threshold,
                max_senses=max_senses,
                neighbor_top_k=neighbor_top_k,
            )
        )

    # 분화 후보 — 평탄화 배치 임베딩
    if split_kws:
        flat_sentences: List[str] = []
        spans: Dict[str, Tuple[int, int]] = {}
        for kw in split_kws:
            items = kw_items[kw]
            start = len(flat_sentences)
            for (before, after), _ in items:
                flat_sentences.append(context_sentence(kw, before, after))
            spans[kw] = (start, len(flat_sentences))

        logger.info(
            "sense 임베딩 | week=%s | 분화후보=%d키워드 | 맥락문장=%d",
            week, len(split_kws), len(flat_sentences),
        )
        vectors = embed_fn(flat_sentences)

        for kw in split_kws:
            s, e = spans[kw]
            rows.extend(
                build_keyword_senses(
                    week, kw, kw_items[kw], vectors[s:e],
                    distance_threshold=distance_threshold,
                    max_senses=max_senses,
                    neighbor_top_k=neighbor_top_k,
                )
            )

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 문장 모드(sentence) — 대안 C: 리드 한정 + 전역 트렌드 군집.
#   ① 수집 단계가 제목+본문 리드 문장만 모음(키워드 추출 단계, weight 부여).
#   ② 그 주의 고유 문장을 "딱 한 번" 임베딩 → "딱 한 번" 전역 군집 = 그 주의 트렌드.
#   ③ 키워드 sense/트렌드 = 키워드가 등장한 문장들의 트렌드 분포(집계만, 추가 비용 0).
#   → 부하가 키워드 수·문장 중복과 무관. 한 문장 다중 키워드 구조적 해결.
#   입력: sent_weight[문장]=가중합,  sent_kws[문장]=Counter{키워드(명사): cnt}
# (week, cluster_id, size, weight, label, rep_sentence, top_keywords, centroid)
# centroid: 군집 중심 벡터(list[float]) — 주 간 트렌드 연결(thread)의 비교 재료.
TrendRow = Tuple[str, int, int, int, str, str, str, List[float]]


def _cluster_sentences_global(
    vectors: np.ndarray,
    *,
    target_size: int,
    kmin: int,
    kmax: int,
) -> Tuple[List[int], np.ndarray]:
    """전역 문장 군집(MiniBatchKMeans). 반환 (labels, centers[k×dim]).

    K = clamp(n/target_size, kmin, kmax). vectors 는 정규화 가정(코사인≈유클리드).
    n 이 작으면 단일 군집(centroid=평균).
    """
    arr = np.asarray(vectors, dtype=np.float32)
    n = len(arr)
    dim = arr.shape[1] if arr.ndim == 2 and n else 0
    if n == 0:
        return [], np.zeros((0, dim), dtype=np.float32)
    if n <= max(2, kmin):
        return [0] * n, arr.mean(axis=0, keepdims=True)
    k = n // max(1, target_size)
    k = max(kmin, min(kmax, k, n))
    from sklearn.cluster import MiniBatchKMeans

    model = MiniBatchKMeans(n_clusters=k, n_init=3, random_state=0)
    labels = model.fit_predict(arr)
    return labels.tolist(), model.cluster_centers_


def compute_week_trend_senses(
    week: str,
    top_kws: Sequence[str],
    sent_weight: Dict[str, float],
    sent_kws: Dict[str, Counter],
    *,
    embed_fn: EmbedFn,
    cluster_target_size: int = 40,
    cluster_kmin: int = 20,
    cluster_kmax: int = 400,
    max_senses: int = 4,
    neighbor_top_k: int = 5,
    min_sense_share: float = 0.12,
    min_sense_weight: float = 1.0,
    logger=logger,
) -> Tuple[List[SenseRow], List[TrendRow]]:
    """대안 C — 전역 문장 군집(트렌드) → 키워드 sense 매핑.

    반환: (sense_rows, trend_rows).
      sense_rows: 키워드별 상위 트렌드 갈래(다의어=여러 군집에 분산).
      trend_rows: 트렌드 군집 자체(weight 내림차순).
    """
    sentences = list(sent_weight.keys())
    n = len(sentences)
    if n == 0:
        return [], []

    logger.info("sense(트렌드) 임베딩 | week=%s | 고유문장=%d", week, n)
    vectors = embed_fn(sentences)
    labels, centers = _cluster_sentences_global(
        vectors, target_size=cluster_target_size, kmin=cluster_kmin, kmax=cluster_kmax
    )

    top_set = set(top_kws)

    # 군집 메타 + 키워드→군집 weight 집계(top_kws 만).
    cluster_items: Dict[int, List[Tuple[str, float]]] = {}
    cluster_kw: Dict[int, Counter] = {}
    kw_cluster_w: Dict[str, Dict[int, float]] = {}
    for sent, lab in zip(sentences, labels):
        w = sent_weight[sent]
        cluster_items.setdefault(lab, []).append((sent, w))
        ck = cluster_kw.setdefault(lab, Counter())
        for kw, cnt in sent_kws.get(sent, {}).items():
            ck[kw] += cnt
            if kw in top_set:
                cw = kw_cluster_w.setdefault(kw, {})
                cw[lab] = cw.get(lab, 0.0) + w

    # 변별력(TF-IDF식): 흔한 키워드(가격·기자)가 라벨을 독식하지 않게, 군집 빈도 × idf 로 점수.
    n_docs = max(1, len(sentences))
    df: Counter = Counter()
    for sent in sentences:
        for k in sent_kws.get(sent, {}):
            df[k] += 1

    def _distinctive(counter: Counter, k: int) -> List[str]:
        scored = sorted(
            counter.items(),
            key=lambda kv: kv[1] * math.log(1.0 + n_docs / max(1, df.get(kv[0], 1))),
            reverse=True,
        )
        return [t for t, _ in scored[:k]]

    cluster_label: Dict[int, str] = {}
    cluster_rep: Dict[int, str] = {}
    cluster_topkw: Dict[int, List[str]] = {}
    for c, items in cluster_items.items():
        rep, _ = max(items, key=lambda sw: sw[1])
        cluster_rep[c] = rep
        topk = _distinctive(cluster_kw[c], neighbor_top_k)
        cluster_topkw[c] = topk
        cluster_label[c] = topk[0] if topk else ""

    # 키워드 sense — 비중 큰 트렌드만(롱테일 군집 제외), 상위 max_senses.
    sense_rows: List[SenseRow] = []
    for kw, cw in kw_cluster_w.items():
        ordered = sorted(cw.items(), key=lambda kv: kv[1], reverse=True)
        total = sum(w for _, w in ordered) or 1.0
        chosen: List[Tuple[int, float]] = []
        for c, w in ordered:
            if not chosen or w >= max(min_sense_weight, total * min_sense_share):
                chosen.append((c, w))
            if len(chosen) >= max_senses:
                break
        for sid, (c, w) in enumerate(chosen):
            sense_rows.append(
                (
                    week, kw, sid, int(round(w)),
                    cluster_label[c], "", "",
                    ", ".join(t for t in cluster_topkw[c] if t != kw),
                    cluster_rep[c],
                )
            )

    # 트렌드 군집 행 — weight 내림차순. centroid(군집 중심)를 함께 저장(주 간 연결 재료).
    n_centers = len(centers)
    trend_rows: List[TrendRow] = []
    for c, items in cluster_items.items():
        size = len(items)
        weight = sum(w for _, w in items)
        centroid = centers[c].tolist() if 0 <= c < n_centers else []
        trend_rows.append(
            (
                week, int(c), size, int(round(weight)),
                cluster_label[c], cluster_rep[c], ", ".join(cluster_topkw[c]),
                centroid,
            )
        )
    trend_rows.sort(key=lambda r: r[3], reverse=True)
    # cluster_id 를 weight 순위로 재부여(0=가장 큰 트렌드) — 표시 안정.
    remap = {r[1]: i for i, r in enumerate(trend_rows)}
    trend_rows = [
        (r[0], remap[r[1]], r[2], r[3], r[4], r[5], r[6], r[7]) for r in trend_rows
    ]
    sense_rows = [
        (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]) for r in sense_rows
    ]

    logger.info(
        "sense(트렌드) 군집 | week=%s | 트렌드=%d | sense행=%d",
        week, len(trend_rows), len(sense_rows),
    )
    return sense_rows, trend_rows

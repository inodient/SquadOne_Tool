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
from collections import Counter
from typing import Callable, Dict, List, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# (week, keyword, sense_id, count, label, rep_before, rep_after, top_neighbors)
SenseRow = Tuple[str, str, int, int, str, str, str, str]

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
) -> List[SenseRow]:
    """주차 단위 sense 산출.

    1) 분화 후보 게이트: 총 occ >= min_occ_for_split AND unique 맥락수 >= min_contexts_for_split.
       미달 키워드는 임베딩 없이 단일 sense(전체 묶음)로 처리(비용 절약).
    2) 후보 키워드의 모든 unique 맥락 문장을 평탄화 → 한 번에 배치 임베딩(효율).
    3) 키워드별로 다시 묶어 군집 → sense 행.
    """
    rows: List[SenseRow] = []

    # 키워드별 맥락 아이템 정리
    kw_items: Dict[str, List[Tuple[Tuple[str, str], int]]] = {}
    for kw in top_kws:
        items = list(ctx_counter.get(kw, Counter()).items())
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

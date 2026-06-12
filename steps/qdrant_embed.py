"""
Qdrant 질의용 임베딩 — `squadone_news_vectordb/scripts/ingest_news_to_qdrant.py` 와 동일 스펙.

- 모델 기본값: sentence-transformers/paraphrase-multilingual-mpnet-base-v2
- encode 시 normalize_embeddings=True (코사인 거리와 인덱싱 시 벡터 정규화와 일치)
"""

from __future__ import annotations

import logging
import os
import time
from functools import lru_cache
from typing import List, Literal

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
ENCODE_BATCH_SIZE = int(os.environ.get("SQUADONE_EMBED_BATCH", "256"))


def _pick_device(device_arg: str) -> str:
    if device_arg != "auto":
        return device_arg
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


@lru_cache(maxsize=4)
def _load_sentence_transformer(model_name: str, device: str):
    from sentence_transformers import SentenceTransformer

    dev = _pick_device(device)
    logger.info("Loading SentenceTransformer model=%s device=%s", model_name, dev)
    return SentenceTransformer(model_name, device=dev)


def get_embed_model(*, model_name: str | None = None, device: str | None = None):
    """
    환경변수 SQUADONE_EMBED_MODEL, SQUADONE_EMBED_DEVICE 가 설정되면 우선합니다.
    """
    name = (model_name or os.environ.get("SQUADONE_EMBED_MODEL") or DEFAULT_MODEL_NAME).strip()
    dev = (device or os.environ.get("SQUADONE_EMBED_DEVICE") or "auto").strip().lower()
    return _load_sentence_transformer(name, dev)


def embed_texts(
    texts: list[str],
    *,
    model_name: str | None = None,
    device: str | None = None,
    normalize: bool = True,
) -> np.ndarray:
    """배치 임베딩. shape (n, dim)."""
    t0 = time.perf_counter()
    model = get_embed_model(model_name=model_name, device=device)
    logger.debug(
        "QDRANT_EMBED_ENCODE_START | batch=%d | normalize=%s",
        len(texts),
        normalize,
    )
    out = model.encode(
        texts,
        batch_size=ENCODE_BATCH_SIZE,
        show_progress_bar=False,
        normalize_embeddings=normalize,
    )
    arr = np.asarray(out, dtype=np.float32)
    logger.debug(
        "QDRANT_EMBED_ENCODE_DONE | batch=%d | dim=%d | sec=%.3f",
        len(texts),
        int(arr.shape[-1]) if arr.size else 0,
        time.perf_counter() - t0,
    )
    return arr


def embed_query_for_news(
    keyword: str,
    *,
    vector_name: Literal["title", "body", "both"] = "body",
    model_name: str | None = None,
    device: str | None = None,
    normalize: bool = True,
) -> dict[str, List[float]]:
    """
    인덱싱 스크립트와 동일한 플레이스홀더 규칙(빈 제목/본문)을 적용한 뒤 벡터를 생성합니다.

    - body: {"body": [...]}
    - title: {"title": [...]}
    - both: {"title": [...], "body": [...]} — 호출부에서 두 번 검색해 점수 병합 가능
    """
    raw = (keyword or "").strip()
    title_text = raw if raw else "(제목 없음)"
    body_text = raw if raw else "(본문 없음)"
    logger.debug(
        "QDRANT_EMBED_QUERY_TEXTS | mode=%s | title_len=%d | body_len=%d",
        vector_name,
        len(title_text),
        len(body_text),
    )

    if vector_name == "title":
        vec = embed_texts([title_text], model_name=model_name, device=device, normalize=normalize)[0]
        return {"title": vec.tolist()}
    if vector_name == "body":
        vec = embed_texts([body_text], model_name=model_name, device=device, normalize=normalize)[0]
        return {"body": vec.tolist()}

    vecs = embed_texts(
        [title_text, body_text],
        model_name=model_name,
        device=device,
        normalize=normalize,
    )
    return {"title": vecs[0].tolist(), "body": vecs[1].tolist()}

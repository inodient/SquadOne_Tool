from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

from steps.common import ensure_output_dir, get_logger, load_config, read_csv, write_csv

try:
    import hdbscan
except ImportError:  # pragma: no cover
    hdbscan = None

try:
    from sklearn.cluster import DBSCAN
    from sklearn.feature_extraction.text import TfidfVectorizer
except ImportError:  # pragma: no cover
    DBSCAN = None
    TfidfVectorizer = None

try:
    from gensim.models import FastText
except ImportError:  # pragma: no cover
    FastText = None


def _keyword_vectors(keywords: List[str]) -> np.ndarray:
    if FastText is not None:
        tokenized = [[k] for k in keywords]
        model = FastText(vector_size=50, window=2, min_count=1)
        model.build_vocab(tokenized)
        model.train(tokenized, total_examples=len(tokenized), epochs=20)
        return np.array([model.wv[k] for k in keywords], dtype=float)

    if TfidfVectorizer is not None:
        tfidf = TfidfVectorizer(analyzer="char", ngram_range=(2, 4))
        return tfidf.fit_transform(keywords).toarray()

    # 최후 폴백: 길이 기반 단순 벡터
    return np.array([[len(k)] for k in keywords], dtype=float)


def _cluster_embeddings(embeddings: np.ndarray, min_cluster_size: int) -> np.ndarray:
    if hdbscan is not None:
        clusterer = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size)
        return clusterer.fit_predict(embeddings)

    if DBSCAN is not None:
        return DBSCAN(eps=0.7, min_samples=min_cluster_size).fit_predict(embeddings)

    return np.zeros(shape=(embeddings.shape[0],), dtype=int)


def _llm_labeling(cluster_keywords: List[str], cluster_id: int) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return f"cluster_{cluster_id}"

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        joined = ", ".join(cluster_keywords[:20])
        prompt = f"다음 키워드 묶음을 대표하는 4~8자 한국어 주제를 1개만 출력하세요: {joined}"
        resp = client.responses.create(model="gpt-4.1-mini", input=prompt)
        return resp.output_text.strip() or f"cluster_{cluster_id}"
    except Exception:
        return f"cluster_{cluster_id}"


def run_clustering(z_score_path: Path, target_week: Optional[str] = None) -> Path:
    logger = get_logger("steps.clustering")
    started = time.perf_counter()
    logger.info("clustering 시작 | 입력=%s | target_week=%s", z_score_path, target_week)

    config = load_config()
    cluster_cfg = config["clustering"]
    top_n = int(cluster_cfg["top_n_keywords"])
    min_cluster_size = int(cluster_cfg["min_cluster_size"])

    output_dir = ensure_output_dir()
    z_df = read_csv(z_score_path)
    if z_df.empty:
        raise ValueError("입력 z_score_keywords.csv가 비어 있습니다.")

    chosen_week = target_week or cluster_cfg["target_week"] or z_df["week"].max()
    weekly = z_df[z_df["week"] == chosen_week].sort_values("z_score", ascending=False).head(top_n)
    if weekly.empty:
        raise ValueError(f"대상 주차({chosen_week}) 데이터가 없습니다.")
    logger.info("클러스터링 대상 주차=%s | 키워드수=%d", chosen_week, len(weekly))

    keywords = weekly["keyword"].astype(str).tolist()
    embeddings = _keyword_vectors(keywords)
    labels = _cluster_embeddings(embeddings, min_cluster_size=min_cluster_size)

    result = weekly.copy()
    result["cluster_id"] = labels

    theme_map = {}
    for cid in sorted(set(labels)):
        cluster_keywords = result[result["cluster_id"] == cid]["keyword"].astype(str).tolist()
        theme_map[cid] = _llm_labeling(cluster_keywords, int(cid))
    result["cluster_theme"] = result["cluster_id"].map(theme_map)

    out_path = output_dir / "clustered_keywords.csv"
    written_path = write_csv(result, out_path)
    elapsed = time.perf_counter() - started
    logger.info(
        "clustering 완료 | cluster수=%d | 출력=%s | %.2fs",
        result["cluster_id"].nunique(),
        written_path,
        elapsed,
    )
    return written_path

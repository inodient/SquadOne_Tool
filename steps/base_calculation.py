from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfTransformer

from steps.common import ensure_output_dir, get_logger, load_config, read_csv, write_csv


def run_base_calculation(frequency_matrix_path: Path) -> Path:
    logger = get_logger("steps.base_calculation")
    started = time.perf_counter()
    logger.info("base_calculation 시작 | 입력=%s", frequency_matrix_path)

    config = load_config()
    tfidf_cfg = config["tfidf"]
    window_cfg = config["window"]

    min_df = int(tfidf_cfg["min_df"])
    max_df = float(tfidf_cfg["max_df"])
    long_term_weeks = int(window_cfg["long_term_weeks"])

    output_dir = ensure_output_dir()
    matrix_df = read_csv(frequency_matrix_path)
    if matrix_df.empty:
        raise ValueError("입력 frequency_matrix.csv가 비어 있습니다.")

    week_cols = [c for c in matrix_df.columns if c != "keyword"]
    counts = matrix_df[week_cols].copy()
    doc_freq = (counts > 0).sum(axis=1)
    num_docs = len(week_cols)

    lower_filtered = doc_freq >= min_df
    upper_filtered = doc_freq <= int(max_df * num_docs)
    filtered_df = matrix_df[lower_filtered & upper_filtered].copy()
    if filtered_df.empty:
        raise ValueError("TF-IDF 상하한 필터 적용 결과가 비어 있습니다.")
    logger.info("TF-IDF 필터 통과 키워드 수: %d", len(filtered_df))

    filtered_counts = filtered_df[week_cols].astype(float)
    transformer = TfidfTransformer(norm=None, smooth_idf=True, sublinear_tf=False)
    tfidf_values = transformer.fit_transform(filtered_counts.values).toarray()
    tfidf_df = pd.DataFrame(tfidf_values, columns=week_cols, index=filtered_df.index)

    long_mean = tfidf_df.T.rolling(window=long_term_weeks, min_periods=1).mean().T
    long_std = tfidf_df.T.rolling(window=long_term_weeks, min_periods=1).std(ddof=0).T
    long_std = long_std.replace(0, np.nan)

    out_df = pd.concat(
        [
            filtered_df[["keyword"]].reset_index(drop=True),
            tfidf_df.reset_index(drop=True).add_prefix("tfidf_"),
            long_mean.reset_index(drop=True).add_prefix("mean_"),
            long_std.reset_index(drop=True).add_prefix("std_"),
        ],
        axis=1,
    )

    out_path = output_dir / "base_calculation.csv"
    written_path = write_csv(out_df, out_path)
    elapsed = time.perf_counter() - started
    logger.info(
        "base_calculation 완료 | 행수=%d | 컬럼수=%d | 출력=%s | %.2fs",
        len(out_df),
        len(out_df.columns),
        written_path,
        elapsed,
    )
    return written_path

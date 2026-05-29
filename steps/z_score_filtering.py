from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from steps.common import ensure_output_dir, get_logger, load_config, read_csv, write_csv


def run_z_score_filtering(base_calculation_path: Path, weekly_keywords_path: Path) -> Path:
    logger = get_logger("steps.z_score_filtering")
    started = time.perf_counter()
    logger.info(
        "z_score_filtering 시작 | 입력(base)=%s | 입력(weekly)=%s",
        base_calculation_path,
        weekly_keywords_path,
    )

    config = load_config()
    short_window = int(config["window"]["short_term_weeks"])
    ma_span = int(config["z_score"]["moving_average_span"])

    output_dir = ensure_output_dir()
    base_df = read_csv(base_calculation_path)
    source_df = read_csv(weekly_keywords_path)
    if base_df.empty:
        raise ValueError("입력 base_calculation.csv가 비어 있습니다.")

    tfidf_cols = sorted([c for c in base_df.columns if c.startswith("tfidf_")])
    mean_cols = sorted([c for c in base_df.columns if c.startswith("mean_")])
    std_cols = sorted([c for c in base_df.columns if c.startswith("std_")])

    weeks = [c.replace("tfidf_", "") for c in tfidf_cols]
    rows = []

    for _, row in base_df.iterrows():
        keyword = row["keyword"]
        tfidf_series = pd.Series([row[c] for c in tfidf_cols], index=weeks, dtype=float)
        mean_series = pd.Series([row[c] for c in mean_cols], index=weeks, dtype=float)
        std_series = pd.Series([row[c] for c in std_cols], index=weeks, dtype=float)

        # 단기 윈도우(기본 1주) 평균 후 이동평균으로 노이즈 완화
        short_term = tfidf_series.rolling(window=short_window, min_periods=1).mean()
        short_term = short_term.ewm(span=ma_span, adjust=False).mean()

        z_scores = (short_term - mean_series) / std_series.replace(0, np.nan)
        z_scores = z_scores.replace([np.inf, -np.inf], np.nan).fillna(0.0)

        for week, z in z_scores.items():
            rows.append({"week": week, "keyword": keyword, "z_score": float(z)})

    z_df = pd.DataFrame(rows)
    source_map = (
        source_df.groupby(["week", "keyword"], as_index=False)["source"]
        .agg(lambda x: "|".join(sorted(set(x.astype(str)))))
        .rename(columns={"source": "sources"})
    )
    merged = z_df.merge(source_map, on=["week", "keyword"], how="left")
    merged["sources"] = merged["sources"].fillna("unknown")
    merged = merged.sort_values(["week", "z_score"], ascending=[True, False])

    out_path = output_dir / "z_score_keywords.csv"
    written_path = write_csv(merged, out_path)
    elapsed = time.perf_counter() - started
    logger.info(
        "z_score_filtering 완료 | 행수=%d | 주차수=%d | 출력=%s | %.2fs",
        len(merged),
        merged["week"].nunique(),
        written_path,
        elapsed,
    )
    return written_path

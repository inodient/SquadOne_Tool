from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from steps.common import ensure_output_dir, get_logger, read_csv, write_csv


def run_frequency_matrix(weekly_keywords_path: Path) -> Path:
    logger = get_logger("steps.frequency_matrix")
    started = time.perf_counter()
    logger.info("frequency_matrix 시작 | 입력=%s", weekly_keywords_path)

    output_dir = ensure_output_dir()
    df = read_csv(weekly_keywords_path)
    if df.empty:
        raise ValueError("입력 weekly_keywords.csv가 비어 있습니다.")

    matrix = (
        df.groupby(["keyword", "week"], as_index=False)["count"]
        .sum()
        .pivot(index="keyword", columns="week", values="count")
        .fillna(0)
        .astype(int)
        .reset_index()
    )
    matrix.columns.name = None

    week_columns = sorted([col for col in matrix.columns if col != "keyword"])
    matrix = matrix[["keyword", *week_columns]]

    out_path = output_dir / "frequency_matrix.csv"
    written_path = write_csv(matrix, out_path)
    elapsed = time.perf_counter() - started
    logger.info(
        "frequency_matrix 완료 | 키워드수=%d | 주차수=%d | 출력=%s | %.2fs",
        len(matrix),
        len(week_columns),
        written_path,
        elapsed,
    )
    return written_path

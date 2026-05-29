from __future__ import annotations

from pathlib import Path
from typing import Dict

import pandas as pd

from steps.common import (
    ensure_output_dir,
    get_logger,
    log_artifact,
    log_step,
    read_csv,
    write_csv,
    write_dataframe_json_export,
)


def run_frequency_matrix(weekly_keywords_path: Path) -> Dict[str, Path]:
    logger = get_logger("steps.frequency_matrix")
    with log_step(logger, 2, "frequency_matrix", input=str(weekly_keywords_path.resolve())):
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
        json_path = write_dataframe_json_export(
            matrix,
            written_path,
            step="frequency_matrix",
            extra_meta={"week_columns": week_columns[:50], "week_column_count": len(week_columns)},
        )
        logger.info(
            "frequency_matrix 요약 | 키워드수=%d | 주차수=%d | csv=%s | json=%s",
            len(matrix),
            len(week_columns),
            written_path,
            json_path,
        )
        log_artifact(logger, "OUTPUT_CSV", written_path)
        log_artifact(logger, "OUTPUT_JSON", json_path)
        meta_path = written_path.with_suffix(".meta.json")
        if meta_path.exists():
            log_artifact(logger, "OUTPUT_JSON_META", meta_path)
        return {"csv": written_path, "json": json_path}

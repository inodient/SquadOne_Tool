from __future__ import annotations

from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfTransformer

from steps.common import (
    ensure_output_dir,
    get_logger,
    load_config,
    log_artifact,
    log_step,
    read_csv,
    write_csv,
    write_dataframe_json_export,
)


def run_base_calculation(frequency_matrix_path: Path) -> Dict[str, Path]:
    logger = get_logger("steps.base_calculation")
    with log_step(logger, 3, "base_calculation", input=str(frequency_matrix_path.resolve())):
        config = load_config()
        tfidf_cfg = config["tfidf"]
        window_cfg = config["window"]

        min_df_cfg = int(tfidf_cfg["min_df"])
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

        # 짧은 기간(예: 1~2주) 테스트에서도 필터 결과가 비지 않도록 최소 문서수 기준을 자동 보정한다.
        min_df_effective = max(1, min(min_df_cfg, num_docs))
        upper_bound = max(1, int(max_df * num_docs))
        if upper_bound < min_df_effective:
            upper_bound = min_df_effective
        logger.info(
            "TF-IDF 필터 기준 | min_df_cfg=%d | min_df_effective=%d | max_df=%.3f | upper_bound=%d | num_weeks=%d",
            min_df_cfg,
            min_df_effective,
            max_df,
            upper_bound,
            num_docs,
        )

        lower_filtered = doc_freq >= min_df_effective
        upper_filtered = doc_freq <= upper_bound
        filtered_df = matrix_df[lower_filtered & upper_filtered].copy()
        if filtered_df.empty:
            # 극단 케이스에서 상한 필터만 완화해서 최소 실행 가능성을 보장한다.
            filtered_df = matrix_df[lower_filtered].copy()
            if filtered_df.empty:
                raise ValueError(
                    "TF-IDF 상하한 필터 적용 결과가 비어 있습니다. "
                    f"(min_df_cfg={min_df_cfg}, min_df_effective={min_df_effective}, "
                    f"max_df={max_df}, upper_bound={upper_bound}, num_weeks={num_docs})"
                )
            logger.warning(
                "상한 필터 완화 적용 | lower_only_rows=%d | min_df_effective=%d | upper_bound=%d",
                len(filtered_df),
                min_df_effective,
                upper_bound,
            )
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
        json_path = write_dataframe_json_export(
            out_df,
            written_path,
            step="base_calculation",
            extra_meta={
                "min_df": min_df_cfg,
                "min_df_effective": min_df_effective,
                "max_df": max_df,
                "upper_bound_effective": upper_bound,
                "long_term_weeks": long_term_weeks,
                "num_weeks": num_docs,
            },
        )
        logger.info(
            "base_calculation 요약 | 행수=%d | 컬럼수=%d | csv=%s | json=%s",
            len(out_df),
            len(out_df.columns),
            written_path,
            json_path,
        )
        log_artifact(logger, "OUTPUT_CSV", written_path)
        log_artifact(logger, "OUTPUT_JSON", json_path)
        meta_path = written_path.with_suffix(".meta.json")
        if meta_path.exists():
            log_artifact(logger, "OUTPUT_JSON_META", meta_path)
        return {"csv": written_path, "json": json_path}

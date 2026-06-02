from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfTransformer

from steps.common import (
    ensure_output_dir,
    get_logger,
    load_config,
    log_artifact,
    log_step,
    write_csv,
    write_dataframe_json_export,
)

from db import repository as repo


def run_base_calculation(frequency_matrix_path: Optional[Path] = None) -> Dict[str, object]:
    """3단계: TF-IDF + 장기 윈도우 rolling mean/std (전체 재계산).

    P1 변경: 입력은 DB weekly_keyword_freq(메모리 wide 피벗), 출력은
    base_calculation(long) DB 적재 + 병행 wide CSV. frequency_matrix_path 는 호환용(무시).
    """
    logger = get_logger("steps.base_calculation")
    with log_step(logger, 3, "base_calculation", input="newstrend.weekly_keyword_freq"):
        config = load_config()
        tfidf_cfg = config["tfidf"]
        window_cfg = config["window"]

        min_df_cfg = int(tfidf_cfg["min_df"])
        max_df = float(tfidf_cfg["max_df"])
        long_term_weeks = int(window_cfg["long_term_weeks"])

        output_dir = ensure_output_dir()

        # 입력: DB long → wide 피벗(메모리)
        long_in = repo.read_weekly_freq()
        if long_in.empty:
            raise ValueError("weekly_keyword_freq 가 비어 있습니다.")
        matrix_df = (
            long_in.pivot_table(index="keyword", columns="week", values="count", fill_value=0)
            .astype(int)
            .reset_index()
        )
        matrix_df.columns.name = None
        week_cols = sorted([c for c in matrix_df.columns if c != "keyword"])
        matrix_df = matrix_df[["keyword", *week_cols]]

        counts = matrix_df[week_cols]
        doc_freq = (counts > 0).sum(axis=1)
        num_docs = len(week_cols)

        # 짧은 기간 테스트에서도 필터 결과가 비지 않도록 최소 문서수 기준을 자동 보정한다.
        min_df_effective = max(1, min(min_df_cfg, num_docs))
        upper_bound = max(1, int(max_df * num_docs))
        if upper_bound < min_df_effective:
            upper_bound = min_df_effective
        logger.info(
            "TF-IDF 필터 기준 | min_df_cfg=%d | min_df_effective=%d | max_df=%.3f | upper_bound=%d | num_weeks=%d",
            min_df_cfg, min_df_effective, max_df, upper_bound, num_docs,
        )

        lower_filtered = doc_freq >= min_df_effective
        upper_filtered = doc_freq <= upper_bound
        filtered_df = matrix_df[lower_filtered & upper_filtered].copy()
        if filtered_df.empty:
            filtered_df = matrix_df[lower_filtered].copy()
            if filtered_df.empty:
                raise ValueError(
                    "TF-IDF 상하한 필터 적용 결과가 비어 있습니다. "
                    f"(min_df_cfg={min_df_cfg}, min_df_effective={min_df_effective}, "
                    f"max_df={max_df}, upper_bound={upper_bound}, num_weeks={num_docs})"
                )
            logger.warning("상한 필터 완화 적용 | rows=%d", len(filtered_df))
        logger.info("TF-IDF 필터 통과 키워드 수: %d", len(filtered_df))

        filtered_counts = filtered_df[week_cols].astype(float)
        transformer = TfidfTransformer(norm=None, smooth_idf=True, sublinear_tf=False)
        tfidf_values = transformer.fit_transform(filtered_counts.values).toarray()
        tfidf_df = pd.DataFrame(tfidf_values, columns=week_cols, index=filtered_df.index)

        long_mean = tfidf_df.T.rolling(window=long_term_weeks, min_periods=1).mean().T
        long_std = tfidf_df.T.rolling(window=long_term_weeks, min_periods=1).std(ddof=0).T
        long_std = long_std.replace(0, np.nan)

        # ── 병행 wide CSV (기존 산출물 호환) ──
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
            out_df, written_path, step="base_calculation",
            extra_meta={"min_df": min_df_cfg, "min_df_effective": min_df_effective,
                        "max_df": max_df, "upper_bound_effective": upper_bound,
                        "long_term_weeks": long_term_weeks, "num_weeks": num_docs},
        )

        # ── DB long 적재 (tfidf != 0 희소화, numpy 벡터화) ──
        weeks_arr = np.asarray(week_cols, dtype=object)
        kw_arr = filtered_df["keyword"].to_numpy()
        tf_vals = tfidf_df.to_numpy()
        mn_vals = long_mean.to_numpy()
        sd_vals = long_std.to_numpy()
        mask = tf_vals != 0
        ri, ci = np.where(mask)
        long_out = pd.DataFrame({
            "week": weeks_arr[ci],
            "keyword": kw_arr[ri],
            "tfidf": tf_vals[mask],
            "base_mean": mn_vals[mask],
            "base_std": sd_vals[mask],
        })
        n_db = repo.write_base(long_out)
        logger.info(
            "base_calculation 적재 | wide행=%d | long(DB)행=%d | csv=%s",
            len(out_df), n_db, written_path,
        )
        log_artifact(logger, "OUTPUT_CSV", written_path)
        log_artifact(logger, "OUTPUT_JSON", json_path)
        meta_path = written_path.with_suffix(".meta.json")
        if meta_path.exists():
            log_artifact(logger, "OUTPUT_JSON_META", meta_path)
        return {"csv": written_path, "json": json_path, "db_rows": n_db}

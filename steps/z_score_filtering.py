from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

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

from db import repository as repo


def _load_base_wide_from_db() -> pd.DataFrame:
    """DB base_calculation(long, dense)을 3단계 산출과 동일한 wide 프레임으로 복원.

    컬럼: keyword, tfidf_<week>, mean_<week>, std_<week>. base_calculation 이 dense 로
    적재되므로(tfidf=0 셀의 mean/std 포함) in-memory frame 과 수치적으로 동일하다.
    """
    long_in = repo.read_base()
    if long_in.empty:
        return long_in
    # dropna=False: std 가 전부 NaN 인 주차(예: 단일 주차의 첫 주)라도 컬럼을 보존해야
    # tfidf_/mean_/std_ 의 주차 집합이 일치한다(아래 EWM 루프가 동일 길이를 가정).
    tfidf_w = long_in.pivot_table(index="keyword", columns="week", values="tfidf", dropna=False).add_prefix("tfidf_")
    mean_w = long_in.pivot_table(index="keyword", columns="week", values="base_mean", dropna=False).add_prefix("mean_")
    std_w = long_in.pivot_table(index="keyword", columns="week", values="base_std", dropna=False).add_prefix("std_")
    wide = pd.concat([tfidf_w, mean_w, std_w], axis=1).reset_index()
    wide.columns.name = None
    return wide


def run_z_score_filtering(
    base_calculation_path: Optional[Path] = None,
    weekly_keywords_bundle: Optional[Dict[str, Path]] = None,
    *,
    base_df: Optional[pd.DataFrame] = None,
) -> Dict[str, Path]:
    """4단계: EWM z-score.

    base 입력 우선순위(기본은 DB):
      1) base_df 인자(in-memory dense wide 프레임) — 호환용
      2) base_calculation_path CSV(단독 실행/REST 호환)
      3) DB base_calculation(dense long → wide 복원) — 기본
    sources(언론사)는 항상 DB weekly_keywords 집계를 사용한다.
    """
    logger = get_logger("steps.z_score_filtering")
    base_src = "base_df(in-memory)" if base_df is not None else (
        str(base_calculation_path.resolve()) if base_calculation_path else "newstrend.base_calculation(DB)"
    )
    with log_step(logger, 4, "z_score_filtering", base_input=base_src):
        config = load_config()
        short_window = int(config["window"]["short_term_weeks"])
        ma_span = int(config["z_score"]["moving_average_span"])
        high_threshold = float(config["z_score"].get("high_z_score_threshold", 2.0))

        output_dir = ensure_output_dir()
        # 입력: dense wide base. 기본은 DB base_calculation(dense long → wide 복원),
        # base_df/CSV 는 호환용 폴백.
        if base_df is None:
            if base_calculation_path is not None:
                base_df = read_csv(base_calculation_path)
            else:
                base_df = _load_base_wide_from_db()
        # sources: DB weekly_keywords 전체에서 (week,keyword)별 집계(증분 CSV는 부분이므로 DB 사용)
        source_map = repo.read_weekly_sources()
        if base_df.empty:
            raise ValueError("입력 base_calculation 이 비어 있습니다. (3단계를 먼저 실행하세요)")

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
        merged = z_df.merge(source_map, on=["week", "keyword"], how="left")
        merged["sources"] = merged["sources"].fillna("unknown")
        merged = merged.sort_values(["week", "z_score"], ascending=[True, False])

        out_path = output_dir / "z_score_keywords.csv"
        written_path = write_csv(merged, out_path)
        json_path = write_dataframe_json_export(
            merged,
            written_path,
            step="z_score_filtering",
            extra_meta={"short_term_weeks": short_window, "moving_average_span": ma_span},
        )
        logger.info(
            "z_score_filtering 요약 | 행수=%d | 주차수=%d | csv=%s | json=%s",
            len(merged),
            merged["week"].nunique(),
            written_path,
            json_path,
        )
        log_artifact(logger, "OUTPUT_CSV", written_path)
        log_artifact(logger, "OUTPUT_JSON", json_path)
        meta_path = written_path.with_suffix(".meta.json")
        if meta_path.exists():
            log_artifact(logger, "OUTPUT_JSON_META", meta_path)

        # DB 적재(전체 재계산 → TRUNCATE 후 COPY). 고z는 뷰 v_z_score_high 로 제공.
        n_db = repo.write_zscore(merged[["week", "keyword", "z_score", "sources"]])
        logger.info("DB 적재 z_score_keywords | %d행", n_db)

        # 추가 산출물: z-score 임계치 이상 키워드만 별도 저장
        high_df = merged[merged["z_score"] >= high_threshold].copy()
        high_out_name = f"z_score_keywords_{high_threshold:.1f}.csv"
        high_out_path = output_dir / high_out_name
        high_written_path = write_csv(high_df, high_out_path)
        high_json_path = write_dataframe_json_export(
            high_df,
            high_written_path,
            step="z_score_filtering_high",
            extra_meta={
                "short_term_weeks": short_window,
                "moving_average_span": ma_span,
                "high_z_score_threshold": high_threshold,
            },
        )
        logger.info(
            "z_score_high 요약 | threshold=%.1f | 행수=%d | 주차수=%d | csv=%s | json=%s",
            high_threshold,
            len(high_df),
            high_df["week"].nunique() if not high_df.empty else 0,
            high_written_path,
            high_json_path,
        )
        log_artifact(logger, "OUTPUT_HIGH_CSV", high_written_path)
        log_artifact(logger, "OUTPUT_HIGH_JSON", high_json_path)
        high_meta_path = high_written_path.with_suffix(".meta.json")
        if high_meta_path.exists():
            log_artifact(logger, "OUTPUT_HIGH_JSON_META", high_meta_path)

        # NOTE: P1에서 _group 산출물(z_score_keywords_group)은 폐기됨(소비처 없음).
        return {
            "csv": written_path,
            "json": json_path,
            "high_csv": high_written_path,
            "high_json": high_json_path,
            "frame": merged,        # 5+6단계 trend in-memory 입력
            "high_frame": high_df,
        }

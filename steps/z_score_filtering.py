from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict

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


def _normalize_keyword_for_grouping(keyword: str) -> str:
    text = str(keyword).strip().lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9a-z가-힣]", "", text)
    return text


def _keyword_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        shorter = min(len(a), len(b))
        longer = max(len(a), len(b))
        if longer > 0:
            return shorter / longer
    return SequenceMatcher(None, a, b).ratio()


def _build_keyword_group_map(
    keywords: pd.Series,
    similarity_threshold: float,
) -> Dict[str, str]:
    """
    유사 키워드를 대표 키워드로 묶기 위한 매핑 생성.
    대표 키워드는 우선 등장(빈도 높은) 키워드를 사용.
    """
    counts = keywords.astype(str).value_counts()
    originals = counts.index.tolist()
    normalized = {k: _normalize_keyword_for_grouping(k) for k in originals}

    representatives: list[tuple[str, str]] = []  # (rep_keyword, rep_normalized)
    group_map: Dict[str, str] = {}

    for keyword in originals:
        norm = normalized[keyword]
        assigned = None
        for rep_keyword, rep_norm in representatives:
            if _keyword_similarity(norm, rep_norm) >= similarity_threshold:
                assigned = rep_keyword
                break
        if assigned is None:
            representatives.append((keyword, norm))
            assigned = keyword
        group_map[keyword] = assigned
    return group_map


def run_z_score_filtering(base_calculation_path: Path, weekly_keywords_bundle: Dict[str, Path]) -> Dict[str, Path]:
    logger = get_logger("steps.z_score_filtering")
    with log_step(
        logger,
        4,
        "z_score_filtering",
        base_input=str(base_calculation_path.resolve()),
        weekly_input=str(weekly_keywords_bundle["csv"].resolve()),
    ):
        config = load_config()
        short_window = int(config["window"]["short_term_weeks"])
        ma_span = int(config["z_score"]["moving_average_span"])
        high_threshold = float(config["z_score"].get("high_z_score_threshold", 2.0))
        group_similarity_threshold = float(config["z_score"].get("group_similarity_threshold", 0.82))

        output_dir = ensure_output_dir()
        base_df = read_csv(base_calculation_path)
        source_df = read_csv(weekly_keywords_bundle["csv"])
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

        # 추가 산출물: z_score >= threshold 데이터에서 유사 키워드 그룹 시계열 집계
        if high_df.empty:
            grouped_df = pd.DataFrame(columns=["week", "keyword", "z_score", "sources"])
        else:
            group_map = _build_keyword_group_map(
                high_df["keyword"],
                similarity_threshold=group_similarity_threshold,
            )
            grouped_df = high_df.copy()
            grouped_df["keyword"] = grouped_df["keyword"].map(group_map).fillna(grouped_df["keyword"])
            grouped_df = (
                grouped_df.groupby(["keyword", "week"], as_index=False)
                .agg(
                    z_score=("z_score", "mean"),
                    sources=("sources", lambda x: "|".join(sorted(set(x.astype(str))))),
                )
                .sort_values(["keyword", "week"], ascending=[True, True])
                .reset_index(drop=True)
            )
            grouped_df = grouped_df[["week", "keyword", "z_score", "sources"]]

        grouped_path = output_dir / "z_score_keywords_group.csv"
        grouped_written_path = write_csv(grouped_df, grouped_path)
        grouped_json_path = write_dataframe_json_export(
            grouped_df,
            grouped_written_path,
            step="z_score_filtering_group",
            extra_meta={
                "source_file": str(high_written_path),
                "high_z_score_threshold": high_threshold,
                "group_similarity_threshold": group_similarity_threshold,
            },
        )
        logger.info(
            "z_score_group 요약 | threshold=%.1f | similarity=%.2f | 행수=%d | csv=%s | json=%s",
            high_threshold,
            group_similarity_threshold,
            len(grouped_df),
            grouped_written_path,
            grouped_json_path,
        )
        log_artifact(logger, "OUTPUT_GROUP_CSV", grouped_written_path)
        log_artifact(logger, "OUTPUT_GROUP_JSON", grouped_json_path)
        grouped_meta_path = grouped_written_path.with_suffix(".meta.json")
        if grouped_meta_path.exists():
            log_artifact(logger, "OUTPUT_GROUP_JSON_META", grouped_meta_path)

        return {
            "csv": written_path,
            "json": json_path,
            "high_csv": high_written_path,
            "high_json": high_json_path,
            "group_csv": grouped_written_path,
            "group_json": grouped_json_path,
        }

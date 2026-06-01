from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import pandas as pd

from steps.common import get_logger, log_step

from db import repository as repo


def run_frequency_matrix(weekly_keywords_path: Path) -> Dict[str, object]:
    """2단계: 주차×키워드 빈도 집계 (SQL 증분).

    P1 변경: dense 피벗 CSV 생성을 폐기하고, 처리된 주차에 대해서만
    DB의 weekly_keyword_freq 를 재계산(DELETE+INSERT…SELECT)한다.
    처리 주차는 step1 산출(weekly_keywords.csv)의 week 컬럼에서 가져온다.
    """
    logger = get_logger("steps.frequency_matrix")
    with log_step(logger, 2, "frequency_matrix", input=str(Path(weekly_keywords_path).resolve())):
        # 처리 대상 주차만 읽음(week 컬럼만 → 경량)
        weeks_df = pd.read_csv(weekly_keywords_path, encoding="utf-8-sig", usecols=["week"])
        weeks: List[str] = sorted(weeks_df["week"].astype(str).unique().tolist())
        if not weeks:
            raise ValueError("frequency_matrix: 입력 weekly_keywords 에 week 가 없습니다.")

        n = repo.recompute_weekly_keyword_freq(weeks)
        total = repo.table_count("weekly_keyword_freq")
        logger.info(
            "frequency_matrix(증분) | 재계산 주차=%d | weekly_keyword_freq 총 행수=%d | weeks=%s..%s",
            n, total, weeks[0], weeks[-1],
        )
        return {"weeks": weeks, "recomputed_weeks": n, "freq_table": "newstrend.weekly_keyword_freq"}

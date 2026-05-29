from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Dict, List

import pandas as pd

from steps.common import ensure_output_dir, get_logger, load_config, resolve_path, write_csv

try:
    from kiwipiepy import Kiwi
except ImportError:  # pragma: no cover - optional dependency
    Kiwi = None


def _load_stopwords(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def _pick_column(columns: List[str], candidates: List[str]) -> str | None:
    lowered = {c.lower(): c for c in columns}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None


def _iso_week_label(date_series: pd.Series) -> pd.Series:
    dt = pd.to_datetime(date_series, errors="coerce")
    iso = dt.dt.isocalendar()
    return iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)


def _extract_nouns(text: str, kiwi: Kiwi | None, stopwords: set[str]) -> List[str]:
    text = re.sub(r"\s+", " ", str(text)).strip()
    if not text:
        return []

    if kiwi is not None:
        tokens = []
        for token in kiwi.tokenize(text):
            if token.tag.startswith("N"):  # 체언 계열
                word = token.form.strip()
                if len(word) >= 2 and word not in stopwords:
                    tokens.append(word)
        return tokens

    # Kiwi가 없는 경우 간단한 한글 명사 유사 토큰 추출
    rough_tokens = re.findall(r"[가-힣]{2,}", text)
    return [tok for tok in rough_tokens if tok not in stopwords]


def run_keyword_extractor() -> Path:
    logger = get_logger("steps.keyword_extractor")
    started = time.perf_counter()
    logger.info("keyword_extractor 시작")

    config = load_config()
    paths = config["paths"]
    news_cfg = config["news"]

    news_dir = resolve_path(paths["news_dir"])
    output_dir = ensure_output_dir()
    stopwords = _load_stopwords(resolve_path(paths["stopwords_path"]))
    kiwi = Kiwi() if Kiwi is not None else None

    files = sorted(news_dir.glob(news_cfg["excel_pattern"]))
    if not files:
        raise FileNotFoundError(f"뉴스 파일을 찾을 수 없습니다: {news_dir}")
    logger.info("입력 파일 수: %d", len(files))

    weekly_rows: List[Dict[str, str | int]] = []
    processed_files = 0

    for excel_path in files:
        logger.info("파일 처리 중: %s", excel_path.name)
        df = pd.read_excel(excel_path)
        if df.empty:
            logger.warning("빈 파일 건너뜀: %s", excel_path.name)
            continue

        text_col = _pick_column(df.columns.tolist(), news_cfg["candidate_text_columns"])
        source_col = _pick_column(df.columns.tolist(), news_cfg["source_columns"])
        date_col = _pick_column(
            df.columns.tolist(), ["date", "날짜", "일자", "published_at", "등록일", "작성일"]
        )

        if text_col is None:
            logger.warning("본문 컬럼 미탐지로 건너뜀: %s", excel_path.name)
            continue
        if date_col is None:
            # 파일명에서 기간이 제공되어도 주차 집계를 위해 최소한의 날짜열이 필요함
            logger.warning("날짜 컬럼 미탐지로 건너뜀: %s", excel_path.name)
            continue

        tmp = df[[c for c in [date_col, text_col, source_col] if c is not None]].copy()
        tmp["week"] = _iso_week_label(tmp[date_col])
        tmp = tmp.dropna(subset=["week", text_col])

        for _, row in tmp.iterrows():
            nouns = _extract_nouns(row[text_col], kiwi, stopwords)
            source_val = row[source_col] if source_col else "unknown"
            for keyword in nouns:
                weekly_rows.append(
                    {
                        "week": row["week"],
                        "keyword": keyword,
                        "source": str(source_val),
                        "count": 1,
                    }
                )
        processed_files += 1

    result = pd.DataFrame(weekly_rows)
    if result.empty:
        raise ValueError("키워드 추출 결과가 비어 있습니다. 입력 데이터와 컬럼명을 확인해주세요.")

    result = (
        result.groupby(["week", "keyword", "source"], as_index=False)["count"]
        .sum()
        .sort_values(["week", "count"], ascending=[True, False])
    )

    out_path = output_dir / "weekly_keywords.csv"
    written_path = write_csv(result, out_path)
    elapsed = time.perf_counter() - started
    logger.info(
        "keyword_extractor 완료 | 처리파일=%d | 행수=%d | 출력=%s | %.2fs",
        processed_files,
        len(result),
        written_path,
        elapsed,
    )
    return written_path

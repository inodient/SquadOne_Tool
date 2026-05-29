from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from steps.common import (
    ensure_output_dir,
    get_logger,
    load_config,
    log_artifact,
    log_step,
    resolve_path,
    write_csv,
    write_dataframe_json_export,
)

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


def _coerce_news_date_series(raw: pd.Series) -> pd.Series:
    """
    뉴스 데이터의 `일자`는 종종 YYYYMMDD 형태의 정수(int/float)로 들어온다.
    이 상태에서 pd.to_datetime을 포맷 없이 호출하면 epoch ns로 오인되어 1970년대 날짜가 된다.
    """
    s = raw
    # 숫자형(정수/실수) 우선: 8자리 YYYYMMDD로 해석
    if pd.api.types.is_numeric_dtype(s):
        s_int = pd.to_numeric(s, errors="coerce").round(0).astype("Int64")
        dt = pd.to_datetime(s_int.astype(str), format="%Y%m%d", errors="coerce")
        return dt

    # 문자열: 숫자 8자리면 YYYYMMDD로, 아니면 일반 파싱
    s_str = s.astype(str).str.strip()
    mask = s_str.str.fullmatch(r"\d{8}")
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    if mask.any():
        out.loc[mask] = pd.to_datetime(s_str.loc[mask], format="%Y%m%d", errors="coerce")
    if (~mask).any():
        out.loc[~mask] = pd.to_datetime(s_str.loc[~mask], errors="coerce")
    return out


def _extract_nouns(text: str, kiwi: Kiwi | None, stopwords: set[str]) -> List[str]:
    text = re.sub(r"\s+", " ", str(text)).strip()
    if not text:
        return []

    if kiwi is not None:
        def _is_person_entity(token_obj: object) -> bool:
            ner = getattr(token_obj, "ner", None)
            if not ner:
                return False
            try:
                # 일반적으로 ("PS", score) 혹은 [("PS", score), ...] 형태
                if isinstance(ner, (list, tuple)):
                    first = ner[0]
                    if isinstance(first, (list, tuple)) and first:
                        return str(first[0]) == "PS"
                    return str(first) == "PS"
            except Exception:
                return False
            return False

        tokens: List[str] = []
        # 조사(J*)는 기본 제거
        for token in kiwi.tokenize(text):
            # 구버전/신버전 호환을 위해 조사(J*)는 후처리로 제외한다.
            if token.tag.startswith("J"):
                continue
            # 인명 개체(PS) 제외
            if _is_person_entity(token):
                continue

            # NNG: 일반명사 / VA: 형용사(기본형 처리)
            if token.tag == "NNG":
                word = token.form.strip()
            elif token.tag == "VA":
                base = token.form.strip()
                word = base if base.endswith("다") else f"{base}다"
            else:
                continue

            if len(word) >= 2 and word not in stopwords:
                tokens.append(word)
        return tokens

    # Kiwi가 없는 경우 간단한 한글 명사 유사 토큰 추출
    # NOTE: 문자 클래스 `[가-힣]`는 모든 현대 한글 음절을 커버하지 못할 수 있어 유니코드 범위를 사용한다.
    rough_tokens = re.findall(r"[\uac00-\ud7a3]{2,}", text)
    return [tok for tok in rough_tokens if tok not in stopwords]


def _extract_feature_keywords(feature_text: str, stopwords: set[str]) -> List[str]:
    """
    `특성추출(가중치순 상위 50개)` 컬럼 파싱 전용.
    예: "반도체(0.51), 수출(0.32)" -> ["반도체", "수출"]
    """
    text = str(feature_text).strip()
    if not text or text.lower() == "nan":
        return []
    parts = re.split(r"[,\|;/\n]+", text)
    tokens: List[str] = []
    for part in parts:
        token = part.strip()
        if not token:
            continue
        token = re.sub(r"\([^)]*\)", "", token).strip()
        token = re.sub(r"\[[^\]]*\]", "", token).strip()
        token = re.sub(r"^\d+[\.\)]\s*", "", token).strip()
        token = re.sub(r"\s+", " ", token).strip()
        if len(token) >= 2 and token not in stopwords:
            tokens.append(token)
    return tokens


def _parse_iso_date(label: str, value: str | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"{label} 날짜 형식이 올바르지 않습니다(YYYY-MM-DD 권장): {value}")
    return pd.Timestamp(ts).normalize()


def _parse_yyyymmdd(value: str) -> pd.Timestamp:
    ts = pd.to_datetime(value, format="%Y%m%d", errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"YYYYMMDD 파싱 실패: {value}")
    return pd.Timestamp(ts).normalize()


def _parse_filename_range(path: Path, pattern: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    m = re.match(pattern, path.name)
    if not m:
        return None
    start = _parse_yyyymmdd(m.group(1))
    end = _parse_yyyymmdd(m.group(2))
    if start > end:
        # 파일명이 뒤바뀐 경우에도 최대한 처리 가능하게 swap
        start, end = end, start
    end_inclusive = end + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    return start, end_inclusive


def _ranges_overlap(
    a_start: pd.Timestamp | None,
    a_end_inclusive: pd.Timestamp | None,
    b_start: pd.Timestamp,
    b_end_inclusive: pd.Timestamp,
) -> bool:
    # a가 None이면 필터가 열려있다고 간주(겹침)
    if a_start is None and a_end_inclusive is None:
        return True
    if a_start is None:
        return b_start <= a_end_inclusive
    if a_end_inclusive is None:
        return b_end_inclusive >= a_start
    return not (b_end_inclusive < a_start or b_start > a_end_inclusive)


def _scan_filename_dataset_range(files: List[Path], pattern: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    starts: list[pd.Timestamp] = []
    ends: list[pd.Timestamp] = []
    for p in files:
        fr = _parse_filename_range(p, pattern)
        if fr is None:
            continue
        s, e = fr
        starts.append(s)
        ends.append(e)
    if not starts or not ends:
        return None
    return min(starts), max(ends)


def _resolve_filename_filter_enabled(raw: object, *, has_user_range: bool) -> bool:
    """
    - true/false: 강제 on/off
    - "auto"(기본): 사용자 기간(start/end 중 하나라도 지정)이 있을 때만 파일명 필터 ON
    """
    if raw is None:
        raw = "auto"
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v in {"auto", ""}:
            return bool(has_user_range)
        if v in {"1", "true", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "no", "n", "off"}:
            return False
        # 알 수 없는 문자열은 안전하게 OFF
        return False
    return bool(raw)


def run_keyword_extractor(
    start_date: str | None = None,
    end_date: str | None = None,
    use_feature_column_mode: Optional[bool] = None,
    category_filter_values: Optional[List[str]] = None,
) -> Dict[str, Path]:
    logger = get_logger("steps.keyword_extractor")
    start_ts = _parse_iso_date("start_date", start_date)
    end_ts = _parse_iso_date("end_date", end_date)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError("start_date가 end_date보다 늦습니다.")
    filter_end_inclusive = None
    if end_ts is not None:
        filter_end_inclusive = end_ts + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)

    with log_step(
        logger,
        1,
        "keyword_extractor",
        start_date=start_date,
        end_date=end_date,
    ):
        config = load_config()
        paths = config["paths"]
        news_cfg = config["news"]

        news_dir = resolve_path(paths["news_dir"])
        output_dir = ensure_output_dir()
        stopwords = _load_stopwords(resolve_path(paths["stopwords_path"]))
        kiwi = None
        if Kiwi is not None:
            try:
                kiwi = Kiwi(num_workers=0, model_type="sbg", load_default_dict=True)
                logger.info("Kiwi 초기화 완료 | model_type=sbg (강제)")
            except Exception as exc:
                raise RuntimeError(
                    "Kiwi sbg 모델 초기화에 실패했습니다. "
                    "sbg 모델 파일 설치/버전을 확인하세요."
                ) from exc

        # 모드 전환: 기본은 기존 명사 추출, 필요시 특성추출 컬럼 직접 사용
        if use_feature_column_mode is None:
            use_feature_column_mode = bool(news_cfg.get("use_feature_column_mode", False))
        if category_filter_values is None:
            category_filter_values = list(news_cfg.get("category_filter_values", []))
        category_filter_values = [str(v).strip() for v in category_filter_values if str(v).strip()]
        logger.info(
            "키워드 모드 | use_feature_column_mode=%s | category_filter_values=%s",
            use_feature_column_mode,
            category_filter_values,
        )

        files = sorted(news_dir.glob(news_cfg["excel_pattern"]))
        if not files:
            raise FileNotFoundError(f"뉴스 파일을 찾을 수 없습니다: {news_dir}")
        logger.info("입력 파일 수: %d", len(files))

        weekly_rows: List[Dict[str, str | int]] = []
        processed_files = 0
        files_opened = 0
        skipped_by_filename = 0
        skipped_filename_unparsed = 0
        skipped_row_filter = 0
        empty_after_nouns = 0

        has_user_range = (start_ts is not None) or (filter_end_inclusive is not None)
        filename_filter_enabled = _resolve_filename_filter_enabled(
            news_cfg.get("filename_date_filter", "auto"),
            has_user_range=has_user_range,
        )
        filename_pattern = str(
            news_cfg.get("filename_date_pattern", r"^NewsResult_(\d{8})-(\d{8})\.xlsx$")
        )
        dataset_range = _scan_filename_dataset_range(files, filename_pattern)
        if dataset_range is not None:
            logger.info(
                "파일명 기준 데이터셋 기간(추정) | min=%s | max=%s | (파일명 구간의 union)",
                dataset_range[0].date(),
                dataset_range[1].date(),
            )
        logger.info(
            "파일명 기반 사전필터 | enabled=%s | user_range=%s~%s",
            filename_filter_enabled,
            start_ts.date() if start_ts is not None else None,
            end_ts.date() if end_ts is not None else None,
        )

        for excel_path in files:
            if filename_filter_enabled and (start_ts is not None or filter_end_inclusive is not None):
                file_range = _parse_filename_range(excel_path, filename_pattern)
                if file_range is None:
                    skipped_filename_unparsed += 1
                    logger.debug("파일명 날짜 구간 파싱 실패(내용 읽기로 진행): %s", excel_path.name)
                else:
                    f_start, f_end = file_range
                    if not _ranges_overlap(start_ts, filter_end_inclusive, f_start, f_end):
                        skipped_by_filename += 1
                        logger.debug(
                            "파일명 기간과 사용자 기간이 겹치지 않아 스킵(엑셀 미오픈) | file=%s | file_range=%s~%s | user_range=%s~%s",
                            excel_path.name,
                            f_start.date(),
                            f_end.date(),
                            start_ts.date() if start_ts is not None else None,
                            end_ts.date() if end_ts is not None else None,
                        )
                        continue

            logger.info("파일 처리 중: %s", excel_path.name)
            df = pd.read_excel(excel_path)
            files_opened += 1
            if df.empty:
                logger.warning("빈 파일 건너뜀: %s", excel_path.name)
                continue

            text_col = _pick_column(df.columns.tolist(), news_cfg["candidate_text_columns"])
            source_col = _pick_column(df.columns.tolist(), news_cfg["source_columns"])
            date_col = _pick_column(
                df.columns.tolist(), ["date", "날짜", "일자", "published_at", "등록일", "작성일"]
            )
            category_col = _pick_column(df.columns.tolist(), ["통합 분류1", "통합분류1", "category1"])
            feature_col = _pick_column(
                df.columns.tolist(),
                ["특성추출(가중치순 상위 50개)", "특성추출", "feature_top50", "feature_keywords"],
            )

            if use_feature_column_mode:
                if feature_col is None:
                    logger.warning("특성추출 컬럼 미탐지로 건너뜀: %s", excel_path.name)
                    continue
            elif text_col is None:
                logger.warning("본문 컬럼 미탐지로 건너뜀: %s", excel_path.name)
                continue
            if date_col is None:
                # 파일명에서 기간이 제공되어도 주차 집계를 위해 최소한의 날짜열이 필요함
                logger.warning("날짜 컬럼 미탐지로 건너뜀: %s", excel_path.name)
                continue

            selected_cols = [date_col, source_col, category_col]
            if use_feature_column_mode:
                selected_cols.append(feature_col)
            else:
                selected_cols.append(text_col)
            tmp = df[[c for c in selected_cols if c is not None]].copy()
            tmp["__dt"] = _coerce_news_date_series(tmp[date_col])
            before_rows = int(len(tmp))
            required_text_col = feature_col if use_feature_column_mode else text_col
            tmp = tmp.dropna(subset=["__dt", required_text_col])
            after_parse_rows = int(len(tmp))

            if start_ts is not None:
                tmp = tmp[tmp["__dt"] >= start_ts]
            if end_ts is not None:
                # inclusive end date (day-level)
                tmp = tmp[tmp["__dt"] <= end_ts + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)]

            if use_feature_column_mode and category_filter_values:
                if category_col is None:
                    logger.warning("통합분류1 컬럼 미탐지로 건너뜀: %s", excel_path.name)
                    continue
                tmp = tmp[
                    tmp[category_col]
                    .astype(str)
                    .apply(lambda x: any(v in x for v in category_filter_values))
                ]

            after_filter_rows = int(len(tmp))
            if after_filter_rows == 0:
                skipped_row_filter += 1
                logger.debug(
                    "행 단위 기간 필터로 인해 데이터 없음(건너뜀) | file=%s | before_rows=%d | parsed_rows=%d | filtered_rows=%d",
                    excel_path.name,
                    before_rows,
                    after_parse_rows,
                    after_filter_rows,
                )
                continue

            tmp["week"] = _iso_week_label(tmp["__dt"])

            file_had_output = False
            for _, row in tmp.iterrows():
                if use_feature_column_mode:
                    nouns = _extract_feature_keywords(row[feature_col], stopwords)
                else:
                    nouns = _extract_nouns(row[text_col], kiwi, stopwords)
                source_val = row[source_col] if source_col else "unknown"
                for keyword in nouns:
                    file_had_output = True
                    weekly_rows.append(
                        {
                            "week": row["week"],
                            "keyword": keyword,
                            "source": str(source_val),
                            "count": 1,
                        }
                    )
            if file_had_output:
                processed_files += 1
            else:
                empty_after_nouns += 1
                logger.debug("명사 추출 결과가 비어 파일 스킵: %s", excel_path.name)

        logger.info(
            "파일 스킵 요약 | filename_skipped=%d | filename_unparsed=%d | rowfilter_skipped=%d | opened=%d | noun_empty=%d | processed=%d",
            skipped_by_filename,
            skipped_filename_unparsed,
            skipped_row_filter,
            files_opened,
            empty_after_nouns,
            processed_files,
        )

        result = pd.DataFrame(weekly_rows)
        if result.empty:
            hint = ""
            if dataset_range is not None and (start_ts is not None or end_ts is not None):
                hint = (
                    f" 파일명 기준 추정 데이터 범위는 {dataset_range[0].date()}~{dataset_range[1].date()} 입니다."
                    " 사용자 기간과 맞지 않으면 결과가 비어있을 수 있습니다."
                    " `config/pipeline_config.json`의 `news.filename_date_filter`를 false로 두거나,"
                    " 기간을 데이터 범위에 맞게 조정하세요."
                )
            raise ValueError(
                "키워드 추출 결과가 비어 있습니다."
                f" (opened_files={files_opened}, filename_skipped={skipped_by_filename}, rowfilter_skipped={skipped_row_filter}, noun_empty_files={empty_after_nouns})"
                + hint
            )

        result = (
            result.groupby(["week", "keyword", "source"], as_index=False)["count"]
            .sum()
            .sort_values(["week", "count"], ascending=[True, False])
        )

        out_path = output_dir / "weekly_keywords.csv"
        written_path = write_csv(result, out_path)
        json_path = write_dataframe_json_export(
            result,
            written_path,
            step="keyword_extractor",
            extra_meta={
                "processed_files": processed_files,
                "start_date": start_date,
                "end_date": end_date,
                "use_feature_column_mode": use_feature_column_mode,
                "category_filter_values": category_filter_values,
            },
        )
        meta_path = written_path.with_suffix(".meta.json")
        logger.info(
            "keyword_extractor 요약 | 처리파일=%d | 행수=%d | csv=%s | json=%s",
            processed_files,
            len(result),
            written_path,
            json_path,
        )
        log_artifact(logger, "OUTPUT_CSV", written_path)
        log_artifact(logger, "OUTPUT_JSON", json_path)
        if meta_path.exists():
            log_artifact(logger, "OUTPUT_JSON_META", meta_path)
        return {"csv": written_path, "json": json_path}

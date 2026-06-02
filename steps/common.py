from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "pipeline_config.json"


def _env_truthy(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return default


def files_output_enabled() -> bool:
    """CSV/JSON 파일 산출 여부. env SQUADONE_WRITE_FILES 로 제어(기본 true).

    DB가 권위 소스로 일원화되기 전까지 단계 간 전달이 여전히 파일 경로에 의존하므로
    기본값은 true(병행 출력)다. 단계를 DB 입력으로 모두 전환한 뒤 false 로 끄면
    `data/output` 디스크 폭증 없이 DB만 적재한다.
    """
    return _env_truthy(os.environ.get("SQUADONE_WRITE_FILES"), default=True)


def configure_logging(
    level: str = "INFO",
    log_file: str | None = None,
    stream: str = "stdout",
    *,
    replace_root_handlers: bool = True,
) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    log_format = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    logger = logging.getLogger()
    logger.setLevel(log_level)

    if replace_root_handlers:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)

    target_stream = sys.stderr if stream.lower() == "stderr" else sys.stdout
    has_same_stream = any(
        isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is target_stream
        for h in logger.handlers
    )
    if replace_root_handlers or not has_same_stream:
        console_handler = logging.StreamHandler(stream=target_stream)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(logging.Formatter(log_format))
        logger.addHandler(console_handler)

    if log_file:
        log_path = resolve_path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        resolved = str(log_path.resolve())
        has_same_file = any(
            isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == resolved
            for h in logger.handlers
        )
        if not has_same_file:
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setLevel(log_level)
            file_handler.setFormatter(logging.Formatter(log_format))
            logger.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


@contextlib.contextmanager
def log_step(logger: logging.Logger, step_no: int, step_name: str, **fields: Any) -> Iterator[None]:
    prefix = f"[STEP {step_no}] {step_name}"
    if fields:
        kv = " | ".join(f"{k}={v}" for k, v in fields.items())
        logger.info("%s | START | %s", prefix, kv)
    else:
        logger.info("%s | START", prefix)
    started = time.perf_counter()
    try:
        yield
    finally:
        logger.info("%s | END | elapsed=%.2fs", prefix, time.perf_counter() - started)


def log_artifact(logger: logging.Logger, label: str, path: Path) -> None:
    try:
        if path.exists():
            logger.info("%s | OK | path=%s | bytes=%d", label, str(path.resolve()), path.stat().st_size)
        else:
            logger.error("%s | MISSING | path=%s", label, str(path.resolve()))
    except OSError as exc:
        logger.error("%s | STAT_FAILED | path=%s | err=%s", label, str(path.resolve()), exc)


def load_config() -> Dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def resolve_path(path_str: str) -> Path:
    return (PROJECT_ROOT / path_str).resolve()


def ensure_output_dir() -> Path:
    config = load_config()
    output_dir = resolve_path(config["paths"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig")


def write_csv(df: pd.DataFrame, path: Path) -> Path:
    if not files_output_enabled():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        df.to_csv(tmp_path, index=False, encoding="utf-8-sig")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return path


def write_json(payload: Any, path: Path, *, indent: int | None = 2) -> Path:
    if not files_output_enabled():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=indent, default=str)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return path


def write_dataframe_json_export(
    df: pd.DataFrame,
    csv_path: Path,
    *,
    step: str,
    extra_meta: Dict[str, Any] | None = None,
) -> Path:
    """
    CSV와 동일한 베이스 파일명으로 JSON을 함께 저장한다.

    - 기본은 pandas to_json(records, lines=True)로 전체를 JSONL로 저장
    - 행/열이 너무 크면 메타데이터 + 상위 N행 샘플만 저장(디스크 폭증 방지)
    """
    logger = get_logger("steps.export")
    if not files_output_enabled():
        return csv_path.with_suffix(".json")
    config = load_config()
    export_cfg = config.get(
        "export",
        {
            "json_orient": "records",
            "json_lines": True,
            "json_indent": None,
            "full_max_rows": 200000,
            "full_max_cols": 400,
            "sample_rows": 200,
        },
    )

    orient = str(export_cfg.get("json_orient", "records"))
    lines = bool(export_cfg.get("json_lines", True))
    indent = export_cfg.get("json_indent", None)
    full_max_rows = int(export_cfg.get("full_max_rows", 200000))
    full_max_cols = int(export_cfg.get("full_max_cols", 400))
    sample_rows = int(export_cfg.get("sample_rows", 200))

    json_path = csv_path.with_suffix(".json")
    rows = int(len(df))
    cols = int(len(df.columns))

    base_meta: Dict[str, Any] = {
        "step": step,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "csv_path": str(csv_path.resolve()),
        "json_path": str(json_path.resolve()),
        "row_count": rows,
        "column_count": cols,
        "columns": list(df.columns),
    }
    if extra_meta:
        base_meta.update(extra_meta)

    too_large = rows > full_max_rows or cols > full_max_cols
    if too_large:
        sample = df.head(sample_rows)
        payload: Dict[str, Any] = {
            **base_meta,
            "export_mode": "compact",
            "reason": "row_or_column_count_exceeds_threshold",
            "thresholds": {"full_max_rows": full_max_rows, "full_max_cols": full_max_cols},
            "sample_row_count": int(len(sample)),
            "sample_rows": sample.to_dict(orient="records"),
        }
        written = write_json(payload, json_path, indent=2 if indent is None else int(indent))
        logger.warning(
            "JSON compact 저장 | step=%s | rows=%d cols=%d | -> %s",
            step,
            rows,
            cols,
            written,
        )
        return written

    # 전체 export: JSONL(records+lines) 권장
    json_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=json_path.name + ".", suffix=".tmp", dir=str(json_path.parent))
    os.close(fd)
    tmp_json = Path(tmp_name)
    try:
        if indent is None:
            df.to_json(tmp_json, orient=orient, lines=lines, force_ascii=False)
        else:
            # indent가 지정되면 lines=False가 일반적
            df.to_json(tmp_json, orient=orient, lines=False, force_ascii=False, indent=int(indent))
        os.replace(tmp_json, json_path)
    finally:
        if tmp_json.exists():
            try:
                tmp_json.unlink()
            except OSError:
                pass

    manifest = {**base_meta, "export_mode": "full", "json_orient": orient, "json_lines": lines}
    write_json(manifest, json_path.with_suffix(".meta.json"), indent=2)
    logger.info("JSON full 저장 | step=%s | rows=%d cols=%d | -> %s", step, rows, cols, json_path)
    return json_path

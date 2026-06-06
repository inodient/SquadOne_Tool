"""Stage0b — 뉴스 엑셀 → 임베딩 → Qdrant 증분 적재 (쓰기 경로).

현행 SquadOne_Tool 은 Qdrant 를 '읽기'만 했다(qdrant_news/qdrant_search/keyword_extractor).
본 모듈이 빠진 고리인 '쓰기'를 담당한다. 출처: SquadOne_Tool_news_vectordb_embedding/
scripts/ingest_news_to_qdrant.py 를 통합 규약(config['ingest'] · db.config.qdrant_url() ·
프로젝트 로거 · DB ingest_state 추적)에 맞춰 이식.

- 입력: config['ingest']['data_dir']/NewsResult_*.xlsx (BigKinds 다운로드 산출)
- 임베딩: named vectors title/body, paraphrase-multilingual-mpnet-base-v2, normalize=True
- payload 계약: docs/QDRANT_CONTRACT.md (news_id/date/press/url/title/body/source_file/
  file_date_start/file_date_end) — 1·5·6단계가 의존.
- 증분: 파일 체크섬(sha256) 비교. 상태는 DB(newstrend.ingest_state) 또는 .state json.

CLI:
    python -m steps.qdrant_ingest --limit-files 1          # 1개만 테스트
    python -m steps.qdrant_ingest --estimate-only          # ETA만
    python -m steps.qdrant_ingest                          # 전체 증분 적재
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from db.config import load_shared_env, qdrant_url
from steps.common import get_logger, load_config, resolve_path

load_shared_env()
logger = get_logger("qdrant_ingest")

DEFAULT_DATA_GLOB = "NewsResult_*.xlsx"
DEFAULT_ETA_SAMPLE_FILES = 3
DEFAULT_CHECKSUM_LOG_EVERY = 50

STATE_DIR = Path(".state")
STATE_FILE = STATE_DIR / "news_ingest_state.json"

COL_NEWS_ID = "뉴스 식별자"
COL_DATE = "일자"
COL_PRESS = "언론사"
COL_TITLE = "제목"
COL_BODY = "본문"
COL_URL = "URL"


def _ingest_cfg() -> dict:
    return load_config().get("ingest", {})


# ── 상태(state): DB 우선, 파일 폴백 ───────────────────────────────

def _load_file_state(state_path: Path) -> Dict[str, str]:
    if not state_path.exists():
        return {}
    with state_path.open("r", encoding="utf-8") as f:
        return json.load(f).get("processed_files", {})


def _save_file_state(state_path: Path, processed: Dict[str, str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump({"processed_files": processed}, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(state_path)


def get_file_checksum(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def extract_file_date_range(filename: str) -> Tuple[Optional[str], Optional[str]]:
    m = re.match(r"NewsResult_(\d{8})(?:-(\d{8}))?\.xlsx$", filename)
    if not m:
        return None, None
    return m.group(1), m.group(2) or m.group(1)


def normalize_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def normalize_date(value: object) -> str:
    text = normalize_str(value)
    digits = re.sub(r"\D", "", text)
    return digits[:8] if len(digits) >= 8 else text


def stable_point_id(file_name: str, row_index: int, news_id: str, url: str, title: str, body: str) -> str:
    base = "||".join([file_name, str(row_index), news_id, url, title[:200], body[:500]])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, base))


def pick_device(device_arg: str) -> str:
    if device_arg != "auto":
        return device_arg
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def format_seconds(seconds: float) -> str:
    if seconds < 0:
        return "N/A"
    sec = int(seconds)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def ensure_collection(client, collection: str, vector_size: int) -> None:
    from qdrant_client.http import models

    existing = [c.name for c in client.get_collections().collections]
    if collection in existing:
        logger.info("컬렉션 이미 존재: %s", collection)
        return
    client.create_collection(
        collection_name=collection,
        vectors_config={
            "title": models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
            "body": models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
        },
    )
    logger.info("컬렉션 생성 완료: %s", collection)


def wait_for_qdrant_ready(client, retries: int, retry_delay_sec: float) -> None:
    from qdrant_client.http.exceptions import ResponseHandlingException

    for attempt in range(retries + 1):
        try:
            _ = client.get_collections()
            if attempt > 0:
                logger.info("Qdrant 연결 복구됨")
            return
        except ResponseHandlingException as exc:
            if attempt >= retries:
                raise
            delay = retry_delay_sec * (2 ** attempt)
            logger.warning("Qdrant 준비 대기: 재시도 %d/%d (delay=%.1fs, reason=%s)",
                           attempt + 1, retries, delay, exc)
            time.sleep(delay)


def build_points_from_df(df: pd.DataFrame, model, file_name: str,
                         file_date_start: Optional[str], file_date_end: Optional[str],
                         encode_batch_size: int) -> list:
    from qdrant_client.http import models

    missing = [c for c in [COL_DATE, COL_PRESS, COL_TITLE, COL_BODY, COL_URL] if c not in df.columns]
    if missing:
        raise ValueError(f"필수 컬럼 누락: {missing}")

    title_texts: List[str] = []
    body_texts: List[str] = []
    rows_cache: List[Tuple[int, pd.Series, str, str]] = []
    for idx, row in df.iterrows():
        title = normalize_str(row.get(COL_TITLE))
        body = normalize_str(row.get(COL_BODY))
        if not title and not body:
            continue
        title_texts.append(title if title else "(제목 없음)")
        body_texts.append(body if body else "(본문 없음)")
        rows_cache.append((idx, row, title, body))
    if not rows_cache:
        return []

    title_vectors = model.encode(title_texts, batch_size=encode_batch_size,
                                 show_progress_bar=False, normalize_embeddings=True)
    body_vectors = model.encode(body_texts, batch_size=encode_batch_size,
                                show_progress_bar=False, normalize_embeddings=True)

    points = []
    for i, (row_idx, row, title, body) in enumerate(rows_cache):
        news_id = normalize_str(row.get(COL_NEWS_ID))
        url = normalize_str(row.get(COL_URL))
        payload = {
            "news_id": news_id,
            "date": normalize_date(row.get(COL_DATE)),
            "press": normalize_str(row.get(COL_PRESS)),
            "url": url,
            "title": title,
            "body": body,
            "source_file": file_name,
            "file_date_start": file_date_start,
            "file_date_end": file_date_end,
        }
        points.append(models.PointStruct(
            id=stable_point_id(file_name, row_idx, news_id, url, title, body),
            vector={"title": title_vectors[i].tolist(), "body": body_vectors[i].tolist()},
            payload=payload,
        ))
    return points


def upsert_with_retry(client, collection_name: str, points: list,
                      timeout_sec: int, retries: int, retry_delay_sec: float) -> None:
    from qdrant_client.http.exceptions import ResponseHandlingException

    for attempt in range(retries + 1):
        try:
            client.upsert(collection_name=collection_name, points=points, wait=True, timeout=int(timeout_sec))
            return
        except ResponseHandlingException as exc:
            if attempt >= retries:
                raise
            delay = retry_delay_sec * (2 ** attempt)
            logger.warning("업서트 타임아웃: 재시도 %d/%d (batch=%d, delay=%.1fs, reason=%s)",
                           attempt + 1, retries, len(points), delay, exc)
            time.sleep(delay)


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def run_qdrant_ingest(
    *,
    data_dir: Optional[str] = None,
    file_glob: str = DEFAULT_DATA_GLOB,
    collection: Optional[str] = None,
    model_name: Optional[str] = None,
    batch_size: Optional[int] = None,
    device: Optional[str] = None,
    reprocess_all: bool = False,
    limit_files: Optional[int] = None,
    skip_checksum: bool = False,
    estimate_only: bool = False,
) -> Dict[str, object]:
    """뉴스 엑셀을 Qdrant 에 증분 적재. 반환: 처리 요약 dict."""
    from sentence_transformers import SentenceTransformer
    from qdrant_client import QdrantClient

    cfg = _ingest_cfg()
    data_dir_p = resolve_path(data_dir or cfg.get("data_dir", "data/news"))
    collection = collection or cfg.get("collection", "news_10y_ko_v1")
    model_name = model_name or cfg.get("embed_model", "sentence-transformers/paraphrase-multilingual-mpnet-base-v2")
    batch_size = int(batch_size or cfg.get("batch_size", 128))
    encode_batch_size = int(cfg.get("encode_batch_size", 64))
    device = device or cfg.get("embed_device", "auto")
    use_db_state = bool(cfg.get("use_db_state", True))

    if not data_dir_p.exists():
        raise FileNotFoundError(f"데이터 폴더를 찾을 수 없습니다: {data_dir_p}")
    files = sorted(data_dir_p.glob(file_glob))
    if limit_files is not None:
        files = files[:limit_files]
    if not files:
        logger.warning("처리할 파일이 없습니다. (%s/%s)", data_dir_p, file_glob)
        return {"processed_files": 0, "points": 0}

    # 상태 로딩(DB 우선)
    if use_db_state:
        from db import repository as repo
        processed: Dict[str, str] = repo.read_ingest_state()
    else:
        processed = _load_file_state(STATE_FILE)

    files_with_checksum: List[Tuple[Path, str]] = []
    files_to_process: List[Path] = []
    for idx, fp in enumerate(files, start=1):
        if skip_checksum:
            marker = processed.get(fp.name)
            if reprocess_all or not marker:
                files_to_process.append(fp)
            files_with_checksum.append((fp, marker or "filename-only"))
        else:
            checksum = get_file_checksum(fp)
            files_with_checksum.append((fp, checksum))
            if reprocess_all or processed.get(fp.name) != checksum:
                files_to_process.append(fp)
        if idx == 1 or idx % DEFAULT_CHECKSUM_LOG_EVERY == 0 or idx == len(files):
            logger.info("스캔 진행: %d/%d files | 처리 대상: %d", idx, len(files), len(files_to_process))

    if not files_to_process:
        logger.info("처리할 신규/변경 파일이 없습니다.")
        return {"processed_files": 0, "points": 0}

    if estimate_only:
        logger.info("estimate-only: 대상 파일 %d개 (실제 적재 안 함)", len(files_to_process))
        return {"to_process": len(files_to_process), "points": 0}

    dev = pick_device(device)
    logger.info("임베딩 모델 로딩: %s (device=%s)", model_name, dev)
    model = SentenceTransformer(model_name, device=dev)
    vector_size = (model.get_embedding_dimension() if hasattr(model, "get_embedding_dimension")
                   else model.get_sentence_embedding_dimension())
    if not vector_size:
        raise RuntimeError("임베딩 차원 정보를 확인할 수 없습니다.")

    client = QdrantClient(url=qdrant_url(), timeout=int(cfg.get("upsert_timeout_sec", 120)))
    wait_for_qdrant_ready(client, int(cfg.get("startup_retries", 8)), float(cfg.get("startup_retry_delay_sec", 2.0)))
    ensure_collection(client, collection, vector_size)

    started = time.perf_counter()
    points_done = 0
    processed_count = 0
    for fp, checksum in files_with_checksum:
        if not reprocess_all:
            if skip_checksum and processed.get(fp.name):
                continue
            if not skip_checksum and processed.get(fp.name) == checksum:
                continue
        logger.info("처리 시작: %s", fp.name)
        df = pd.read_excel(fp)
        ds, de = extract_file_date_range(fp.name)
        points = build_points_from_df(df, model, fp.name, ds, de, encode_batch_size)
        marker = "filename-only" if skip_checksum else checksum
        if not points:
            logger.info("유효 뉴스 없음: %s", fp.name)
        else:
            for batch in _chunks(points, batch_size):
                upsert_with_retry(client, collection, batch,
                                  int(cfg.get("upsert_timeout_sec", 120)),
                                  int(cfg.get("upsert_retries", 4)), 2.0)
        # 상태 기록
        if use_db_state:
            from db import repository as repo
            repo.upsert_ingest_state(fp.name, marker, len(points), collection)
        else:
            processed[fp.name] = marker
            _save_file_state(STATE_FILE, processed)
        processed_count += 1
        points_done += len(points)
        elapsed = time.perf_counter() - started
        logger.info("처리 완료: %s (points=%d) | 누적 %d files, %d points | 경과 %s",
                    fp.name, len(points), processed_count, points_done, format_seconds(elapsed))

    logger.info("적재 완료 | 파일 %d개 | points %d | 총 %s",
                processed_count, points_done, format_seconds(time.perf_counter() - started))
    return {"processed_files": processed_count, "points": points_done, "collection": collection}


def main() -> None:
    p = argparse.ArgumentParser(description="뉴스 엑셀 → Qdrant 증분 적재 (Stage0b)")
    p.add_argument("--data-dir", default=None)
    p.add_argument("--file-glob", default=DEFAULT_DATA_GLOB)
    p.add_argument("--collection", default=None)
    p.add_argument("--model-name", default=None)
    p.add_argument("--batch-size", type=int, default=None)
    p.add_argument("--device", default=None, choices=["auto", "cpu", "cuda", "mps"])
    p.add_argument("--reprocess-all", action="store_true")
    p.add_argument("--limit-files", type=int, default=None)
    p.add_argument("--skip-checksum", action="store_true")
    p.add_argument("--estimate-only", action="store_true")
    args = p.parse_args()
    out = run_qdrant_ingest(
        data_dir=args.data_dir, file_glob=args.file_glob, collection=args.collection,
        model_name=args.model_name, batch_size=args.batch_size, device=args.device,
        reprocess_all=args.reprocess_all, limit_files=args.limit_files,
        skip_checksum=args.skip_checksum, estimate_only=args.estimate_only,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

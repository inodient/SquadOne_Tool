"""1단계 재실행 — 2023~2025를 ISO 주 13개씩 청크로 순차 실행(메모리 안전 + 경계 무손실).

각 청크 실행 전후로 진행/메모리(RSS)를 /tmp/step1_run.log 에 기록한다.
"""
from __future__ import annotations
import os
import subprocess
import datetime
import resource

ROOT = "/Users/inodient/Desktop/SquadOne_Tool"
PY = f"{ROOT}/.venv/bin/python"
LOG = "/tmp/step1_run.log"
CHUNK_WEEKS = 13


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"[{_now()}] {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")


def iso_weeks(y0: int, y1: int):
    out = []
    for y in range(y0, y1 + 1):
        w = 1
        while True:
            try:
                d = datetime.date.fromisocalendar(y, w, 1)
            except ValueError:
                break
            if d.isocalendar()[0] != y:
                break
            out.append((y, w))
            w += 1
    return out


def main() -> None:
    weeks = iso_weeks(2023, 2025)
    chunks = [weeks[i:i + CHUNK_WEEKS] for i in range(0, len(weeks), CHUNK_WEEKS)]
    log(f"=== STEP1 RERUN 시작 | 총 주차={len(weeks)} | 청크={len(chunks)} (각 {CHUNK_WEEKS}주) ===")
    env = {**os.environ, "PYTHONPATH": ROOT}
    for ci, chunk in enumerate(chunks, 1):
        y0, w0 = chunk[0]
        y1, w1 = chunk[-1]
        start = datetime.date.fromisocalendar(y0, w0, 1).isoformat()      # 월요일
        end = datetime.date.fromisocalendar(y1, w1, 7).isoformat()        # 일요일
        log(f"[chunk {ci}/{len(chunks)}] START {start}~{end} ({y0}-W{w0:02d}~{y1}-W{w1:02d})")
        t0 = datetime.datetime.now()
        r = subprocess.run(
            [PY, "scripts/run_step.py", "1", "--start-date", start, "--end-date", end],
            cwd=ROOT, env=env,
        )
        rss_gb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / (1024 ** 3)  # macOS: bytes
        dt = (datetime.datetime.now() - t0).total_seconds()
        log(f"[chunk {ci}/{len(chunks)}] DONE exit={r.returncode} | {dt:.0f}s | child_max_rss~{rss_gb:.1f}GB")
        if r.returncode != 0:
            log(f"[chunk {ci}] !! 비정상 종료 — 중단. 로그/인시던트 확인 필요.")
            return
    log("=== STEP1 RERUN 전체 완료 ===")


if __name__ == "__main__":
    main()

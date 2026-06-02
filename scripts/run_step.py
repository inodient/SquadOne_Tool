"""단계별 실행 CLI (REST 서버 불필요).

단계 번호:
  1  keyword_extractor   (Qdrant→DB, --start-date/--end-date 또는 --week)
  2  frequency_matrix    (증분 집계)
  3  base_calculation    (전체 재계산)
  4  z_score_filtering
  56 trend_extractor     (5+6 통합, --base-start-week/--base-end-week 또는 --week)
  7  product_extractor   (--news-keyword 선택)

사용 예:
  python -m scripts.run_step 1 --week 2023-W13
  python -m scripts.run_step 2
  python -m scripts.run_step 3
  python -m scripts.run_step 4
  python -m scripts.run_step 56 --week 2023-W13
  python -m scripts.run_step 7  --week 2023-W13

LLM 환경은 .env(LLM_PROVIDER)로 제어. 무거운 단계(56)는 임베딩 모델을 로드한다.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from db.config import load_shared_env

load_shared_env()

from steps.common import configure_logging, get_logger  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data" / "output"


def _week_to_dates(week: str) -> tuple[str, str]:
    m = re.fullmatch(r"(\d{4})-W(\d{2})", week.strip())
    if not m:
        raise ValueError(f"주차 형식 오류(YYYY-Www): {week}")
    mon = datetime.strptime(f"{m.group(1)}-W{m.group(2)}-1", "%G-W%V-%u")
    return mon.strftime("%Y-%m-%d"), (mon + timedelta(days=6)).strftime("%Y-%m-%d")


def main() -> int:
    ap = argparse.ArgumentParser(description="뉴스 트렌드 단계별 실행")
    ap.add_argument("step", choices=["1", "2", "3", "4", "56", "5", "6", "7"], help="단계 번호(5+6은 56)")
    ap.add_argument("--week", default=None, help="편의: 단일 주차(YYYY-Www) → 날짜범위/coverage 자동")
    ap.add_argument("--start-date", default=None)
    ap.add_argument("--end-date", default=None)
    ap.add_argument("--base-start-week", default=None)
    ap.add_argument("--base-end-week", default=None)
    ap.add_argument("--news-keyword", default=None)
    ap.add_argument("--test-mode", action="store_true")
    ap.add_argument("--test-max-weeks", type=int, default=None)
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args()

    if args.week:
        ws, we = _week_to_dates(args.week)
        args.start_date = args.start_date or ws
        args.end_date = args.end_date or we
        args.base_start_week = args.base_start_week or args.week
        args.base_end_week = args.base_end_week or args.week

    configure_logging(args.log_level, None, stream="stdout")
    logger = get_logger("run_step")
    step = args.step
    logger.info("단계 실행 | step=%s | week=%s", step, args.week)

    if step == "1":
        from steps.keyword_extractor import run_keyword_extractor
        out = run_keyword_extractor(start_date=args.start_date, end_date=args.end_date)
    elif step == "2":
        from steps.frequency_matrix import run_frequency_matrix
        out = run_frequency_matrix(OUT / "weekly_keywords.csv")
    elif step == "3":
        from steps.base_calculation import run_base_calculation
        out = run_base_calculation()
    elif step == "4":
        from steps.z_score_filtering import run_z_score_filtering
        bundle = {"csv": OUT / "weekly_keywords.csv", "json": OUT / "weekly_keywords.json"}
        out = run_z_score_filtering(OUT / "base_calculation.csv", bundle)
    elif step in ("56", "5", "6"):
        from steps.trend_extractor import run_trend_extractor
        out = run_trend_extractor(
            OUT / "z_score_keywords.csv",
            OUT / "z_score_keywords_2.0.csv",
            OUT / "weekly_keywords.csv",
            base_start_week=args.base_start_week,
            base_end_week=args.base_end_week,
            test_mode=args.test_mode,
            test_max_weeks=args.test_max_weeks,
        )
    elif step == "7":
        from steps.product_extractor import run_product_extractor
        out = run_product_extractor(
            OUT / "trend_timeseries_report.csv",
            base_start_week=args.base_start_week,
            base_end_week=args.base_end_week,
            news_keyword=args.news_keyword,
            test_mode=args.test_mode,
            test_max_weeks=args.test_max_weeks,
        )
    else:
        ap.error(f"알 수 없는 step: {step}")
        return 2

    print(json.dumps({k: str(v) for k, v in out.items()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

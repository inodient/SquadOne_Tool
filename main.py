from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta

from db.config import load_shared_env
from steps.common import configure_logging, get_logger
from tool_news_trend import run_news_trend_pipeline

# 프로젝트 루트 .env를 1회 로딩(멱등) — DB/LLM/임베딩/로깅 env를 일관 적용.
load_shared_env()


def _week_to_dates(week: str) -> tuple[str, str]:
    """ISO 주차 'YYYY-Www' → (월요일, 일요일) YYYY-MM-DD."""
    m = re.fullmatch(r"(\d{4})-W(\d{2})", week.strip())
    if not m:
        raise ValueError(f"주차 형식 오류(YYYY-Www): {week}")
    mon = datetime.strptime(f"{m.group(1)}-W{m.group(2)}-1", "%G-W%V-%u")
    sun = mon + timedelta(days=6)
    return mon.strftime("%Y-%m-%d"), sun.strftime("%Y-%m-%d")


async def _run_mcp_client(
    target_week: str | None,
    start_date: str | None,
    end_date: str | None,
) -> dict:
    """
    MCP SDK가 설치된 경우 stdio 기반으로 tool_news_trend 서버를 호출한다.
    미설치 환경에서는 예외를 발생시켜 direct call 폴백으로 전환한다.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_params = StdioServerParameters(command=sys.executable, args=["tool_news_trend.py"])
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                "run_news_trend",
                {"target_week": target_week, "start_date": start_date, "end_date": end_date},
            )
            if hasattr(result, "content"):
                # MCP SDK 버전에 따라 ToolResult 포맷이 다를 수 있음
                return {"raw": str(result.content)}
            return {"raw": str(result)}


def main() -> None:
    parser = argparse.ArgumentParser(description="뉴스 트렌드 MCP 클라이언트 테스트")
    parser.add_argument("--target-week", type=str, default=None, help="예: 2026-W15")
    parser.add_argument(
        "--week",
        type=str,
        default=None,
        help="편의 옵션: 단일 주차(YYYY-Www). start/end-date(1단계)와 base-start/end-week(5~7 coverage)를 자동 설정.",
    )
    parser.add_argument("--base-start-week", type=str, default=None, help="5~7단계 coverage 시작 주차(YYYY-Www)")
    parser.add_argument("--base-end-week", type=str, default=None, help="5~7단계 coverage 종료 주차(YYYY-Www)")
    parser.add_argument("--news-keyword", type=str, default=None, help="7단계 상품 추출 스코프 키워드")
    parser.add_argument("--test-mode", action="store_true", help="테스트 모드(주차 수 제한)")
    parser.add_argument("--test-max-weeks", type=int, default=None, help="처리할 최대 주차 수")
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="필터 시작일(YYYY-MM-DD). 뉴스 '일자' 기준으로 1단계 입력을 제한합니다.",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="필터 종료일(YYYY-MM-DD, 당일 포함). 뉴스 '일자' 기준으로 1단계 입력을 제한합니다.",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["mcp", "direct"],
        default="mcp",
        help="mcp: MCP 서버 호출, direct: 파이프라인 함수 직접 호출",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="로그 레벨 설정",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="로그 파일 경로(예: data/output/pipeline.log). 미지정 시 터미널만 출력",
    )
    parser.add_argument(
        "--log-stream",
        type=str,
        default=None,
        choices=["stdout", "stderr"],
        help="로그 출력 스트림. 미지정 시 mode=mcp는 stderr, mode=direct는 stdout",
    )
    args = parser.parse_args()

    # --week 편의 옵션 해석: 미지정 인자만 자동 채움
    if args.week:
        ws, we = _week_to_dates(args.week)
        if args.start_date is None:
            args.start_date = ws
        if args.end_date is None:
            args.end_date = we
        if args.base_start_week is None:
            args.base_start_week = args.week
        if args.base_end_week is None:
            args.base_end_week = args.week

    log_stream = args.log_stream or ("stderr" if args.mode == "mcp" else "stdout")
    configure_logging(args.log_level, args.log_file, stream=log_stream)
    logger = get_logger("main")
    logger.info(
        "실행 시작 | mode=%s | target_week=%s | start_date=%s | end_date=%s | log_stream=%s | log_file=%s",
        args.mode,
        args.target_week,
        args.start_date,
        args.end_date,
        log_stream,
        args.log_file,
    )

    if args.mode == "direct":
        logger.info(
            "direct 모드로 파이프라인 호출 | base_week=%s~%s | test_mode=%s | test_max_weeks=%s",
            args.base_start_week, args.base_end_week, args.test_mode, args.test_max_weeks,
        )
        result = run_news_trend_pipeline(
            target_week=args.target_week,
            start_date=args.start_date,
            end_date=args.end_date,
            base_start_week=args.base_start_week,
            base_end_week=args.base_end_week,
            test_mode=args.test_mode,
            test_max_weeks=args.test_max_weeks,
            news_keyword=args.news_keyword,
        )
    else:
        try:
            logger.info("mcp 모드로 서버 호출")
            result = asyncio.run(
                _run_mcp_client(
                    target_week=args.target_week,
                    start_date=args.start_date,
                    end_date=args.end_date,
                )
            )
        except Exception as exc:
            logger.exception("MCP 호출 실패, direct 모드로 폴백")
            result = {
                "status": "fallback_to_direct",
                "reason": str(exc),
                "outputs": run_news_trend_pipeline(
                    target_week=args.target_week,
                    start_date=args.start_date,
                    end_date=args.end_date,
                ).get("outputs", {}),
            }

    logger.info("실행 완료")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

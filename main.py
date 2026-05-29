from __future__ import annotations

import argparse
import asyncio
import json
import sys

from steps.common import configure_logging, get_logger
from tool_news_trend import run_news_trend_pipeline


async def _run_mcp_client(target_week: str | None) -> dict:
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
            result = await session.call_tool("run_news_trend", {"target_week": target_week})
            if hasattr(result, "content"):
                # MCP SDK 버전에 따라 ToolResult 포맷이 다를 수 있음
                return {"raw": str(result.content)}
            return {"raw": str(result)}


def main() -> None:
    parser = argparse.ArgumentParser(description="뉴스 트렌드 MCP 클라이언트 테스트")
    parser.add_argument("--target-week", type=str, default=None, help="예: 2026-W15")
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
    args = parser.parse_args()
    configure_logging(args.log_level, args.log_file)
    logger = get_logger("main")
    logger.info(
        "실행 시작 | mode=%s | target_week=%s | log_file=%s",
        args.mode,
        args.target_week,
        args.log_file,
    )

    if args.mode == "direct":
        logger.info("direct 모드로 파이프라인 호출")
        result = run_news_trend_pipeline(target_week=args.target_week)
    else:
        try:
            logger.info("mcp 모드로 서버 호출")
            result = asyncio.run(_run_mcp_client(target_week=args.target_week))
        except Exception as exc:
            logger.exception("MCP 호출 실패, direct 모드로 폴백")
            result = {
                "status": "fallback_to_direct",
                "reason": str(exc),
                "outputs": run_news_trend_pipeline(target_week=args.target_week).get("outputs", {}),
            }

    logger.info("실행 완료")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

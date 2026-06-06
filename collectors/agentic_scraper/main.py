"""CLI entry: URL and objective → scraping agent → stdout JSON summary.

Legacy pipeline (default):
    python main.py --url <url> --objective <text>

ReAct agent:
    python main.py --react --url <url> --objective <text> [--provider gemini] [--model gemini-2.0-flash]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from agents.base import BaseAgent
from core.config import get_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    p = argparse.ArgumentParser(description="Agentic scraper orchestrator")
    p.add_argument("--url", default="", help="Target page URL")
    p.add_argument("--objective", default="", help="What to extract or accomplish")
    p.add_argument("--stdin", action="store_true", help="Read JSON line with url and objective from stdin")
    p.add_argument("--mock", action="store_true", help="Skip real LLM — use deterministic fake responses")

    # ReAct agent flags
    p.add_argument("--react", action="store_true", help="Use ReAct agent instead of legacy pipeline")
    p.add_argument(
        "--provider",
        choices=["ollama", "gemini", "anthropic"],
        default=None,
        help="Override LLM_PROVIDER from .env",
    )
    p.add_argument("--model", default=None, help="Override LLM_MODEL (e.g. gemini-2.0-flash)")
    p.add_argument("--max-steps", type=int, default=None, dest="max_steps", help="Override REACT_MAX_STEPS")
    p.add_argument("--no-cache", action="store_true", dest="no_cache",
                   help="Force ReAct discovery even if a cached strategy exists")
    p.add_argument("--search", default=None, metavar="QUERY",
                   help="URL 없이 검색어로 시작. --url 대신 사용 가능. (네이버 검색 스크래핑)")

    # Batch flags
    p.add_argument("--batch", metavar="FILE", default=None,
                   help="Text file with one URL per line; scrape all with --objective")
    p.add_argument("--pages", type=int, default=None, metavar="N",
                   help="Paginate: scrape N pages starting from --url")

    # Cache management flags (exit early, no scraping)
    p.add_argument("--cache-list", action="store_true", dest="cache_list",
                   help="Print all cached strategies as JSON and exit")
    p.add_argument("--cache-clear", metavar="DOMAIN", nargs="?", const="__all__",
                   dest="cache_clear",
                   help="Clear cache entries. Omit domain to clear all.")

    args = p.parse_args()

    # Build config overrides from CLI flags
    overrides: dict = {}
    if args.mock:
        overrides["mock_llm"] = True
    if args.provider:
        overrides["llm_provider"] = args.provider
    if args.model:
        overrides["llm_model"] = args.model
    if args.max_steps:
        overrides["react_max_steps"] = args.max_steps

    cfg = get_config(overrides if overrides else None)

    if args.stdin:
        line = sys.stdin.readline()
        data = json.loads(line) if line.strip() else {}
        url = str(data.get("url", ""))
        objective = str(data.get("objective", ""))
    else:
        url = args.url
        objective = args.objective

    # ── Cache management commands ─────────────────────────────────────────────
    if args.cache_list:
        from cache.selector_cache import SelectorCache
        entries = SelectorCache(cfg).list_entries()
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        sys.exit(0)

    if args.cache_clear is not None:
        from cache.selector_cache import SelectorCache
        domain = None if args.cache_clear == "__all__" else args.cache_clear
        deleted = SelectorCache(cfg).clear(domain=domain)
        print(f"Cleared {deleted} cache entries (domain={domain!r})")
        sys.exit(0)

    # ── Batch execution ───────────────────────────────────────────────────────
    if args.batch:
        if not args.objective:
            logger.error("--batch requires --objective")
            sys.exit(2)
        from pathlib import Path as _Path
        batch_file = _Path(args.batch)
        if not batch_file.exists():
            logger.error("Batch file not found: %s", args.batch)
            sys.exit(2)
        urls_list = [
            line.strip() for line in batch_file.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        from batch.executor import BatchExecutor
        results = BatchExecutor(cfg).run_urls(urls_list, args.objective)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        sys.exit(0)

    if args.pages:
        if not args.url or not args.objective:
            logger.error("--pages requires --url and --objective")
            sys.exit(2)
        from batch.executor import BatchExecutor
        result = BatchExecutor(cfg).run_pages(args.url, args.objective, n_pages=args.pages)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)

    # --search 가 있으면 url 없어도 허용
    if not args.search and not url:
        logger.error("--url or --search is required.")
        sys.exit(2)
    if not objective:
        logger.error("--objective is required.")
        sys.exit(2)

    if args.react:
        from react_agent.runner import ReactAgent

        agent = ReactAgent(cfg)
        result = agent.run({
            "url": url or "",
            "objective": objective,
            "no_cache": args.no_cache,
            "search_query": args.search or "",
        })
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        # Legacy pipeline (unchanged)
        agent = BaseAgent(cfg)
        state = agent.run({"url": url, "objective": objective})
        summary = {
            "run_id": state.get("run_id"),
            "success": state.get("success"),
            "history_record_id": state.get("history_record_id"),
            "retry_count": state.get("retry_count"),
            "critic_feedback": state.get("critic_feedback"),
            "plan_steps": state.get("plan_steps"),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Adapter: ReactAgent → SquadOne ResponseEnvelope."""

from __future__ import annotations

import time
from typing import Any

from app.common.contracts import (
    ToolDirective,
    ResponseEnvelope,
    build_success_envelope,
    build_error_envelope,
)

_MAX_EVIDENCE_ITEMS = 5


def run_scrape(directive: ToolDirective) -> ResponseEnvelope:
    """Execute ReactAgent scraping and wrap result in SquadOne ResponseEnvelope."""
    inp = directive.tool_input
    url          = str(inp.get("url") or "").strip()
    objective    = str(inp.get("objective") or "").strip()
    search_query = str(inp.get("search_query") or "").strip()
    no_cache     = bool(inp.get("no_cache", False))

    if not objective:
        return build_error_envelope(
            code="DATA_INPUT_INVALID",
            message="objective is required",
            retryable=False,
        )
    if url and not (url.startswith("http://") or url.startswith("https://")):
        return build_error_envelope(
            code="DATA_INPUT_INVALID",
            message="url must start with http:// or https://",
            retryable=False,
        )
    if not url and not search_query:
        return build_error_envelope(
            code="DATA_INPUT_INVALID",
            message="either url or search_query must be provided",
            retryable=False,
        )

    started = time.perf_counter()
    try:
        from react_agent.runner import ReactAgent
        from core.config import get_config
        agent = ReactAgent(get_config())
        agent_result = agent.run({
            "url": url,
            "objective": objective,
            "search_query": search_query,
            "no_cache": no_cache,
        })
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        msg = str(exc)
        if "timeout" in msg.lower():
            code, retryable = "TRANSIENT_TIMEOUT", True
        elif "connection" in msg.lower():
            code, retryable = "TRANSIENT_PROVIDER_UNAVAILABLE", True
        else:
            code, retryable = "TRANSIENT_PROVIDER_UNAVAILABLE", True
        return build_error_envelope(
            code=code, message=msg, retryable=retryable,
            timing={"elapsed_ms": elapsed_ms},
        )

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

    if not agent_result.get("success") or not agent_result.get("result"):
        return build_error_envelope(
            code="TRANSIENT_PROVIDER_UNAVAILABLE",
            message="ReactAgent returned no results",
            retryable=True,
            timing={"elapsed_ms": elapsed_ms},
        )

    inner = agent_result["result"]
    items = inner.get("items") or []
    result_payload: dict[str, Any] = {
        "run_id":     agent_result.get("run_id", ""),
        "success":    agent_result.get("success", False),
        "step_count": agent_result.get("step_count", 0),
        "cache_hit":  agent_result.get("cache_hit", False),
        "items":      items,
        "count":      inner.get("count", len(items)),
    }

    evidence = _build_evidence(items, url or search_query, objective)

    return build_success_envelope(
        result=result_payload,
        evidence=evidence,
        timing={"elapsed_ms": elapsed_ms},
        meta={
            "run_id":    agent_result.get("run_id", ""),
            "cache_hit": agent_result.get("cache_hit", False),
        },
    )


def _build_evidence(
    items: list[dict[str, Any]],
    source_hint: str,
    objective: str,
) -> list[dict[str, str]]:
    if not items:
        return [{
            "source":  source_hint or "web",
            "summary": f"No items extracted for: {objective[:200]}",
        }]
    evidence: list[dict[str, str]] = []
    for item in items[:_MAX_EVIDENCE_ITEMS]:
        src  = (item.get("url") or item.get("link") or source_hint or "scraper").strip()[:500]
        summ = (item.get("title") or item.get("name") or str(item))[:400]
        if src and summ:
            evidence.append({"source": src, "summary": summ})
    if not evidence:
        evidence = [{
            "source":  source_hint or "web",
            "summary": f"Extracted {len(items)} items",
        }]
    return evidence

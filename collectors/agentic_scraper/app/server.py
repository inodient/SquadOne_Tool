"""Entrypoint for the Agentic Scrapper MCP server."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from app.common.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from app.common.config import Settings, load_settings
from app.common.contracts import (
    attach_contract_meta,
    build_error_envelope_from_exception,
    parse_tool_directive,
    validate_response_envelope,
)
from app.common.errors import (
    CanonicalError,
    ERROR_ORIGIN_MCP_SERVER,
    invalid_request,
    is_retryable_code,
    map_exception,
    policy_violation,
    tool_not_found,
)
from app.common.idempotency import _store as _idem_store
from app.common.health import (
    HealthReport,
    adapter_check,
    aggregate_status,
    circuit_check,
    credential_check,
    health_report,
    manifest_check,
    rate_limit_snapshot,
)
from app.common.logging import get_logger
from app.common.rate_limit import TokenBucketRateLimiter
from app.tools.scrape import run_scrape

logger = get_logger(__name__)
settings = load_settings()

MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "tool_manifest.json")


def _build_rate_limiter(cfg: Settings) -> TokenBucketRateLimiter:
    return TokenBucketRateLimiter(
        global_capacity=cfg.rate_limit_global_burst,
        global_refill_per_second=cfg.rate_limit_global_qps,
        per_key_capacity=cfg.rate_limit_per_trace_burst,
        per_key_refill_per_second=cfg.rate_limit_per_trace_qps,
    )


def _build_circuit_breaker(cfg: Settings) -> CircuitBreaker:
    return CircuitBreaker(
        CircuitBreakerConfig(
            window_seconds=cfg.circuit_breaker_window_seconds,
            open_seconds=cfg.circuit_breaker_open_seconds,
            failure_rate_threshold=cfg.circuit_breaker_failure_threshold,
            minimum_samples=cfg.circuit_breaker_minimum_samples,
            half_open_probe=cfg.circuit_breaker_half_open_probe,
        )
    )


rate_limiter = _build_rate_limiter(settings)
circuit_breaker = _build_circuit_breaker(settings)


def load_tool_manifest() -> dict[str, Any]:
    with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError("tool manifest must be an object")
    return payload


def discover_tools() -> dict[str, Any]:
    """Expose tool registration payload used by SquadOne discover/list-tools."""
    manifest = load_tool_manifest()
    return {
        "service_name":     manifest.get("service_name", settings.service_name),
        "protocol_version": manifest.get("protocol_version", settings.protocol_version),
        "tools":            manifest.get("tools", []),
    }


def _fallback_context(meta: dict[str, Any]) -> tuple[int, list[str]]:
    try:
        depth = int(meta.get("fallback_depth", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise invalid_request("meta.fallback_depth must be an integer") from exc
    chain_raw = meta.get("fallback_chain")
    if not isinstance(chain_raw, list):
        return depth, []
    chain = [str(item).strip() for item in chain_raw if str(item).strip()]
    return depth, chain


def _validate_fallback_guard(directive: Any, max_depth: int) -> None:
    if directive is None:
        return
    depth, chain = _fallback_context(directive.meta)
    tool_name = directive.tool_name
    if depth < 0:
        raise invalid_request("meta.fallback_depth must be >= 0")
    if tool_name in chain or depth > max_depth:
        raise policy_violation("fallback depth exceeded or recursive fallback detected")


def _attach_fallback_meta(response: Any, directive: Any, max_depth: int) -> None:
    if directive is None or not directive.fallback_tool:
        return
    depth, chain = _fallback_context(directive.meta)
    response.meta.setdefault("fallback_tool", directive.fallback_tool)
    response.meta.setdefault("fallback_depth", depth)
    response.meta.setdefault("fallback_chain", chain)
    response.meta.setdefault("max_fallback_depth", max_depth)


def handle_tool_call(payload: dict[str, Any]) -> dict[str, Any]:
    started_at = time.perf_counter()
    directive = None
    rate_key: str | None = None
    error_origin: str | None = None
    retry_scope: str | None = None

    try:
        directive = parse_tool_directive(
            payload,
            expected_tool_name=None,
            expected_protocol_version=settings.protocol_version,
        )
        _validate_fallback_guard(directive, settings.max_fallback_depth)

        idem_key = directive.idempotency_key
        is_dup, cached_response = _idem_store.check_and_record(idem_key, {})
        if is_dup:
            logger.info(
                "idempotency_event",
                extra={
                    "trace_id": directive.trace_id,
                    "task_id": directive.task_id,
                    "idempotency_key": idem_key,
                    "status": "duplicate_detected",
                },
            )
            if cached_response:
                return cached_response

        rate_key = directive.trace_id
        rate_limiter.acquire(rate_key)
        circuit_breaker.before_call()

        tool_name = directive.tool_name
        if tool_name == "scrape":
            response = run_scrape(directive)
        else:
            raise tool_not_found(f"tool {tool_name!r} is not registered on this server")

        circuit_breaker.record_success()
    except CanonicalError as exc:
        error_origin = exc.origin
        retry_scope = "transient" if is_retryable_code(exc.code) else "permanent"
        if is_retryable_code(exc.code):
            circuit_breaker.record_failure()
        response = build_error_envelope_from_exception(exc)
    except Exception as exc:  # pragma: no cover
        logger.exception("unhandled tool call error")
        error_origin = ERROR_ORIGIN_MCP_SERVER
        retry_scope = "transient"
        circuit_breaker.record_failure()
        response = build_error_envelope_from_exception(map_exception(exc))

    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
    response.timing["elapsed_ms"] = elapsed_ms
    attach_contract_meta(response, directive, protocol_version=settings.protocol_version)
    if directive is not None:
        response.meta.setdefault("evidence_contract_applied", bool(directive.evidence_schema))
        response.meta.setdefault("evidence_contract_fields", sorted(directive.evidence_schema.keys()))
    _attach_fallback_meta(response, directive, settings.max_fallback_depth)
    validate_response_envelope(response)

    error_payload = response.error
    error_code = error_payload.code if error_payload is not None else ""
    idem_key = directive.idempotency_key if directive is not None else ""

    logger.info(
        "tool_call_completed",
        extra={
            "trace_id":     response.meta.get("trace_id", ""),
            "task_id":      response.meta.get("task_id", ""),
            "request_id":   response.meta.get("request_id", ""),
            "tool_name":    response.meta.get("tool_name", settings.tool_name),
            "status":       response.status,
            "error_code":   error_code,
            "error_origin": error_origin or "",
            "retry_scope":  retry_scope or "",
            "elapsed_ms":   elapsed_ms,
        },
    )

    result_dict = response.to_dict()
    if idem_key:
        _idem_store.update(idem_key, result_dict)
    return result_dict


def health_check(adapter_probe: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Liveness probe."""
    checks = [
        adapter_check(adapter_probe),
        credential_check(settings.required_provider_credential_env_vars()),
    ]
    report = health_report(
        checks,
        extra={
            "service_name":     settings.service_name,
            "protocol_version": settings.protocol_version,
        },
    )
    return report.to_dict()


def readiness_check(adapter_probe: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Readiness probe: liveness + manifest + circuit-breaker."""
    checks = [
        manifest_check(MANIFEST_PATH, expected_tool_name="scrape"),
        adapter_check(adapter_probe),
        credential_check(settings.required_provider_credential_env_vars()),
        circuit_check(circuit_breaker),
    ]
    report = HealthReport(
        status=aggregate_status(checks),
        checks=checks,
        extra={
            "service_name":     settings.service_name,
            "protocol_version": settings.protocol_version,
            "rate_limit":       rate_limit_snapshot(rate_limiter),
        },
    )
    return report.to_dict()

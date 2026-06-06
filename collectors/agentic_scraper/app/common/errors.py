"""Canonical error classes and mappings for SquadOne MCP contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TRANSIENT_TIMEOUT = "TRANSIENT_TIMEOUT"
TRANSIENT_PROVIDER_UNAVAILABLE = "TRANSIENT_PROVIDER_UNAVAILABLE"
TRANSIENT_RATE_LIMIT = "TRANSIENT_RATE_LIMIT"
TRANSIENT_EXECUTION_ERROR = "TRANSIENT_EXECUTION_ERROR"
DATA_INPUT_INVALID = "DATA_INPUT_INVALID"
DATA_OUTPUT_SCHEMA_MISMATCH = "DATA_OUTPUT_SCHEMA_MISMATCH"
PLAN_TOOL_NOT_FOUND = "PLAN_TOOL_NOT_FOUND"
POLICY_APPROVAL_REQUIRED = "POLICY_APPROVAL_REQUIRED"
ERROR_ORIGIN_MCP_SERVER = "mcp_server"


@dataclass
class CanonicalError(Exception):
    """Domain error as Exception; avoid slots=True (breaks BaseException init on Py3.13+)."""

    code: str
    message: str
    origin: str = ERROR_ORIGIN_MCP_SERVER
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message or self.code)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def is_retryable_code(code: str) -> bool:
    return isinstance(code, str) and code.upper().startswith("TRANSIENT_")


def canonical_error(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> CanonicalError:
    return CanonicalError(
        code=code,
        message=message,
        retryable=is_retryable_code(code),
        details=details or {},
    )


def invalid_request(message: str) -> CanonicalError:
    return canonical_error(DATA_INPUT_INVALID, message)


def output_schema_mismatch(message: str) -> CanonicalError:
    return canonical_error(DATA_OUTPUT_SCHEMA_MISMATCH, message)


def idempotency_conflict(expected: str, actual: str) -> CanonicalError:
    return canonical_error(
        DATA_INPUT_INVALID,
        "idempotency_key does not match trace_id/task_id/attempt",
        details={"expected_idempotency_key": expected, "actual_idempotency_key": actual},
    )


def transient_upstream(message: str) -> CanonicalError:
    return canonical_error(TRANSIENT_PROVIDER_UNAVAILABLE, message)


def transient_timeout(message: str) -> CanonicalError:
    return canonical_error(TRANSIENT_TIMEOUT, message)


def transient_rate_limit(message: str) -> CanonicalError:
    return canonical_error(TRANSIENT_RATE_LIMIT, message)


def policy_violation(message: str) -> CanonicalError:
    return canonical_error(POLICY_APPROVAL_REQUIRED, message)


def tool_not_found(message: str) -> CanonicalError:
    return canonical_error(PLAN_TOOL_NOT_FOUND, message)


def map_exception(exc: BaseException) -> CanonicalError:
    if isinstance(exc, CanonicalError):
        return exc
    if isinstance(exc, TimeoutError):
        return transient_timeout(str(exc) or "operation timed out")
    if isinstance(exc, ConnectionError):
        return transient_upstream(str(exc) or "upstream connection failed")

    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if "timeout" in name or "timeout" in message:
        return transient_timeout(str(exc) or "operation timed out")
    if "connection" in name or "unreachable" in message or "dns" in message:
        return transient_upstream(str(exc) or "upstream connection failed")
    if "schema" in message or "json" in message or "parse" in message:
        return output_schema_mismatch(str(exc) or "response schema mismatch")
    return canonical_error(TRANSIENT_EXECUTION_ERROR, str(exc) or type(exc).__name__)

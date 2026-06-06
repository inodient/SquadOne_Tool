"""Health and readiness checks for the LLM Search server."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .circuit_breaker import CircuitBreaker, CircuitState
from .rate_limit import TokenBucketRateLimiter


@dataclass(slots=True)
class CheckResult:
    name: str
    healthy: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "healthy": self.healthy, "detail": self.detail}


@dataclass(slots=True)
class HealthReport:
    status: str
    checks: list[CheckResult] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
            **({"extra": self.extra} if self.extra else {}),
        }


def manifest_check(manifest_path: str, *, expected_tool_name: str) -> CheckResult:
    if not os.path.isfile(manifest_path):
        return CheckResult(
            name="tool_manifest",
            healthy=False,
            detail=f"manifest not found at {manifest_path}",
        )
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        return CheckResult(
            name="tool_manifest",
            healthy=False,
            detail=f"manifest unreadable: {exc}",
        )

    tools = manifest.get("tools") or []
    names = [str(tool.get("name") or "") for tool in tools if isinstance(tool, dict)]
    if expected_tool_name not in names:
        return CheckResult(
            name="tool_manifest",
            healthy=False,
            detail=f"tool {expected_tool_name!r} missing from manifest",
        )
    return CheckResult(name="tool_manifest", healthy=True, detail="loaded")


def adapter_check(adapter_probe: Callable[[], bool] | None) -> CheckResult:
    if adapter_probe is None:
        return CheckResult(
            name="llm_adapter",
            healthy=True,
            detail="probe not configured; assumed healthy",
        )
    try:
        ok = bool(adapter_probe())
    except Exception as exc:  # noqa: BLE001 - probe is best-effort.
        return CheckResult(name="llm_adapter", healthy=False, detail=str(exc))
    return CheckResult(
        name="llm_adapter",
        healthy=ok,
        detail="probe ok" if ok else "probe failed",
    )


def credential_check(required_env_vars: Iterable[str]) -> CheckResult:
    required = [name for name in required_env_vars if name]
    if not required:
        return CheckResult(
            name="credentials",
            healthy=True,
            detail="no credentials required",
        )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        return CheckResult(
            name="credentials",
            healthy=False,
            detail=f"missing env vars: {', '.join(missing)}",
        )
    return CheckResult(name="credentials", healthy=True, detail="present")


def circuit_check(breaker: CircuitBreaker) -> CheckResult:
    snapshot = breaker.snapshot()
    healthy = snapshot.state != CircuitState.OPEN.value
    detail = (
        f"state={snapshot.state}; failures={snapshot.failures}; "
        f"successes={snapshot.successes}"
    )
    if not healthy:
        detail += f"; retry_after={snapshot.open_remaining_seconds}s"
    return CheckResult(name="circuit_breaker", healthy=healthy, detail=detail)


def rate_limit_snapshot(limiter: TokenBucketRateLimiter) -> dict[str, Any]:
    return limiter.snapshot()


def aggregate_status(checks: list[CheckResult]) -> str:
    return "ok" if all(check.healthy for check in checks) else "unhealthy"


def health_report(checks: list[CheckResult], *, extra: dict[str, Any] | None = None) -> HealthReport:
    return HealthReport(
        status=aggregate_status(checks),
        checks=checks,
        extra=extra or {},
    )

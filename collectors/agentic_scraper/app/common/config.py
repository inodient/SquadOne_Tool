"""Configuration for the Agentic Scrapper MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    service_name: str = "tool-suite-agentic-scrapper"
    tool_name: str = "scrape"
    protocol_version: str = "1.0.0"
    request_timeout_seconds: float = 150.0
    max_fallback_depth: int = 0
    rate_limit_global_qps: float = 2.0
    rate_limit_global_burst: float = 4.0
    rate_limit_per_trace_qps: float = 1.0
    rate_limit_per_trace_burst: float = 2.0
    circuit_breaker_window_seconds: float = 60.0
    circuit_breaker_open_seconds: float = 120.0
    circuit_breaker_failure_threshold: float = 0.5
    circuit_breaker_minimum_samples: int = 3
    circuit_breaker_half_open_probe: int = 1

    def required_provider_credential_env_vars(self) -> tuple[str, ...]:
        return ()


def load_settings() -> Settings:
    _load_dotenv_if_present()
    return Settings(
        service_name=os.getenv("SERVICE_NAME", "tool-suite-agentic-scrapper"),
        tool_name=os.getenv("TOOL_NAME", "scrape"),
        protocol_version=os.getenv("PROTOCOL_VERSION", "1.0.0"),
        request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "150")),
        max_fallback_depth=int(os.getenv("MAX_FALLBACK_DEPTH", "0")),
        rate_limit_global_qps=float(os.getenv("RATE_LIMIT_GLOBAL_QPS", "2")),
        rate_limit_global_burst=float(os.getenv("RATE_LIMIT_GLOBAL_BURST", "4")),
        rate_limit_per_trace_qps=float(os.getenv("RATE_LIMIT_PER_TRACE_QPS", "1")),
        rate_limit_per_trace_burst=float(os.getenv("RATE_LIMIT_PER_TRACE_BURST", "2")),
        circuit_breaker_window_seconds=float(os.getenv("CIRCUIT_BREAKER_WINDOW_SECONDS", "60")),
        circuit_breaker_open_seconds=float(os.getenv("CIRCUIT_BREAKER_OPEN_SECONDS", "120")),
        circuit_breaker_failure_threshold=float(
            os.getenv("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "0.5")
        ),
        circuit_breaker_minimum_samples=int(
            os.getenv("CIRCUIT_BREAKER_MINIMUM_SAMPLES", "3")
        ),
        circuit_breaker_half_open_probe=int(
            os.getenv("CIRCUIT_BREAKER_HALF_OPEN_PROBE", "1")
        ),
    )


def _load_dotenv_if_present() -> None:
    dotenv_path = Path.cwd() / ".env"
    if not dotenv_path.is_file():
        return
    try:
        lines = dotenv_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env_key = key.strip()
        if not env_key:
            continue
        env_value = value.strip()
        if len(env_value) >= 2 and env_value[0] == env_value[-1] and env_value[0] in {"'", '"'}:
            env_value = env_value[1:-1]
        os.environ.setdefault(env_key, env_value)

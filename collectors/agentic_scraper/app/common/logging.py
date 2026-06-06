"""Structured logger setup."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for field_name in ("trace_id", "task_id", "request_id"):
            value = getattr(record, field_name, None)
            if value:
                payload[field_name] = value

        for field_name in (
            "method",
            "path",
            "client_ip",
            "status_code",
            "result_status",
            "error_code",
            "elapsed_ms",
            "tool_name",
            "status",
            "provider_requested",
            "provider_used",
            "idempotency_key",
            "error_origin",
            "retry_scope",
            "protocol_version",
            "provider_latency_ms",
        ):
            value = getattr(record, field_name, None)
            if value is not None and value != "":
                payload[field_name] = value

        return json.dumps(payload, ensure_ascii=True)


def get_logger(name: str = "llm_search") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger

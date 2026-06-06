"""Idempotency key generation and validation."""

from __future__ import annotations

import threading
import time as _time
from typing import Any

from .errors import idempotency_conflict


def build_idempotency_key(directive_or_trace_id: Any, task_id: str | None = None, attempt: int | None = None) -> str:
    """Build the SquadOne idempotency key: trace_id-task_id-attempt."""
    if task_id is None and attempt is None and hasattr(directive_or_trace_id, "trace_id"):
        trace_id = str(getattr(directive_or_trace_id, "trace_id") or "na")
        task_id = str(getattr(directive_or_trace_id, "task_id") or "na")
        attempt = int(getattr(directive_or_trace_id, "attempt"))
    else:
        trace_id = str(directive_or_trace_id or "na")
        task_id = str(task_id or "na")
        attempt = int(attempt if attempt is not None else 1)
    return f"{trace_id}-{task_id}-{attempt}"


def idempotency_key_matches(directive: Any, key: str) -> bool:
    return build_idempotency_key(directive) == str(key or "")


def validate_idempotency_key(directive: Any, key: str) -> None:
    expected = build_idempotency_key(directive)
    actual = str(key or "")
    if expected != actual:
        raise idempotency_conflict(expected, actual)


_IDEMPOTENCY_TTL_SECONDS = 300


class IdempotencyStore:
    """Thread-safe in-memory idempotency cache with TTL eviction."""

    def __init__(self, ttl_seconds: float = _IDEMPOTENCY_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, dict[str, Any]]] = {}
        self._ttl = ttl_seconds

    def check_and_record(self, key: str, response: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        """Return (is_duplicate, cached_response).

        First call with a key records it and returns (False, None).
        Subsequent calls within TTL return (True, cached_response).
        """
        now = _time.monotonic()
        with self._lock:
            self._evict(now)
            if key in self._store:
                return True, self._store[key][1]
            self._store[key] = (now + self._ttl, response)
            return False, None

    def update(self, key: str, response: dict[str, Any]) -> None:
        """Replace the cached response for an already-recorded key."""
        now = _time.monotonic()
        with self._lock:
            if key in self._store:
                expires_at = self._store[key][0]
                self._store[key] = (expires_at, response)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def _evict(self, now: float) -> None:
        expired = [k for k, (exp, _) in self._store.items() if exp <= now]
        for k in expired:
            del self._store[k]


_store = IdempotencyStore()

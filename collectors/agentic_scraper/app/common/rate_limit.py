"""Token-bucket rate limiter with global and per-key scopes."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .errors import transient_rate_limit


@dataclass(slots=True)
class _Bucket:
    capacity: float
    refill_per_second: float
    tokens: float
    last_refill: float


class TokenBucketRateLimiter:
    """Thread-safe token bucket rate limiter.

    Two scopes are supported:
    - Global QPS limit at the server level.
    - Per-key bucket (typically keyed by trace_id) to bound any single trace.
    """

    def __init__(
        self,
        *,
        global_capacity: float,
        global_refill_per_second: float,
        per_key_capacity: float,
        per_key_refill_per_second: float,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if global_capacity <= 0 or per_key_capacity <= 0:
            raise ValueError("rate limit capacity must be > 0")
        if global_refill_per_second <= 0 or per_key_refill_per_second <= 0:
            raise ValueError("rate limit refill must be > 0")

        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._global = _Bucket(
            capacity=global_capacity,
            refill_per_second=global_refill_per_second,
            tokens=global_capacity,
            last_refill=self._clock(),
        )
        self._per_key_capacity = per_key_capacity
        self._per_key_refill = per_key_refill_per_second
        self._buckets: dict[str, _Bucket] = {}

    def acquire(self, key: str | None = None, *, cost: float = 1.0) -> None:
        if cost <= 0:
            return
        with self._lock:
            now = self._clock()
            if not self._consume(self._global, now, cost):
                raise transient_rate_limit(
                    "global QPS limit exceeded; please retry with backoff"
                )
            if key:
                bucket = self._buckets.get(key)
                if bucket is None:
                    bucket = _Bucket(
                        capacity=self._per_key_capacity,
                        refill_per_second=self._per_key_refill,
                        tokens=self._per_key_capacity,
                        last_refill=now,
                    )
                    self._buckets[key] = bucket
                if not self._consume(bucket, now, cost):
                    raise transient_rate_limit(
                        f"per-key rate limit exceeded for key={key!r}"
                    )

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            now = self._clock()
            self._refill(self._global, now)
            return {
                "global_tokens": round(self._global.tokens, 4),
                "global_capacity": self._global.capacity,
                "global_refill_per_second": self._global.refill_per_second,
                "tracked_keys": float(len(self._buckets)),
            }

    def _consume(self, bucket: _Bucket, now: float, cost: float) -> bool:
        self._refill(bucket, now)
        if bucket.tokens + 1e-9 < cost:
            return False
        bucket.tokens -= cost
        return True

    @staticmethod
    def _refill(bucket: _Bucket, now: float) -> None:
        elapsed = max(0.0, now - bucket.last_refill)
        if elapsed <= 0:
            return
        bucket.tokens = min(
            bucket.capacity,
            bucket.tokens + elapsed * bucket.refill_per_second,
        )
        bucket.last_refill = now

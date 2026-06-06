"""Circuit breaker with closed/open/half-open states."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Deque

from .errors import canonical_error, TRANSIENT_PROVIDER_UNAVAILABLE

CIRCUIT_OPEN_MESSAGE = "circuit breaker is open; upstream marked unavailable"


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class _Sample:
    ts: float
    success: bool


@dataclass(slots=True)
class CircuitBreakerConfig:
    window_seconds: float = 30.0
    open_seconds: float = 60.0
    failure_rate_threshold: float = 0.5
    minimum_samples: int = 5
    half_open_probe: int = 1


@dataclass(slots=True)
class CircuitBreakerSnapshot:
    state: str
    failures: int
    successes: int
    open_remaining_seconds: float = 0.0
    samples: int = 0


class CircuitBreaker:
    """Failure-rate based circuit breaker.

    - Window: trailing `window_seconds` of outcomes is evaluated.
    - Open: when failure ratio >= `failure_rate_threshold` and `minimum_samples`
      observed; calls reject for `open_seconds` with a transient canonical error.
    - Half-open: after the open period, a small number of probe calls is allowed.
      A success closes the circuit; a failure reopens it.
    """

    def __init__(
        self,
        config: CircuitBreakerConfig | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config or CircuitBreakerConfig()
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._samples: Deque[_Sample] = deque()
        self._state = CircuitState.CLOSED
        self._opened_at: float | None = None
        self._half_open_in_flight = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_transition_to_half_open(self._clock())
            return self._state

    def before_call(self) -> None:
        with self._lock:
            now = self._clock()
            self._maybe_transition_to_half_open(now)
            if self._state is CircuitState.OPEN:
                remaining = self._open_remaining(now)
                raise canonical_error(
                    TRANSIENT_PROVIDER_UNAVAILABLE,
                    f"{CIRCUIT_OPEN_MESSAGE}; retry_after_seconds={remaining:.2f}",
                    details={"retry_after_seconds": round(remaining, 3)},
                )
            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_in_flight >= self._config.half_open_probe:
                    raise canonical_error(
                        TRANSIENT_PROVIDER_UNAVAILABLE,
                        "circuit breaker is half-open; probe in progress",
                    )
                self._half_open_in_flight += 1

    def record_success(self) -> None:
        with self._lock:
            now = self._clock()
            self._samples.append(_Sample(ts=now, success=True))
            self._evict_old(now)
            if self._state is CircuitState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._state = CircuitState.CLOSED
                self._opened_at = None
                self._samples.clear()

    def record_failure(self) -> None:
        with self._lock:
            now = self._clock()
            self._samples.append(_Sample(ts=now, success=False))
            self._evict_old(now)
            if self._state is CircuitState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._open(now)
                return
            self._maybe_open(now)

    def snapshot(self) -> CircuitBreakerSnapshot:
        with self._lock:
            now = self._clock()
            self._evict_old(now)
            self._maybe_transition_to_half_open(now)
            failures = sum(1 for s in self._samples if not s.success)
            successes = len(self._samples) - failures
            return CircuitBreakerSnapshot(
                state=self._state.value,
                failures=failures,
                successes=successes,
                open_remaining_seconds=round(self._open_remaining(now), 3),
                samples=len(self._samples),
            )

    def _maybe_open(self, now: float) -> None:
        if len(self._samples) < self._config.minimum_samples:
            return
        failures = sum(1 for s in self._samples if not s.success)
        ratio = failures / len(self._samples)
        if ratio >= self._config.failure_rate_threshold:
            self._open(now)

    def _open(self, now: float) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = now
        self._half_open_in_flight = 0

    def _maybe_transition_to_half_open(self, now: float) -> None:
        if self._state is not CircuitState.OPEN or self._opened_at is None:
            return
        if now - self._opened_at >= self._config.open_seconds:
            self._state = CircuitState.HALF_OPEN
            self._half_open_in_flight = 0

    def _open_remaining(self, now: float) -> float:
        if self._state is not CircuitState.OPEN or self._opened_at is None:
            return 0.0
        remaining = self._config.open_seconds - (now - self._opened_at)
        return max(0.0, remaining)

    def _evict_old(self, now: float) -> None:
        cutoff = now - self._config.window_seconds
        while self._samples and self._samples[0].ts < cutoff:
            self._samples.popleft()

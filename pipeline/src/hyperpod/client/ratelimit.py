# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Weight-aware token-bucket rate limiter and backoff policy for Hyperliquid APIs (§6.1)."""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

_BASE_WEIGHTS: dict[str, int] = {
    "userRole": 60,
    "clearinghouseState": 2,
    "spotClearinghouseState": 2,
    "userAbstraction": 2,
    "subAccounts": 20,
    "portfolio": 20,
    "userFillsByTime": 20,
    "userFunding": 20,
    "candleSnapshot": 20,
    "meta": 20,
    "metaAndAssetCtxs": 20,
}

_WINDOW_SECONDS = 60.0
_WINDOW_EPS = 1e-9


def base_weight_for_endpoint(endpoint_type: str) -> int:
    """Return the pre-request Hyperliquid Info API weight for an endpoint type."""
    return _BASE_WEIGHTS.get(endpoint_type, 20)


def compute_dynamic_extra_weight(endpoint_type: str, item_count: int) -> int:
    """Compute post-response dynamic weight surcharge (§6.1)."""
    if item_count <= 0:
        return 0
    if endpoint_type.startswith("userFills") or endpoint_type == "userFunding":
        return math.ceil(item_count / 20)
    if endpoint_type == "candleSnapshot":
        return math.ceil(item_count / 60)
    return 0


@dataclass
class RateLimitStats:
    """Telemetry collected during a client run for runs.jsonl and diagnostics."""

    started_at: float
    total_requests: int = 0
    total_weight: int = 0
    base_weight_total: int = 0
    extra_weight_total: int = 0
    weight_by_endpoint: dict[str, int] = field(default_factory=dict)
    requests_by_endpoint: dict[str, int] = field(default_factory=dict)
    retries_total: int = 0
    http_429_count: int = 0
    http_5xx_count: int = 0
    transport_error_count: int = 0
    halve_events: int = 0
    total_sleep_seconds: float = 0.0

    def to_dict(self, *, clock: Callable[[], float] = time.monotonic) -> dict[str, object]:
        """Serialize stats to camelCase JSON structure suitable for runs.jsonl."""
        elapsed = max(0.0, clock() - self.started_at)
        return {
            "totalRequests": self.total_requests,
            "totalWeight": self.total_weight,
            "baseWeightTotal": self.base_weight_total,
            "extraWeightTotal": self.extra_weight_total,
            "weightByEndpoint": dict(sorted(self.weight_by_endpoint.items())),
            "requestsByEndpoint": dict(sorted(self.requests_by_endpoint.items())),
            "retriesTotal": self.retries_total,
            "http429Count": self.http_429_count,
            "http5xxCount": self.http_5xx_count,
            "transportErrorCount": self.transport_error_count,
            "halveEvents": self.halve_events,
            "totalSleepSeconds": round(self.total_sleep_seconds, 6),
            "elapsedSeconds": round(elapsed, 6),
        }


class WeightRateLimiter:
    """Async weight-aware token bucket + 60s rolling-window rate limiter (§6.1).

    Features:
    - Enforces per-minute weight budget using both token bucket refill and a 60s rolling window.
    - Supports post-response weight surcharges (which may create temporary token debt).
    - Computes exponential backoff (`2^k + jitter`) for 429/5xx/transport retries.
    - Halves effective per-minute budget for `halve_cooldown_seconds` on consecutive HTTP 429s.
    """

    def __init__(
        self,
        *,
        budget_per_min: int = 800,
        max_retries: int = 6,
        halve_cooldown_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        if budget_per_min < 1:
            raise ValueError("budget_per_min must be >= 1")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if halve_cooldown_seconds <= 0:
            raise ValueError("halve_cooldown_seconds must be > 0")

        self.budget_per_min = budget_per_min
        self.max_retries = max_retries
        self.halve_cooldown_seconds = float(halve_cooldown_seconds)
        self._clock = clock
        self._sleep = sleep
        self._rng = rng

        now = self._clock()
        self._tokens: float = float(budget_per_min)
        self._last_refill: float = now
        self._halved_until: float = float("-inf")
        self._consecutive_429s: int = 0
        self._window: deque[tuple[float, int]] = deque()
        self._stats = RateLimitStats(started_at=now)

        self._acquire_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()

    @property
    def stats(self) -> RateLimitStats:
        """Return live telemetry statistics."""
        return self._stats

    def is_halved(self, now: float | None = None) -> bool:
        """Return True while the 429 consecutive-failure cooldown is active."""
        ts = self._clock() if now is None else now
        return ts < self._halved_until - _WINDOW_EPS

    def effective_budget_per_min(self, now: float | None = None) -> int:
        """Return active per-minute weight budget (halved during 429 cooldown)."""
        if self.is_halved(now):
            return max(1, self.budget_per_min // 2)
        return self.budget_per_min

    def compute_backoff_delay(self, attempt: int) -> float:
        """Return `2^attempt + uniform(0, 1)` jittered backoff delay in seconds."""
        if attempt < 0:
            raise ValueError("attempt must be >= 0")
        jitter = max(0.0, min(self._rng(), 1.0 - 1e-12))
        return float(2**attempt) + jitter

    def _prune_and_refill_locked(self, now: float) -> int:
        while self._window and (now - self._window[0][0]) >= (_WINDOW_SECONDS - _WINDOW_EPS):
            self._window.popleft()

        eff_budget = self.effective_budget_per_min(now)
        dt = max(0.0, now - self._last_refill)
        if dt > 0.0:
            refill = dt * (eff_budget / _WINDOW_SECONDS)
            self._tokens = min(float(eff_budget), self._tokens + refill)
            self._last_refill = now
        else:
            self._tokens = min(float(eff_budget), self._tokens)
        return eff_budget

    def _compute_wait_seconds_locked(self, base_weight: int, now: float, eff_budget: int) -> float:
        rate_per_sec = eff_budget / _WINDOW_SECONDS
        rolling_weight = sum(w for _, w in self._window)

        if base_weight > eff_budget:
            # Avoid deadlock if a single endpoint weight exceeds configured/halved budget:
            # wait until the bucket is full and no rolling debt remains.
            token_wait = max(0.0, float(eff_budget) - self._tokens) / rate_per_sec
            if rolling_weight > 0 and self._window:
                window_wait = max(0.0, (self._window[-1][0] + _WINDOW_SECONDS) - now)
            else:
                window_wait = 0.0
            wait_s = max(token_wait, window_wait)
        else:
            token_deficit = max(0.0, float(base_weight) - self._tokens)
            token_wait = token_deficit / rate_per_sec

            window_wait = 0.0
            excess = (rolling_weight + base_weight) - eff_budget
            if excess > 0:
                freed = 0
                for ts, w in self._window:
                    freed += w
                    if freed >= excess:
                        window_wait = max(0.0, (ts + _WINDOW_SECONDS) - now)
                        if window_wait == 0.0:
                            window_wait = _WINDOW_EPS * 2.0
                        break
            wait_s = max(token_wait, window_wait)

        if self.is_halved(now) and wait_s > 0.0 and (now + wait_s) > self._halved_until:
            wait_s = max(0.0, self._halved_until - now)
            if wait_s == 0.0:
                wait_s = _WINDOW_EPS * 2.0
        return wait_s

    async def acquire(self, base_weight: int, endpoint: str = "unknown") -> None:
        """Reserve `base_weight` before sending a request, waiting as required."""
        if base_weight < 0:
            raise ValueError("base_weight must be >= 0")

        async with self._acquire_lock:
            while True:
                async with self._state_lock:
                    now = self._clock()
                    eff_budget = self._prune_and_refill_locked(now)
                    wait_s = self._compute_wait_seconds_locked(base_weight, now, eff_budget)
                    if wait_s <= 0.0:
                        self._tokens -= float(base_weight)
                        if base_weight > 0:
                            self._window.append((now, base_weight))
                        self._stats.total_requests += 1
                        self._stats.base_weight_total += base_weight
                        self._stats.total_weight += base_weight
                        self._stats.requests_by_endpoint[endpoint] = (
                            self._stats.requests_by_endpoint.get(endpoint, 0) + 1
                        )
                        self._stats.weight_by_endpoint[endpoint] = (
                            self._stats.weight_by_endpoint.get(endpoint, 0) + base_weight
                        )
                        return

                await self._sleep(wait_s)
                async with self._state_lock:
                    self._stats.total_sleep_seconds += wait_s

    async def adjust(self, extra_weight: int, endpoint: str = "unknown") -> None:
        """Apply post-response dynamic weight surcharge (`ceil(n/20)` or `ceil(n/60)`)."""
        if extra_weight < 0:
            raise ValueError("extra_weight must be >= 0")
        if extra_weight == 0:
            return

        async with self._state_lock:
            now = self._clock()
            self._prune_and_refill_locked(now)
            self._tokens -= float(extra_weight)
            self._window.append((now, extra_weight))
            self._stats.extra_weight_total += extra_weight
            self._stats.total_weight += extra_weight
            self._stats.weight_by_endpoint[endpoint] = (
                self._stats.weight_by_endpoint.get(endpoint, 0) + extra_weight
            )

    async def record_response(self, status_code: int) -> None:
        """Update consecutive-429 and error counters based on an HTTP status code."""
        async with self._state_lock:
            self._record_response_locked(status_code)

    async def record_transport_error(self) -> None:
        """Record a terminal transport failure and reset consecutive-429 streak."""
        async with self._state_lock:
            self._stats.transport_error_count += 1
            self._consecutive_429s = 0

    def _record_response_locked(self, status_code: int) -> None:
        now = self._clock()
        if status_code == 429:
            self._stats.http_429_count += 1
            self._consecutive_429s += 1
            if self._consecutive_429s >= 2:
                was_halved = self.is_halved(now)
                self._halved_until = now + self.halve_cooldown_seconds
                halved_budget = self.effective_budget_per_min(now)
                self._tokens = min(self._tokens, float(halved_budget))
                if not was_halved:
                    self._stats.halve_events += 1
        elif 500 <= status_code < 600:
            self._stats.http_5xx_count += 1
            self._consecutive_429s = 0
        else:
            self._consecutive_429s = 0

    async def wait_for_retry(
        self,
        attempt: int,
        *,
        status_code: int | None = None,
        is_transport_error: bool = False,
    ) -> float:
        """Record retry cause and sleep exponential backoff `2^attempt + jitter`.

        Callers should invoke this instead of `record_response` when scheduling a retry
        to avoid double-counting HTTP 429/5xx statuses.
        """
        async with self._state_lock:
            if status_code is not None:
                self._record_response_locked(status_code)
            elif is_transport_error:
                self._stats.transport_error_count += 1
                self._consecutive_429s = 0
            self._stats.retries_total += 1
            delay = self.compute_backoff_delay(attempt)

        await self._sleep(delay)
        async with self._state_lock:
            self._stats.total_sleep_seconds += delay
        return delay

    def stats_dict(self) -> dict[str, object]:
        """Return serialized rate-limiter telemetry using this limiter's clock."""
        return self._stats.to_dict(clock=self._clock)

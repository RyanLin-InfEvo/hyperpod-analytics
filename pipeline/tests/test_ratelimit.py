# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for weight-aware rate limiter, dynamic surcharge, and 429 halving (§6.1)."""

from __future__ import annotations

import asyncio

import pytest

from hyperpod.client.ratelimit import (
    WeightRateLimiter,
    base_weight_for_endpoint,
    compute_dynamic_extra_weight,
)


def test_endpoint_weights_and_dynamic_surcharges() -> None:
    assert base_weight_for_endpoint("userRole") == 60
    assert base_weight_for_endpoint("clearinghouseState") == 2
    assert base_weight_for_endpoint("spotClearinghouseState") == 2
    assert base_weight_for_endpoint("userAbstraction") == 2
    assert base_weight_for_endpoint("subAccounts") == 20
    assert base_weight_for_endpoint("portfolio") == 20
    assert base_weight_for_endpoint("userFillsByTime") == 20
    assert base_weight_for_endpoint("userFunding") == 20
    assert base_weight_for_endpoint("candleSnapshot") == 20
    assert base_weight_for_endpoint("metaAndAssetCtxs") == 20
    assert base_weight_for_endpoint("unknownEndpoint") == 20

    assert compute_dynamic_extra_weight("userFillsByTime", 0) == 0
    assert compute_dynamic_extra_weight("userFillsByTime", -5) == 0
    assert compute_dynamic_extra_weight("userFillsByTime", 1) == 1
    assert compute_dynamic_extra_weight("userFillsByTime", 20) == 1
    assert compute_dynamic_extra_weight("userFillsByTime", 21) == 2
    assert compute_dynamic_extra_weight("userFillsByTime", 2000) == 100
    assert compute_dynamic_extra_weight("userFunding", 500) == 25
    assert compute_dynamic_extra_weight("candleSnapshot", 1) == 1
    assert compute_dynamic_extra_weight("candleSnapshot", 60) == 1
    assert compute_dynamic_extra_weight("candleSnapshot", 61) == 2
    assert compute_dynamic_extra_weight("clearinghouseState", 500) == 0


def test_rate_limiter_validation_and_simulated_budget_enforcement() -> None:
    with pytest.raises(ValueError, match="budget_per_min"):
        WeightRateLimiter(budget_per_min=0)
    with pytest.raises(ValueError, match="max_retries"):
        WeightRateLimiter(max_retries=-1)
    with pytest.raises(ValueError, match="halve_cooldown_seconds"):
        WeightRateLimiter(halve_cooldown_seconds=0)

    async def scenario(budget: int) -> None:
        now = 1000.0
        events: list[tuple[float, int]] = []

        async def sim_sleep(dt: float) -> None:
            nonlocal now
            assert dt > 0.0
            now += dt

        limiter = WeightRateLimiter(
            budget_per_min=budget,
            clock=lambda: now,
            sleep=sim_sleep,
        )

        # Send 100 requests of weight 20 (2,000 total weight > single-minute budget).
        for _ in range(100):
            await limiter.acquire(20, endpoint="portfolio")
            events.append((now, 20))

        # Verify no 60-second rolling window ever exceeds `budget`.
        for idx, (ts, _) in enumerate(events):
            window_sum = sum(w for prev_ts, w in events[: idx + 1] if (ts - prev_ts) < 60.0 - 1e-9)
            assert window_sum <= budget, f"exceeded budget at idx={idx}, ts={ts}: {window_sum}"

        stats = limiter.stats_dict()
        assert stats["totalRequests"] == 100
        assert stats["totalWeight"] == 2000
        assert stats["baseWeightTotal"] == 2000
        assert stats["extraWeightTotal"] == 0
        assert stats["requestsByEndpoint"] == {"portfolio": 100}

    asyncio.run(scenario(800))
    asyncio.run(scenario(1000))


def test_dynamic_extra_weight_debt_and_large_single_request_no_deadlock() -> None:
    async def scenario() -> None:
        now = 0.0

        async def sim_sleep(dt: float) -> None:
            nonlocal now
            now += dt

        limiter = WeightRateLimiter(budget_per_min=800, clock=lambda: now, sleep=sim_sleep)
        # 7 requests * 120 (20 base + 100 extra) = 840 > 800 budget -> puts bucket in -40 debt.
        for _ in range(7):
            await limiter.acquire(20, endpoint="userFillsByTime")
            await limiter.adjust(100, endpoint="userFillsByTime")
        await limiter.adjust(0, endpoint="userFillsByTime")

        assert now == 0.0
        t_before = now
        await limiter.acquire(20, endpoint="clearinghouseState")
        # Must wait full 60s for rolling 840 excess to clear and debt to recover.
        assert now - t_before >= 60.0 - 1e-9

        stats = limiter.stats_dict()
        assert stats["baseWeightTotal"] == 160
        assert stats["extraWeightTotal"] == 700
        assert stats["totalWeight"] == 860

        # Single request larger than configured budget should not deadlock.
        small = WeightRateLimiter(budget_per_min=40, clock=lambda: now, sleep=sim_sleep)
        await small.acquire(60, endpoint="userRole")
        t_large = now
        await small.acquire(20, endpoint="portfolio")
        assert now - t_large >= 60.0 - 1e-9

    asyncio.run(scenario())


def test_429_5xx_backoff_and_consecutive_429_budget_halving() -> None:
    async def scenario() -> None:
        now = 0.0
        sleeps: list[float] = []

        async def sim_sleep(dt: float) -> None:
            nonlocal now
            sleeps.append(dt)
            now += dt

        limiter = WeightRateLimiter(
            budget_per_min=800,
            max_retries=6,
            halve_cooldown_seconds=300.0,
            clock=lambda: now,
            sleep=sim_sleep,
            rng=lambda: 0.25,
        )

        for k in range(6):
            assert limiter.compute_backoff_delay(k) == pytest.approx((2**k) + 0.25)

        # Single 429 followed by 200 OK does NOT halve budget.
        await limiter.wait_for_retry(0, status_code=429)
        assert not limiter.is_halved()
        assert limiter.effective_budget_per_min() == 800
        await limiter.record_response(200)

        # Two consecutive 429s activate 300s budget halving (800 -> 400).
        await limiter.wait_for_retry(0, status_code=429)
        assert not limiter.is_halved()
        t_halved_start = now
        await limiter.wait_for_retry(1, status_code=429)
        assert limiter.is_halved()
        assert limiter.effective_budget_per_min() == 400
        assert limiter.stats.halve_events == 1

        # A third consecutive 429 extends cooldown without creating a duplicate halve_event.
        await limiter.wait_for_retry(2, status_code=429)
        assert limiter.stats.halve_events == 1

        # Verify halved budget caps 60s rolling acquisitions at 400 weight.
        t_burst_start = now
        for _ in range(21):  # 21 * 20 = 420 > 400
            await limiter.acquire(20, endpoint="portfolio")
        assert now - t_burst_start >= 60.0 - 1e-9

        # 5xx and transport errors reset consecutive-429 counter and increment stats.
        await limiter.wait_for_retry(0, status_code=503)
        await limiter.wait_for_retry(0, is_transport_error=True)
        assert limiter.stats.http_5xx_count == 1
        assert limiter.stats.transport_error_count == 1
        assert limiter.stats.http_429_count == 4
        assert limiter.stats.retries_total == 6

        # Advance past 300s cooldown from last 429 -> full 800 budget restores.
        now = t_halved_start + 350.0
        assert not limiter.is_halved()
        assert limiter.effective_budget_per_min() == 800

    asyncio.run(scenario())


def test_rate_limiter_negative_input_guards() -> None:
    async def scenario() -> None:
        limiter = WeightRateLimiter()
        with pytest.raises(ValueError, match="base_weight"):
            await limiter.acquire(-1)
        with pytest.raises(ValueError, match="extra_weight"):
            await limiter.adjust(-1)
        with pytest.raises(ValueError, match="attempt"):
            limiter.compute_backoff_delay(-1)

    asyncio.run(scenario())

# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit and mocked integration tests for streaming LeaderboardClient and fallback cache."""

from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from hyperpod.client.leaderboard import (
    DEFAULT_LEADERBOARD_URL,
    LeaderboardClient,
    LeaderboardFetchError,
    fetch_leaderboard,
)
from hyperpod.client.ratelimit import WeightRateLimiter


def _sample_leaderboard_payload(av: str = "12500000.5") -> dict[str, Any]:
    return {
        "leaderboardRows": [
            {
                "ethAddress": "0xf5d81a135f756ca16544e53c20fc20643ec3ad53",
                "accountValue": av,
                "windowPerformances": [
                    ["day", {"pnl": "1000.0", "roi": "0.01", "vlm": "50000.0"}],
                    ["week", {"pnl": "5000.0", "roi": "0.04", "vlm": "250000.0"}],
                    ["month", {"pnl": "25000.0", "roi": "0.15", "vlm": "1500000.0"}],
                    ["allTime", {"pnl": "2500000.0", "roi": "0.55", "vlm": "18000000.0"}],
                ],
                "prize": 0,
                "displayName": None,
            }
        ]
    }


def _fast_limiter(max_retries: int = 2) -> WeightRateLimiter:
    now = 0.0

    async def fast_sleep(dt: float) -> None:
        nonlocal now
        now += dt

    return WeightRateLimiter(
        max_retries=max_retries,
        clock=lambda: now,
        sleep=fast_sleep,
        rng=lambda: 0.0,
    )


def test_leaderboard_streaming_download_validation_and_atomic_cache(tmp_path: Path) -> None:
    payload = _sample_leaderboard_payload()
    raw_bytes = json.dumps(payload).encode("utf-8")
    cache_path = tmp_path / "daily" / "leaderboard_last.json.gz"

    async def scenario() -> None:
        with respx.mock(assert_all_called=True) as router:
            router.get(DEFAULT_LEADERBOARD_URL).mock(
                return_value=httpx.Response(200, content=raw_bytes)
            )
            res = await fetch_leaderboard(
                cache_path=cache_path,
                rate_limiter=_fast_limiter(),
            )

        assert not res.used_fallback
        assert res.warnings == []
        assert res.bytes_downloaded == len(raw_bytes)
        assert len(res.response.leaderboardRows) == 1
        row = res.response.leaderboardRows[0]
        assert row.account_value_float == pytest.approx(12_500_000.5)
        assert row.month_pnl == pytest.approx(25_000.0)
        assert row.month_vlm == pytest.approx(1_500_000.0)
        assert row.all_time_pnl == pytest.approx(2_500_000.0)
        assert row.all_time_roi == pytest.approx(0.55)

        assert cache_path.is_file()
        cached_json = json.loads(gzip.decompress(cache_path.read_bytes()).decode("utf-8"))
        assert cached_json == payload

    asyncio.run(scenario())


def test_leaderboard_fallback_on_http_transport_json_and_schema_failures(tmp_path: Path) -> None:
    cache_path = tmp_path / "leaderboard_last.json.gz"
    cached_payload = _sample_leaderboard_payload(av="9999999.0")
    cache_path.write_bytes(gzip.compress(json.dumps(cached_payload).encode("utf-8")))

    async def run_with_mock(mock_response: Any) -> None:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(DEFAULT_LEADERBOARD_URL)
            if isinstance(mock_response, Exception):
                route.mock(side_effect=mock_response)
            else:
                route.mock(return_value=mock_response)
            async with LeaderboardClient(
                max_retries=1, rate_limiter=_fast_limiter(max_retries=1)
            ) as client:
                res = await client.fetch(cache_path=cache_path)

        assert res.used_fallback is True
        assert res.bytes_downloaded == 0
        assert len(res.warnings) == 1
        assert res.response.leaderboardRows[0].accountValue == "9999999.0"

    asyncio.run(run_with_mock(httpx.Response(500, text="server error")))
    asyncio.run(run_with_mock(httpx.ReadTimeout("timed out")))
    asyncio.run(run_with_mock(httpx.Response(200, text="{malformed json")))
    # Schema mismatch: missing allTime window
    bad_schema = _sample_leaderboard_payload()
    bad_schema["leaderboardRows"][0]["windowPerformances"].pop()
    asyncio.run(run_with_mock(httpx.Response(200, json=bad_schema)))


def test_leaderboard_errors_when_no_cache_corrupt_cache_or_exceeds_max_bytes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        # 1. No cache on HTTP 500 -> raises LeaderboardFetchError
        with respx.mock(assert_all_called=True) as router:
            router.get(DEFAULT_LEADERBOARD_URL).mock(return_value=httpx.Response(503))
            async with LeaderboardClient(
                max_retries=1, rate_limiter=_fast_limiter(max_retries=1)
            ) as client:
                with pytest.raises(LeaderboardFetchError, match="HTTP 503"):
                    await client.fetch(cache_path=None)

        # 2. HTTP 404 without cache -> fails fast without retries
        with respx.mock(assert_all_called=True) as router:
            router.get(DEFAULT_LEADERBOARD_URL).mock(return_value=httpx.Response(404))
            limiter = _fast_limiter(max_retries=3)
            async with LeaderboardClient(max_retries=3, rate_limiter=limiter) as client:
                with pytest.raises(LeaderboardFetchError, match="HTTP 404"):
                    await client.fetch(cache_path=None)
            assert limiter.stats.retries_total == 0

        # 3. Corrupted cache on live failure -> raises LeaderboardFetchError
        bad_cache = tmp_path / "corrupt.json.gz"
        bad_cache.write_bytes(b"not-gzip")
        with respx.mock(assert_all_called=True) as router:
            router.get(DEFAULT_LEADERBOARD_URL).mock(return_value=httpx.Response(500))
            async with LeaderboardClient(
                max_retries=0, rate_limiter=_fast_limiter(max_retries=0)
            ) as client:
                with pytest.raises(LeaderboardFetchError, match="fallback cache"):
                    await client.fetch(cache_path=bad_cache)

        # 4. Streamed payload exceeding max_bytes raises LeaderboardFetchError
        oversized = json.dumps(_sample_leaderboard_payload()).encode("utf-8")
        with respx.mock(assert_all_called=True) as router:
            router.get(DEFAULT_LEADERBOARD_URL).mock(
                return_value=httpx.Response(200, content=oversized)
            )
            async with LeaderboardClient(max_bytes=50, chunk_size=16) as client:
                with pytest.raises(LeaderboardFetchError, match="max_bytes"):
                    await client.fetch(cache_path=None)

        # 5. Content-Length exceeding max_bytes raises immediately
        with respx.mock(assert_all_called=True) as router:
            router.get(DEFAULT_LEADERBOARD_URL).mock(
                return_value=httpx.Response(
                    200,
                    headers={"Content-Length": "999999"},
                    content=b"{}",
                )
            )
            async with LeaderboardClient(max_bytes=1024) as client:
                with pytest.raises(LeaderboardFetchError, match="Content-Length"):
                    await client.fetch(cache_path=None)

    asyncio.run(scenario())

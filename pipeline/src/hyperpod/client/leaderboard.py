# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Streaming Hyperliquid leaderboard client with schema validation and fallback cache (§6.4)."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import ValidationError

from hyperpod.client.ratelimit import WeightRateLimiter
from hyperpod.models.api import LeaderboardResponse

DEFAULT_LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
DEFAULT_MAX_LEADERBOARD_BYTES = 256 * 1024 * 1024


class LeaderboardFetchError(RuntimeError):
    """Raised when both live leaderboard download and fallback cache fail."""


@dataclass(frozen=True)
class LeaderboardFetchResult:
    """Result of fetching and validating the Hyperliquid leaderboard."""

    response: LeaderboardResponse
    used_fallback: bool
    warnings: list[str]
    bytes_downloaded: int


def _write_gzip_json_atomic(path: Path, raw_bytes: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    compressed = gzip.compress(raw_bytes)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(compressed)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _read_gzip_json(path: Path) -> object:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


class LeaderboardClient:
    """Async streaming downloader for `stats-data.hyperliquid.xyz/Mainnet/leaderboard`."""

    def __init__(
        self,
        *,
        leaderboard_url: str = DEFAULT_LEADERBOARD_URL,
        timeout_seconds: float = 120.0,
        max_bytes: int = DEFAULT_MAX_LEADERBOARD_BYTES,
        chunk_size: int = 65_536,
        max_retries: int = 3,
        rate_limiter: WeightRateLimiter | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")

        self.leaderboard_url = leaderboard_url
        self.max_bytes = max_bytes
        self.chunk_size = chunk_size
        self.max_retries = max_retries
        self.rate_limiter = rate_limiter
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    async def __aenter__(self) -> LeaderboardClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the owned `httpx.AsyncClient` if created internally."""
        if self._owns_client:
            await self._client.aclose()

    async def _wait_retry(
        self,
        attempt: int,
        *,
        status_code: int | None = None,
        is_transport_error: bool = False,
    ) -> None:
        if self.rate_limiter is not None:
            await self.rate_limiter.wait_for_retry(
                attempt,
                status_code=status_code,
                is_transport_error=is_transport_error,
            )
            return
        await asyncio.sleep(float(2**attempt))

    async def _download_and_validate(self) -> tuple[LeaderboardResponse, bytes]:
        for attempt in range(self.max_retries + 1):
            try:
                async with self._client.stream("GET", self.leaderboard_url) as resp:
                    status = resp.status_code
                    if status == 429 or 500 <= status < 600:
                        if attempt < self.max_retries:
                            await self._wait_retry(attempt, status_code=status)
                            continue
                        if self.rate_limiter is not None:
                            await self.rate_limiter.record_response(status)
                        raise LeaderboardFetchError(f"Leaderboard endpoint returned HTTP {status}")
                    if self.rate_limiter is not None:
                        await self.rate_limiter.record_response(status)
                    if status >= 400:
                        raise LeaderboardFetchError(f"Leaderboard endpoint returned HTTP {status}")

                    content_len_hdr = resp.headers.get("content-length")
                    if content_len_hdr is not None:
                        try:
                            content_len = int(content_len_hdr)
                        except ValueError:
                            content_len = 0
                        if content_len > self.max_bytes:
                            raise LeaderboardFetchError(
                                f"Leaderboard Content-Length ({content_len} B) exceeds "
                                f"max_bytes ({self.max_bytes} B)"
                            )

                    buf = bytearray()
                    async for chunk in resp.aiter_bytes(chunk_size=self.chunk_size):
                        if len(buf) + len(chunk) > self.max_bytes:
                            raise LeaderboardFetchError(
                                f"Leaderboard stream exceeded max_bytes ({self.max_bytes} B)"
                            )
                        buf.extend(chunk)
            except (httpx.TransportError, httpx.DecodingError, httpx.StreamError) as exc:
                if attempt < self.max_retries:
                    await self._wait_retry(attempt, is_transport_error=True)
                    continue
                if self.rate_limiter is not None:
                    await self.rate_limiter.record_transport_error()
                raise LeaderboardFetchError(f"Leaderboard transport failure: {exc}") from exc

            raw_bytes = bytes(buf)
            try:
                payload = json.loads(raw_bytes.decode("utf-8"))
                parsed = LeaderboardResponse.model_validate(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
                raise LeaderboardFetchError(
                    f"Leaderboard payload validation failed: {exc}"
                ) from exc

            return parsed, raw_bytes

        raise LeaderboardFetchError("Leaderboard download exhausted retries")  # pragma: no cover

    async def fetch(self, cache_path: Path | None = None) -> LeaderboardFetchResult:
        """Stream, validate, and optionally cache leaderboard JSON, falling back on failure."""
        try:
            parsed, raw_bytes = await self._download_and_validate()
        except LeaderboardFetchError as exc:
            if cache_path is not None and cache_path.is_file():
                try:
                    cached_raw = _read_gzip_json(cache_path)
                    cached = LeaderboardResponse.model_validate(cached_raw)
                except (OSError, json.JSONDecodeError, ValidationError) as cache_exc:
                    raise LeaderboardFetchError(
                        f"{exc}; fallback cache {cache_path} failed: {cache_exc}"
                    ) from exc
                return LeaderboardFetchResult(
                    response=cached,
                    used_fallback=True,
                    warnings=[f"Leaderboard fetch failed ({exc}); using cached {cache_path.name}"],
                    bytes_downloaded=0,
                )
            raise

        if cache_path is not None:
            _write_gzip_json_atomic(cache_path, raw_bytes)

        return LeaderboardFetchResult(
            response=parsed,
            used_fallback=False,
            warnings=[],
            bytes_downloaded=len(raw_bytes),
        )


async def fetch_leaderboard(
    cache_path: Path | None = None,
    *,
    leaderboard_url: str = DEFAULT_LEADERBOARD_URL,
    max_bytes: int = DEFAULT_MAX_LEADERBOARD_BYTES,
    rate_limiter: WeightRateLimiter | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> LeaderboardFetchResult:
    """Convenience wrapper around `LeaderboardClient.fetch`."""
    async with LeaderboardClient(
        leaderboard_url=leaderboard_url,
        max_bytes=max_bytes,
        rate_limiter=rate_limiter,
        http_client=http_client,
    ) as client:
        return await client.fetch(cache_path=cache_path)

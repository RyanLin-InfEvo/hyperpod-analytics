# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed Hyperliquid Info API client and fill/equity normalization (§5.1–§5.4, ADR 0001–0003)."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from hyperpod.client.ratelimit import (
    WeightRateLimiter,
    base_weight_for_endpoint,
    compute_dynamic_extra_weight,
)
from hyperpod.models.api import (
    ClearinghouseStateResponse,
    MetaAndAssetCtxsResponse,
    PerpMeta,
    PortfolioResponse,
    RawCandle,
    RawFill,
    SpotClearinghouseStateResponse,
    SubAccountItem,
    UserFundingEvent,
    UserRoleResponse,
)
from hyperpod.models.fills import DirCode, FillRow, LiqFlag
from hyperpod.models.whitelist import EquitySource

DEFAULT_INFO_URL = "https://api.hyperliquid.xyz/info"
_FILLS_PAGE_LIMIT = 2000
_FUNDING_PAGE_LIMIT = 500
_ZERO_HASH = "0x" + "0" * 64
_ISOLATED_ABSTRACTIONS = frozenset({"disabled", "default"})
_SHARED_ABSTRACTIONS = frozenset({"unifiedAccount", "portfolioMargin", "dexAbstraction"})
_STANDARD_DIR_MAP: dict[str, DirCode] = {
    "Open Long": "OL",
    "Close Long": "CL",
    "Open Short": "OS",
    "Close Short": "CS",
}


class InfoApiError(RuntimeError):
    """Base error raised by InfoApiClient for unrecoverable HTTP or payload errors."""


class ApiHttpError(InfoApiError):
    """HTTP error status returned by Hyperliquid Info API."""

    def __init__(self, endpoint: str, status_code: int, detail: str = "") -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        self.detail = detail
        msg = f"Hyperliquid Info API {endpoint} failed with HTTP {status_code}"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg)


class ApiTransportError(InfoApiError):
    """Network/transport error raised after exhausting retries."""

    def __init__(self, endpoint: str, detail: str) -> None:
        self.endpoint = endpoint
        self.detail = detail
        super().__init__(f"Hyperliquid Info API {endpoint} transport error: {detail}")


def is_main_perp_coin(coin: str, main_perp_universe: set[str] | None = None) -> bool:
    """Return True iff `coin` belongs to the main perpetual DEX (ADR-0002 Decision 2)."""
    if not coin or coin == "PURR/USDC":
        return False
    if any(ch in coin for ch in ("#", ":", "@", "/")):
        return False
    return main_perp_universe is None or coin in main_perp_universe


@dataclass(frozen=True)
class NormalizedFill:
    """Normalized main-perp fill leg ordered by `(time, api_seq_index, leg_idx)`."""

    coin: str
    time: int
    api_seq_index: int
    leg_idx: int
    dir_code: DirCode
    px: float
    sz: float
    notional: float
    closed_pnl: float
    fee: float
    start_position: float
    crossed: bool
    liq: LiqFlag
    tid: int
    oid: int
    hash: str


@dataclass(frozen=True)
class FillsPageResult:
    """Result of paginated `userFillsByTime` retrieval with deduplication metadata."""

    fills: list[tuple[int, RawFill]]
    pages_fetched: int
    raw_fetched_count: int
    last_cursor_ms: int | None
    has_more: bool
    truncated_10k: bool
    warnings: list[str]


@dataclass(frozen=True)
class FundingPageResult:
    """Result of paginated `userFunding` retrieval with main-perp net/abs summaries."""

    events: list[UserFundingEvent]
    main_perp_events: list[UserFundingEvent]
    cum_funding_net: float
    cum_funding_abs: float
    last_funding_time: int | None
    last_funding_coins: list[str]
    pages_fetched: int


def normalize_fills(
    raw_fills: Sequence[tuple[int, RawFill] | RawFill],
    user_address: str,
    *,
    main_perp_universe: set[str] | None = None,
) -> tuple[list[NormalizedFill], list[str]]:
    """Filter and normalize raw fills into ordered main-perp legs (§5.2, ADR-0002, Proposal 0001).

    Returns `(normalized_fills, warnings)`.
    """
    user_lower = user_address.lower()
    normalized: list[NormalizedFill] = []
    warnings: list[str] = []
    unknown_count = 0
    unknown_samples: list[str] = []

    indexed: list[tuple[int, RawFill]] = []
    for idx, item in enumerate(raw_fills):
        if isinstance(item, RawFill):
            indexed.append((idx, item))
        else:
            indexed.append(item)

    for seq_idx, fill in indexed:
        if not is_main_perp_coin(fill.coin, main_perp_universe):
            continue

        is_self_liq = (
            fill.liquidation is not None and fill.liquidation.liquidatedUser.lower() == user_lower
        )
        liq: LiqFlag = 1 if is_self_liq else 0
        px = float(fill.px)
        sz = float(fill.sz)
        start_pos = float(fill.startPosition)
        closed_pnl = float(fill.closedPnl)
        fee = float(fill.fee)

        direct_dir = _STANDARD_DIR_MAP.get(fill.dir)
        if direct_dir is not None:
            normalized.append(
                NormalizedFill(
                    coin=fill.coin,
                    time=fill.time,
                    api_seq_index=seq_idx,
                    leg_idx=0,
                    dir_code=direct_dir,
                    px=px,
                    sz=sz,
                    notional=px * sz,
                    closed_pnl=closed_pnl,
                    fee=fee,
                    start_position=start_pos,
                    crossed=fill.crossed,
                    liq=liq,
                    tid=fill.tid,
                    oid=fill.oid,
                    hash=fill.hash,
                )
            )
            continue

        if fill.dir in ("Long > Short", "Short > Long"):
            close_dir: DirCode = "CL" if fill.dir == "Long > Short" else "CS"
            open_dir: DirCode = "OS" if fill.dir == "Long > Short" else "OL"
            expected_sign_ok = (
                (start_pos > 0.0) if fill.dir == "Long > Short" else (start_pos < 0.0)
            )
            close_sz = abs(start_pos) if expected_sign_ok else 0.0

            if close_sz <= 0.0:
                warnings.append(
                    f"Reversal fill {fill.dir!r} for {fill.coin} at t={fill.time} "
                    f"had non-matching startPosition={start_pos}; emitting {open_dir}"
                )
                normalized.append(
                    NormalizedFill(
                        coin=fill.coin,
                        time=fill.time,
                        api_seq_index=seq_idx,
                        leg_idx=0,
                        dir_code=open_dir,
                        px=px,
                        sz=sz,
                        notional=px * sz,
                        closed_pnl=closed_pnl,
                        fee=fee,
                        start_position=start_pos,
                        crossed=fill.crossed,
                        liq=liq,
                        tid=fill.tid,
                        oid=fill.oid,
                        hash=fill.hash,
                    )
                )
            elif close_sz >= sz:
                warnings.append(
                    f"Reversal fill {fill.dir!r} for {fill.coin} at t={fill.time} "
                    f"had |startPosition|={close_sz} >= sz={sz}; emitting {close_dir}"
                )
                normalized.append(
                    NormalizedFill(
                        coin=fill.coin,
                        time=fill.time,
                        api_seq_index=seq_idx,
                        leg_idx=0,
                        dir_code=close_dir,
                        px=px,
                        sz=sz,
                        notional=px * sz,
                        closed_pnl=closed_pnl,
                        fee=fee,
                        start_position=start_pos,
                        crossed=fill.crossed,
                        liq=liq,
                        tid=fill.tid,
                        oid=fill.oid,
                        hash=fill.hash,
                    )
                )
            else:
                open_sz = sz - close_sz
                close_fee = fee * (close_sz / sz)
                open_fee = fee - close_fee
                normalized.append(
                    NormalizedFill(
                        coin=fill.coin,
                        time=fill.time,
                        api_seq_index=seq_idx,
                        leg_idx=0,
                        dir_code=close_dir,
                        px=px,
                        sz=close_sz,
                        notional=px * close_sz,
                        closed_pnl=closed_pnl,
                        fee=close_fee,
                        start_position=start_pos,
                        crossed=fill.crossed,
                        liq=liq,
                        tid=fill.tid,
                        oid=fill.oid,
                        hash=fill.hash,
                    )
                )
                normalized.append(
                    NormalizedFill(
                        coin=fill.coin,
                        time=fill.time,
                        api_seq_index=seq_idx,
                        leg_idx=1,
                        dir_code=open_dir,
                        px=px,
                        sz=open_sz,
                        notional=px * open_sz,
                        closed_pnl=0.0,
                        fee=open_fee,
                        start_position=0.0,
                        crossed=fill.crossed,
                        liq=liq,
                        tid=fill.tid,
                        oid=fill.oid,
                        hash=fill.hash,
                    )
                )
            continue

        dir_lower = fill.dir.lower()
        if is_self_liq or any(
            tok in dir_lower for tok in ("liquidat", "adl", "deleverag", "settl")
        ):
            if start_pos > 0.0:
                derived_dir: DirCode = "CL"
            elif start_pos < 0.0:
                derived_dir = "CS"
            else:
                derived_dir = "CL" if fill.side == "A" else "CS"
            warnings.append(
                f"Fallback liquidation/settlement dir {fill.dir!r} mapped to {derived_dir} "
                f"for {fill.coin} at t={fill.time} (startPosition={start_pos}, side={fill.side})"
            )
            fallback_liq: LiqFlag = 0 if (fill.liquidation is not None and not is_self_liq) else 1
            normalized.append(
                NormalizedFill(
                    coin=fill.coin,
                    time=fill.time,
                    api_seq_index=seq_idx,
                    leg_idx=0,
                    dir_code=derived_dir,
                    px=px,
                    sz=sz,
                    notional=px * sz,
                    closed_pnl=closed_pnl,
                    fee=fee,
                    start_position=start_pos,
                    crossed=fill.crossed,
                    liq=fallback_liq,
                    tid=fill.tid,
                    oid=fill.oid,
                    hash=fill.hash,
                )
            )
            continue

        unknown_count += 1
        if len(unknown_samples) < 5:
            unknown_samples.append(f"{fill.coin}:{fill.dir!r}@{fill.time}")

    if unknown_count > 0:
        warnings.append(
            f"Discarded {unknown_count} main-perp fill(s) with unknown dir: {unknown_samples}"
        )

    normalized.sort(key=lambda item: (item.time, item.api_seq_index, item.leg_idx))
    return normalized, warnings


def aggregate_public_fill_rows(
    fills: Iterable[NormalizedFill], addr_id: int
) -> dict[str, list[FillRow]]:
    """Aggregate normalized fills into `§3.4` public `FillRow` tuples per coin.

    Rows sharing `(time, addr_id, dir_code, liq)` are merged with VWAP px and summed sz/notional.
    """
    if addr_id < 0:
        raise ValueError("addr_id must be >= 0")
    buckets: dict[str, dict[tuple[int, int, DirCode, LiqFlag], list[float]]] = defaultdict(dict)
    for fill in fills:
        if fill.px <= 0.0 or fill.sz <= 0.0:
            continue
        key: tuple[int, int, DirCode, LiqFlag] = (fill.time, addr_id, fill.dir_code, fill.liq)
        acc = buckets[fill.coin].get(key)
        if acc is None:
            buckets[fill.coin][key] = [fill.sz, fill.notional]
        else:
            acc[0] += fill.sz
            acc[1] += fill.notional

    result: dict[str, list[FillRow]] = {}
    for coin, coin_buckets in sorted(buckets.items()):
        rows: list[FillRow] = []
        for (ts, aid, dir_code, liq), (total_sz, total_notional) in sorted(coin_buckets.items()):
            if total_sz <= 0.0:
                continue
            vwap_px = total_notional / total_sz
            if vwap_px <= 0.0:
                continue
            rows.append((ts, aid, dir_code, vwap_px, total_sz, total_notional, liq))
        result[coin] = rows
    return result


def compute_account_equity(
    clearinghouse: ClearinghouseStateResponse,
    abstraction: str,
    spot: SpotClearinghouseStateResponse | None = None,
) -> tuple[float, EquitySource, list[str]]:
    """Compute account equity and equitySource across Hyperliquid abstraction modes (ADR-0001)."""
    ch_av = float(clearinghouse.marginSummary.accountValue)
    c_mm = float(clearinghouse.crossMaintenanceMarginUsed)

    if abstraction in _ISOLATED_ABSTRACTIONS:
        return ch_av, "perp", []

    warnings: list[str] = []
    if abstraction not in _SHARED_ABSTRACTIONS:
        warnings.append(
            f"Unrecognized userAbstraction {abstraction!r}; using perp+spot equity calculation"
        )

    if spot is None:
        warnings.append(
            "Missing spotClearinghouseState for perp+spot abstraction; "
            "falling back to clearinghouseState accountValue"
        )
        return ch_av, "perp+spot", warnings

    spot_usdc_total = 0.0
    for bal in spot.balances:
        if bal.token == 0 or bal.coin == "USDC":
            spot_usdc_total = float(bal.total)
            break

    avail_after_maint_usdc: float | None = None
    if spot.tokenToAvailableAfterMaintenance is not None:
        for token_id, val in spot.tokenToAvailableAfterMaintenance:
            if token_id == 0:
                avail_after_maint_usdc = float(val)
                break

    terms = [ch_av, spot_usdc_total]
    if avail_after_maint_usdc is not None:
        terms.append(avail_after_maint_usdc + c_mm)

    return max(terms), "perp+spot", warnings


class InfoApiClient:
    """Async typed wrapper around Hyperliquid `POST /info` with bounded concurrency and retries."""

    def __init__(
        self,
        *,
        rate_limiter: WeightRateLimiter | None = None,
        max_concurrency: int = 4,
        info_url: str = DEFAULT_INFO_URL,
        timeout_seconds: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.rate_limiter = rate_limiter or WeightRateLimiter()
        self.max_concurrency = max_concurrency
        self.info_url = info_url
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    async def __aenter__(self) -> InfoApiClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the owned `httpx.AsyncClient` if created internally."""
        if self._owns_client:
            await self._client.aclose()

    async def _post_info(
        self,
        payload: dict[str, Any],
        endpoint_type: str,
        *,
        item_counter: Any = None,
    ) -> Any:
        base_weight = base_weight_for_endpoint(endpoint_type)
        max_retries = self.rate_limiter.max_retries

        for attempt in range(max_retries + 1):
            retry_status: int | None = None
            retry_transport = False

            async with self._semaphore:
                await self.rate_limiter.acquire(base_weight, endpoint=endpoint_type)
                try:
                    resp = await self._client.post(self.info_url, json=payload)
                except (httpx.TransportError, httpx.DecodingError, httpx.StreamError) as exc:
                    if attempt < max_retries:
                        retry_transport = True
                    else:
                        await self.rate_limiter.record_transport_error()
                        raise ApiTransportError(endpoint_type, str(exc)) from exc
                else:
                    status = resp.status_code
                    if status == 429 or 500 <= status < 600:
                        if attempt < max_retries:
                            retry_status = status
                        else:
                            await self.rate_limiter.record_response(status)
                            raise ApiHttpError(endpoint_type, status, resp.text[:300])
                    else:
                        await self.rate_limiter.record_response(status)
                        if status >= 400:
                            raise ApiHttpError(endpoint_type, status, resp.text[:300])

                        try:
                            data: Any = resp.json()
                        except ValueError as exc:
                            raise InfoApiError(
                                f"Hyperliquid Info API {endpoint_type} returned invalid JSON"
                            ) from exc

                        if item_counter is not None:
                            count = int(item_counter(data))
                            extra = compute_dynamic_extra_weight(endpoint_type, count)
                            await self.rate_limiter.adjust(extra, endpoint=endpoint_type)

                        return data

            if retry_transport:
                await self.rate_limiter.wait_for_retry(attempt, is_transport_error=True)
            elif retry_status is not None:
                await self.rate_limiter.wait_for_retry(attempt, status_code=retry_status)

        raise InfoApiError(f"Unreachable retry loop exit for {endpoint_type}")  # pragma: no cover

    async def fetch_meta(self) -> PerpMeta:
        data = await self._post_info({"type": "meta"}, "meta")
        return PerpMeta.model_validate(data)

    async def fetch_meta_and_asset_ctxs(self) -> MetaAndAssetCtxsResponse:
        data = await self._post_info({"type": "metaAndAssetCtxs"}, "metaAndAssetCtxs")
        return MetaAndAssetCtxsResponse.model_validate(data)

    async def fetch_user_role(self, user: str) -> UserRoleResponse:
        data = await self._post_info({"type": "userRole", "user": user}, "userRole")
        return UserRoleResponse.model_validate(data)

    async def fetch_user_abstraction(self, user: str) -> str:
        data = await self._post_info({"type": "userAbstraction", "user": user}, "userAbstraction")
        if not isinstance(data, str) or not data.strip():
            raise InfoApiError(f"Expected non-empty string for userAbstraction, got {data!r}")
        return data

    async def fetch_sub_accounts(self, user: str) -> list[SubAccountItem]:
        data = await self._post_info({"type": "subAccounts", "user": user}, "subAccounts")
        if data is None:
            return []
        if not isinstance(data, list):
            raise InfoApiError(f"Expected list or null for subAccounts, got {type(data).__name__}")
        return [SubAccountItem.model_validate(item) for item in data]

    async def fetch_clearinghouse_state(self, user: str) -> ClearinghouseStateResponse:
        data = await self._post_info(
            {"type": "clearinghouseState", "user": user}, "clearinghouseState"
        )
        return ClearinghouseStateResponse.model_validate(data)

    async def fetch_spot_clearinghouse_state(self, user: str) -> SpotClearinghouseStateResponse:
        data = await self._post_info(
            {"type": "spotClearinghouseState", "user": user}, "spotClearinghouseState"
        )
        return SpotClearinghouseStateResponse.model_validate(data)

    async def fetch_portfolio(self, user: str) -> PortfolioResponse:
        data = await self._post_info({"type": "portfolio", "user": user}, "portfolio")
        return PortfolioResponse.model_validate(data)

    async def fetch_candle_snapshot(
        self,
        coin: str,
        interval: str,
        start_time: int,
        end_time: int,
    ) -> list[RawCandle]:
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": start_time,
                "endTime": end_time,
            },
        }
        data = await self._post_info(
            payload,
            "candleSnapshot",
            item_counter=lambda d: len(d) if isinstance(d, list) else 0,
        )
        if not isinstance(data, list):
            raise InfoApiError(f"Expected list for candleSnapshot, got {type(data).__name__}")
        return [RawCandle.model_validate(item) for item in data]

    async def fetch_user_fills_by_time(
        self,
        user: str,
        *,
        start_time: int = 0,
        end_time: int | None = None,
        overlap_ms: int = 60_000,
        include_initial_overlap: bool = True,
        max_pages: int | None = None,
        seed_keys: Iterable[tuple[int, int, str, str, str]] = (),
        main_perp_universe: set[str] | None = None,
    ) -> FillsPageResult:
        """Paginate `userFillsByTime(aggregateByTime=True)` inclusively (§5.2, ADR-0002)."""
        if start_time < 0:
            raise ValueError("start_time must be >= 0")
        if overlap_ms < 0:
            raise ValueError("overlap_ms must be >= 0")
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be >= 1")

        cursor = (
            max(0, start_time - overlap_ms)
            if (start_time > 0 and include_initial_overlap)
            else start_time
        )
        seen_keys: set[tuple[int, int, str, str, str]] = set(seed_keys)
        initial_seed_empty = len(seen_keys) == 0
        deduped: list[tuple[int, RawFill]] = []
        warnings: list[str] = []
        seq_idx = 0
        pages = 0
        raw_count = 0
        last_cursor_ms: int | None = None
        exact_ms_retry_for: int | None = None
        has_more = False

        while True:
            if max_pages is not None and pages >= max_pages:
                has_more = True
                break

            payload: dict[str, Any] = {
                "type": "userFillsByTime",
                "user": user,
                "startTime": cursor,
                "aggregateByTime": True,
            }
            if end_time is not None:
                payload["endTime"] = end_time

            raw_data = await self._post_info(
                payload,
                "userFillsByTime",
                item_counter=lambda d: len(d) if isinstance(d, list) else 0,
            )
            if not isinstance(raw_data, list):
                raise InfoApiError(
                    f"Expected list for userFillsByTime, got {type(raw_data).__name__}"
                )

            page = [RawFill.model_validate(item) for item in raw_data]
            pages += 1
            raw_count += len(page)
            if not page:
                has_more = False
                break

            added = 0
            for fill in page:
                key = (fill.time, fill.tid, fill.hash, fill.coin, fill.dir)
                if key not in seen_keys:
                    seen_keys.add(key)
                    deduped.append((seq_idx, fill))
                    seq_idx += 1
                    added += 1

            last_t = page[-1].time
            last_cursor_ms = last_t

            if len(page) < _FILLS_PAGE_LIMIT:
                has_more = False
                break

            if added == 0:
                if exact_ms_retry_for != last_t and overlap_ms > 0 and cursor < last_t:
                    exact_ms_retry_for = last_t
                    cursor = last_t
                    continue
                next_ms = last_t + 1
                warnings.append(
                    f"userFillsByTime dense boundary guard triggered at t={last_t} for {user}; "
                    f"advancing cursor to {next_ms}"
                )
                exact_ms_retry_for = None
                cursor = next_ms
                continue

            exact_ms_retry_for = None
            candidate_cursor = max(0, last_t - overlap_ms) if overlap_ms > 0 else last_t
            if candidate_cursor <= cursor:
                if last_t > cursor:
                    exact_ms_retry_for = last_t
                    cursor = last_t
                else:
                    # All 2,000 fills fell on `cursor`; advance to last_t + 1 directly.
                    next_ms = last_t + 1
                    warnings.append(
                        f"userFillsByTime dense boundary guard triggered at t={last_t} for {user}; "
                        f"advancing cursor to {next_ms}"
                    )
                    exact_ms_retry_for = None
                    cursor = next_ms
            else:
                cursor = candidate_cursor

        has_nonzero_initial_start_pos = False
        if start_time == 0 and initial_seed_empty:
            first_seen_coins: set[str] = set()
            for _, fill in deduped:
                if not is_main_perp_coin(fill.coin, main_perp_universe):
                    continue
                if fill.coin not in first_seen_coins:
                    first_seen_coins.add(fill.coin)
                    if abs(float(fill.startPosition)) > 1e-12:
                        has_nonzero_initial_start_pos = True
                        break

        truncated_10k = len(deduped) >= 10_000 or has_nonzero_initial_start_pos
        return FillsPageResult(
            fills=deduped,
            pages_fetched=pages,
            raw_fetched_count=raw_count,
            last_cursor_ms=last_cursor_ms,
            has_more=has_more,
            truncated_10k=truncated_10k,
            warnings=warnings,
        )

    async def fetch_user_funding(
        self,
        user: str,
        *,
        start_time: int = 0,
        end_time: int | None = None,
        max_pages: int | None = None,
        seed_last_funding_coins: Iterable[str] = (),
        main_perp_universe: set[str] | None = None,
    ) -> FundingPageResult:
        """Paginate `userFunding` inclusively with `(time, hash, delta.coin)` dedup (ADR-0003)."""
        if start_time < 0:
            raise ValueError("start_time must be >= 0")
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be >= 1")

        cursor = start_time
        seed_coins = set(seed_last_funding_coins)
        seen_keys: set[tuple[int, str, str]] = {
            (start_time, _ZERO_HASH, coin) for coin in seed_coins
        }
        coins_by_time: dict[int, set[str]] = defaultdict(set)
        if seed_coins:
            coins_by_time[start_time].update(seed_coins)

        events: list[UserFundingEvent] = []
        pages = 0
        max_raw_t: int | None = start_time if seed_coins else None

        while True:
            if max_pages is not None and pages >= max_pages:
                break
            if end_time is not None and cursor > end_time:
                break

            payload: dict[str, Any] = {
                "type": "userFunding",
                "user": user,
                "startTime": cursor,
            }
            if end_time is not None:
                payload["endTime"] = end_time

            raw_data = await self._post_info(
                payload,
                "userFunding",
                item_counter=lambda d: len(d) if isinstance(d, list) else 0,
            )
            if not isinstance(raw_data, list):
                raise InfoApiError(f"Expected list for userFunding, got {type(raw_data).__name__}")

            page = [UserFundingEvent.model_validate(item) for item in raw_data]
            pages += 1
            if not page:
                break

            added = 0
            for ev in page:
                coins_by_time[ev.time].add(ev.delta.coin)
                if max_raw_t is None or ev.time > max_raw_t:
                    max_raw_t = ev.time

                key = (ev.time, ev.hash, ev.delta.coin)
                is_seeded_boundary = ev.time == start_time and ev.delta.coin in seed_coins
                if key not in seen_keys and not is_seeded_boundary:
                    seen_keys.add(key)
                    events.append(ev)
                    added += 1

            if len(page) < _FUNDING_PAGE_LIMIT:
                break

            cursor = (page[-1].time + 1) if added == 0 else page[-1].time

        main_perp_events = [
            ev for ev in events if is_main_perp_coin(ev.delta.coin, main_perp_universe)
        ]
        cum_net = sum(float(ev.delta.usdc) for ev in main_perp_events)
        cum_abs = sum(abs(float(ev.delta.usdc)) for ev in main_perp_events)
        last_coins = sorted(coins_by_time[max_raw_t]) if max_raw_t is not None else []

        return FundingPageResult(
            events=events,
            main_perp_events=main_perp_events,
            cum_funding_net=cum_net,
            cum_funding_abs=cum_abs,
            last_funding_time=max_raw_t,
            last_funding_coins=last_coins,
            pages_fetched=pages,
        )

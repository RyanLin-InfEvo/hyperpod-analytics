# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round-trip trade reconstruction and daily fill statistics (§4, §5.2, §5.3, ADR-0002)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import StrictStr

from hyperpod.client.info_api import NormalizedFill, normalize_fills
from hyperpod.models.api import RawFill
from hyperpod.models.common import (
    ContractModel,
    JsonNumber,
    NonNegInt,
    NonNegNumber,
)
from hyperpod.util.time import ms_to_utc_date

FillDedupKey = tuple[int, int, str, str, str]
TradeSide = Literal["long", "short"]
ClosedTradeTuple = tuple[str | int, int, int, float, float, TradeSide]


def quantize_size(szi: float, sz_decimals: int) -> float:
    """Quantize a position size to ``sz_decimals`` and zero out dust below ``10^-(d+2)`` (§5.3)."""
    if sz_decimals < 0:
        raise ValueError(f"sz_decimals must be >= 0, got {sz_decimals}")
    zero_thresh = 10.0 ** (-(sz_decimals + 2))
    if abs(szi) < zero_thresh:
        return 0.0
    rounded = round(szi, sz_decimals)
    if abs(rounded) < zero_thresh or rounded == 0.0:
        return 0.0
    return float(rounded)


class OpenTradeState(ContractModel):
    """In-progress round-trip trade state persisted in ``state/daily/addresses/{shard}.json.gz``.

    ``openTime == 0`` marks a pre-existing truncated position (first seen with
    ``startPosition != 0``) awaiting its first return to flat (``0``) before tracked
    round-trip accounting begins (§5.3).
    """

    openTime: NonNegInt
    size: JsonNumber
    realized: JsonNumber
    fees: JsonNumber
    maxNotional: NonNegNumber


class ClosedTrade(ContractModel):
    """Completed round-trip trade matching §4 ``closedTrades`` tuple semantics."""

    coin: StrictStr | NonNegInt
    open_t: NonNegInt
    close_t: NonNegInt
    net_pnl: JsonNumber
    max_notional: NonNegNumber
    side: TradeSide

    def to_tuple(self) -> ClosedTradeTuple:
        """Serialize to compact 6-tuple ``[coin, openT, closeT, netPnl, maxNotional, side]``."""
        return (
            self.coin,
            self.open_t,
            self.close_t,
            self.net_pnl,
            self.max_notional,
            self.side,
        )

    @classmethod
    def from_tuple(cls, raw: ClosedTradeTuple) -> ClosedTrade:
        """Construct from a 6-tuple ``(coin, openT, closeT, netPnl, maxNotional, side)``."""
        coin, open_t, close_t, net_pnl, max_notional, side = raw
        return cls(
            coin=coin,
            open_t=open_t,
            close_t=close_t,
            net_pnl=net_pnl,
            max_notional=max_notional,
            side=side,
        )


@dataclass
class TradeReconstructionResult:
    """Output state produced by incremental or batch round-trip trade reconstruction."""

    open_trades: dict[str, OpenTradeState]
    closed_trades: list[ClosedTrade]
    fill_stats_days: dict[str, tuple[int, int]]
    fill_notional_days: dict[str, float]
    fills_history_start: int | None
    last_fill_time: int | None
    processed_fills_count: int
    saw_truncated_position: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class FillIngestionResult:
    """Result of incremental raw fill deduplication, normalization, and trade reconstruction."""

    trades: TradeReconstructionResult
    fills_cursor_scoring: int
    recent_scoring_fill_keys: list[FillDedupKey]
    deduped_raw_count: int
    normalized_fills_count: int
    warnings: list[str] = field(default_factory=list)


def _coerce_closed_trades(
    closed_trades: Sequence[ClosedTrade | ClosedTradeTuple] | None,
) -> list[ClosedTrade]:
    if not closed_trades:
        return []
    out: list[ClosedTrade] = []
    for item in closed_trades:
        if isinstance(item, ClosedTrade):
            out.append(item.model_copy())
        else:
            out.append(ClosedTrade.from_tuple(item))
    return out


def reconstruct_trades_incremental(
    normalized_fills: Sequence[NormalizedFill],
    *,
    open_trades: Mapping[str, OpenTradeState] | None = None,
    closed_trades: Sequence[ClosedTrade | ClosedTradeTuple] | None = None,
    fill_stats_days: Mapping[str, tuple[int, int]] | None = None,
    fill_notional_days: Mapping[str, float] | None = None,
    sz_decimals_by_coin: Mapping[str, int] | None = None,
    default_sz_decimals: int = 8,
    fills_history_start: int | None = None,
    last_fill_time: int | None = None,
) -> TradeReconstructionResult:
    """Incrementally reconstruct round-trip trades and daily fill stats from normalized fills.

    Follows Proposal 0001 ``(time, api_seq_index, leg_idx)`` ordering, ``szDecimals`` zero
    quantization (§5.3), and 10k-truncation handling (ignoring fills prior to the first zero
    crossing when a coin's first observed fill has ``startPosition != 0``).
    """
    current_open: dict[str, OpenTradeState] = {
        coin: state.model_copy() for coin, state in (open_trades or {}).items()
    }
    current_closed = _coerce_closed_trades(closed_trades)
    current_stats: dict[str, tuple[int, int]] = dict(fill_stats_days or {})
    current_notionals: dict[str, float] = dict(fill_notional_days or {})
    sz_map = sz_decimals_by_coin or {}
    warnings: list[str] = []

    sorted_fills = sorted(normalized_fills, key=lambda f: (f.time, f.api_seq_index, f.leg_idx))

    hist_start = fills_history_start
    last_t = last_fill_time
    saw_truncated_position = False

    for fill in sorted_fills:
        if hist_start is None or fill.time < hist_start:
            hist_start = fill.time
        if last_t is None or fill.time > last_t:
            last_t = fill.time

        day_key = ms_to_utc_date(fill.time)
        if fill.leg_idx == 0:
            prev_n, prev_maker = current_stats.get(day_key, (0, 0))
            current_stats[day_key] = (prev_n + 1, prev_maker + (0 if fill.crossed else 1))
        current_notionals[day_key] = current_notionals.get(day_key, 0.0) + fill.notional

        coin = fill.coin
        sz_dec = sz_map.get(coin, default_sz_decimals)
        delta = fill.sz if fill.dir_code in ("OL", "CS") else -fill.sz
        state = current_open.get(coin)

        if state is None:
            start_pos_q = quantize_size(fill.start_position, sz_dec)
            if start_pos_q != 0.0:
                saw_truncated_position = True
                after_pos = quantize_size(fill.start_position + delta, sz_dec)
                if after_pos == 0.0:
                    continue
                if after_pos * start_pos_q > 0.0:
                    current_open[coin] = OpenTradeState(
                        openTime=0,
                        size=after_pos,
                        realized=0.0,
                        fees=0.0,
                        maxNotional=0.0,
                    )
                else:
                    open_ratio = min(1.0, abs(after_pos) / fill.sz) if fill.sz > 0.0 else 0.0
                    current_open[coin] = OpenTradeState(
                        openTime=fill.time,
                        size=after_pos,
                        realized=0.0,
                        fees=fill.fee * open_ratio,
                        maxNotional=abs(after_pos) * fill.px,
                    )
                continue

            if fill.dir_code in ("CL", "CS"):
                warnings.append(
                    f"Ignored orphan closing fill {fill.dir_code} for {coin} at t={fill.time} "
                    "while flat with startPosition=0"
                )
                continue

            new_size = quantize_size(delta, sz_dec)
            if new_size != 0.0:
                current_open[coin] = OpenTradeState(
                    openTime=fill.time,
                    size=new_size,
                    realized=fill.closed_pnl,
                    fees=fill.fee,
                    maxNotional=abs(new_size) * fill.px,
                )
            continue

        if state.openTime == 0:
            new_size = quantize_size(state.size + delta, sz_dec)
            if new_size == 0.0:
                current_open.pop(coin, None)
            elif new_size * state.size > 0.0:
                state.size = new_size
            else:
                open_ratio = min(1.0, abs(new_size) / fill.sz) if fill.sz > 0.0 else 0.0
                current_open[coin] = OpenTradeState(
                    openTime=fill.time,
                    size=new_size,
                    realized=0.0,
                    fees=fill.fee * open_ratio,
                    maxNotional=abs(new_size) * fill.px,
                )
            continue

        new_size = quantize_size(state.size + delta, sz_dec)
        if new_size == 0.0:
            realized = state.realized + fill.closed_pnl
            fees = state.fees + fill.fee
            side: TradeSide = "long" if state.size > 0.0 else "short"
            current_closed.append(
                ClosedTrade(
                    coin=coin,
                    open_t=state.openTime,
                    close_t=fill.time,
                    net_pnl=realized - fees,
                    max_notional=state.maxNotional,
                    side=side,
                )
            )
            current_open.pop(coin, None)
        elif new_size * state.size > 0.0:
            state.size = new_size
            state.realized += fill.closed_pnl
            state.fees += fill.fee
            state.maxNotional = max(state.maxNotional, abs(new_size) * fill.px)
        else:
            close_sz = abs(state.size)
            close_ratio = min(1.0, close_sz / fill.sz) if fill.sz > 0.0 else 1.0
            close_fee = fill.fee * close_ratio
            open_fee = fill.fee - close_fee
            realized = state.realized + fill.closed_pnl
            fees = state.fees + close_fee
            closed_side: TradeSide = "long" if state.size > 0.0 else "short"
            current_closed.append(
                ClosedTrade(
                    coin=coin,
                    open_t=state.openTime,
                    close_t=fill.time,
                    net_pnl=realized - fees,
                    max_notional=state.maxNotional,
                    side=closed_side,
                )
            )
            current_open[coin] = OpenTradeState(
                openTime=fill.time,
                size=new_size,
                realized=0.0,
                fees=open_fee,
                maxNotional=abs(new_size) * fill.px,
            )

    return TradeReconstructionResult(
        open_trades=current_open,
        closed_trades=current_closed,
        fill_stats_days=current_stats,
        fill_notional_days=current_notionals,
        fills_history_start=hist_start,
        last_fill_time=last_t,
        processed_fills_count=len(sorted_fills),
        saw_truncated_position=saw_truncated_position,
        warnings=warnings,
    )


def raw_fill_dedup_key(fill: RawFill) -> FillDedupKey:
    """Return the ADR-0002 composite deduplication key ``(time, tid, hash, coin, dir)``."""
    return (fill.time, fill.tid, fill.hash, fill.coin, fill.dir)


def ingest_raw_fills_incremental(
    raw_fills: Sequence[tuple[int, RawFill] | RawFill],
    user_address: str,
    *,
    fills_cursor_scoring: int = 0,
    recent_scoring_fill_keys: Iterable[FillDedupKey] = (),
    overlap_ms: int = 60_000,
    main_perp_universe: set[str] | None = None,
    open_trades: Mapping[str, OpenTradeState] | None = None,
    closed_trades: Sequence[ClosedTrade | ClosedTradeTuple] | None = None,
    fill_stats_days: Mapping[str, tuple[int, int]] | None = None,
    fill_notional_days: Mapping[str, float] | None = None,
    sz_decimals_by_coin: Mapping[str, int] | None = None,
    default_sz_decimals: int = 8,
    fills_history_start: int | None = None,
    last_fill_time: int | None = None,
) -> FillIngestionResult:
    """Deduplicate raw fills against ``recent_scoring_fill_keys``, normalize, and reconstruct.

    Prunes ``recent_scoring_fill_keys`` to keys with ``time >= new_cursor - overlap_ms`` so
    incremental daily/checkpoint runs match one-shot batch processing while bounding state size
    without storing raw fills (ADR-0002 Decision 1).
    """
    seen_list: list[FillDedupKey] = list(recent_scoring_fill_keys)
    seen_set: set[FillDedupKey] = set(seen_list)

    indexed_new: list[tuple[int, RawFill]] = []
    max_raw_time = fills_cursor_scoring

    for fallback_idx, item in enumerate(raw_fills):
        if isinstance(item, RawFill):
            seq_idx, fill = fallback_idx, item
        else:
            seq_idx, fill = item
        if fill.time > max_raw_time:
            max_raw_time = fill.time
        key = raw_fill_dedup_key(fill)
        if key in seen_set:
            continue
        seen_set.add(key)
        seen_list.append(key)
        indexed_new.append((seq_idx, fill))

    normalized, norm_warnings = normalize_fills(
        indexed_new,
        user_address,
        main_perp_universe=main_perp_universe,
    )

    trades_res = reconstruct_trades_incremental(
        normalized,
        open_trades=open_trades,
        closed_trades=closed_trades,
        fill_stats_days=fill_stats_days,
        fill_notional_days=fill_notional_days,
        sz_decimals_by_coin=sz_decimals_by_coin,
        default_sz_decimals=default_sz_decimals,
        fills_history_start=fills_history_start,
        last_fill_time=last_fill_time,
    )

    prune_floor = max(0, max_raw_time - overlap_ms)
    retained_keys = [k for k in seen_list if k[0] >= prune_floor]

    return FillIngestionResult(
        trades=trades_res,
        fills_cursor_scoring=max_raw_time,
        recent_scoring_fill_keys=retained_keys,
        deduped_raw_count=len(indexed_new),
        normalized_fills_count=len(normalized),
        warnings=[*norm_warnings, *trades_res.warnings],
    )

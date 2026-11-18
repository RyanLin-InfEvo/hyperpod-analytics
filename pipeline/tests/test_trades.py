# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit and property-based tests for round-trip trade reconstruction (§5.2–§5.3)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hyperpod.client.info_api import normalize_fills
from hyperpod.models.api import RawFill
from hyperpod.scoring.trades import (
    ClosedTrade,
    ingest_raw_fills_incremental,
    quantize_size,
    reconstruct_trades_incremental,
)

USER_ADDR = "0x1111111111111111111111111111111111111111"


def _raw_fill(
    *,
    coin: str,
    t: int,
    dir_str: str,
    px: float,
    sz: float,
    start_pos: float,
    closed_pnl: float = 0.0,
    fee: float = 0.0,
    crossed: bool = True,
    tid: int = 1,
    side: str = "B",
) -> RawFill:
    return RawFill(
        coin=coin,
        px=f"{px}",
        sz=f"{sz}",
        side="B" if side == "B" else "A",
        time=t,
        startPosition=f"{start_pos}",
        dir=dir_str,
        closedPnl=f"{closed_pnl}",
        hash=f"0x{tid:064x}",
        oid=tid,
        crossed=crossed,
        fee=f"{fee}",
        tid=tid,
    )


def test_quantize_size_zeroes_floating_dust() -> None:
    assert quantize_size(1e-8, 5) == 0.0
    assert quantize_size(-1e-8, 5) == 0.0
    assert quantize_size(0.1 + 0.2 - 0.3, 4) == 0.0
    assert quantize_size(1.234564, 4) == 1.2346
    assert quantize_size(-0.0001, 4) == -0.0001


def test_round_trip_scale_in_partial_close_and_flip() -> None:
    # Trade 1 (BTC long): open 1.0 @ 90k -> add 1.0 @ 92k -> partial close 0.5 -> flip 1.5 -> -1.0
    # Trade 2 (BTC short): open -1.0 @ 95k -> close 1.0 @ 93k
    raws = [
        _raw_fill(
            coin="BTC",
            t=1_763_000_000_000,
            dir_str="Open Long",
            px=90_000.0,
            sz=1.0,
            start_pos=0.0,
            fee=45.0,
            crossed=True,
            tid=10,
        ),
        _raw_fill(
            coin="BTC",
            t=1_763_001_000_000,
            dir_str="Open Long",
            px=92_000.0,
            sz=1.0,
            start_pos=1.0,
            fee=-5.0,  # maker rebate
            crossed=False,
            tid=11,
        ),
        _raw_fill(
            coin="BTC",
            t=1_763_002_000_000,
            dir_str="Close Long",
            px=94_000.0,
            sz=0.5,
            start_pos=2.0,
            closed_pnl=1500.0,
            fee=23.5,
            crossed=True,
            tid=12,
        ),
        # Long > Short: startPosition=1.5, sz=2.5 -> CL 1.5 + OS 1.0
        _raw_fill(
            coin="BTC",
            t=1_763_003_000_000,
            dir_str="Long > Short",
            px=95_000.0,
            sz=2.5,
            start_pos=1.5,
            closed_pnl=6000.0,
            fee=100.0,  # 60.0 close + 40.0 open
            crossed=True,
            tid=13,
        ),
        _raw_fill(
            coin="BTC",
            t=1_763_004_000_000,
            dir_str="Close Short",
            px=93_000.0,
            sz=1.0,
            start_pos=-1.0,
            closed_pnl=2000.0,
            fee=46.5,
            crossed=True,
            tid=14,
        ),
    ]

    norm, warnings = normalize_fills(raws, USER_ADDR, main_perp_universe={"BTC", "ETH"})
    assert warnings == []

    res = reconstruct_trades_incremental(norm, sz_decimals_by_coin={"BTC": 5})
    assert res.open_trades == {}
    assert len(res.closed_trades) == 2

    t1, t2 = res.closed_trades
    assert t1.coin == "BTC"
    assert t1.side == "long"
    assert t1.open_t == 1_763_000_000_000
    assert t1.close_t == 1_763_003_000_000
    assert t1.max_notional == pytest.approx(2.0 * 92_000.0)
    # realized = 1500 + 6000 = 7500; fees = 45 - 5 + 23.5 + 60 = 123.5
    assert t1.net_pnl == pytest.approx(7376.5)

    assert t2.coin == "BTC"
    assert t2.side == "short"
    assert t2.open_t == 1_763_003_000_000
    assert t2.close_t == 1_763_004_000_000
    assert t2.max_notional == pytest.approx(1.0 * 95_000.0)
    # realized = 2000; fees = 40 + 46.5 = 86.5
    assert t2.net_pnl == pytest.approx(1913.5)

    # 5 raw wire fills -> 5 counted fills (reversal leg_idx=1 not double-counted in fill_stats)
    assert res.fill_stats_days == {"2025-11-13": (5, 1)}


def test_truncated_initial_start_position_ignored_until_zero() -> None:
    # First observed fill has startPosition = 3.0 (10k truncation).
    # Fills before position first reaches 0 must be ignored; subsequent round trip counts.
    raws = [
        _raw_fill(
            coin="ETH",
            t=1_763_000_000_000,
            dir_str="Open Long",
            px=3000.0,
            sz=2.0,
            start_pos=3.0,
            fee=3.0,
            tid=1,
        ),
        _raw_fill(
            coin="ETH",
            t=1_763_000_010_000,
            dir_str="Close Long",
            px=3100.0,
            sz=5.0,
            start_pos=5.0,
            closed_pnl=500.0,
            fee=7.5,
            tid=2,
        ),
        _raw_fill(
            coin="ETH",
            t=1_763_000_020_000,
            dir_str="Open Short",
            px=3200.0,
            sz=4.0,
            start_pos=0.0,
            fee=6.4,
            tid=3,
        ),
        _raw_fill(
            coin="ETH",
            t=1_763_000_050_000,
            dir_str="Close Short",
            px=3150.0,
            sz=4.0,
            start_pos=-4.0,
            closed_pnl=200.0,
            fee=6.3,
            tid=4,
        ),
    ]
    norm, _ = normalize_fills(raws, USER_ADDR, main_perp_universe={"ETH"})
    res = reconstruct_trades_incremental(norm, sz_decimals_by_coin={"ETH": 4})

    assert res.open_trades == {}
    assert res.saw_truncated_position is True
    assert len(res.closed_trades) == 1
    tr = res.closed_trades[0]
    assert tr.side == "short"
    assert tr.open_t == 1_763_000_020_000
    assert tr.close_t == 1_763_000_050_000
    assert tr.net_pnl == pytest.approx(200.0 - (6.4 + 6.3))


def test_cross_run_overlap_dedup_equivalence() -> None:
    raws = [
        _raw_fill(
            coin="BTC",
            t=1_763_000_000_000,
            dir_str="Open Long",
            px=90_000.0,
            sz=1.0,
            start_pos=0.0,
            fee=10.0,
            tid=101,
        ),
        _raw_fill(
            coin="BTC",
            t=1_763_000_050_000,
            dir_str="Open Long",
            px=91_000.0,
            sz=0.5,
            start_pos=1.0,
            fee=5.0,
            crossed=False,
            tid=102,
        ),
        _raw_fill(
            coin="BTC",
            t=1_763_000_120_000,
            dir_str="Close Long",
            px=93_000.0,
            sz=1.5,
            start_pos=1.5,
            closed_pnl=4000.0,
            fee=15.0,
            tid=103,
        ),
    ]

    batch = ingest_raw_fills_incremental(
        raws,
        USER_ADDR,
        main_perp_universe={"BTC"},
        sz_decimals_by_coin={"BTC": 5},
    )

    chunk1 = ingest_raw_fills_incremental(
        raws[:2],
        USER_ADDR,
        main_perp_universe={"BTC"},
        sz_decimals_by_coin={"BTC": 5},
    )
    # Chunk 2 overlaps on fill[1] (within 60s overlap window)
    chunk2 = ingest_raw_fills_incremental(
        raws[1:],
        USER_ADDR,
        fills_cursor_scoring=chunk1.fills_cursor_scoring,
        recent_scoring_fill_keys=chunk1.recent_scoring_fill_keys,
        main_perp_universe={"BTC"},
        open_trades=chunk1.trades.open_trades,
        closed_trades=chunk1.trades.closed_trades,
        fill_stats_days=chunk1.trades.fill_stats_days,
        fill_notional_days=chunk1.trades.fill_notional_days,
        sz_decimals_by_coin={"BTC": 5},
        fills_history_start=chunk1.trades.fills_history_start,
        last_fill_time=chunk1.trades.last_fill_time,
    )

    assert chunk2.trades.open_trades == batch.trades.open_trades
    assert chunk2.trades.closed_trades == batch.trades.closed_trades
    assert chunk2.trades.fill_stats_days == batch.trades.fill_stats_days
    assert chunk2.trades.fill_notional_days == batch.trades.fill_notional_days
    assert chunk2.fills_cursor_scoring == batch.fills_cursor_scoring


@st.composite
def _fill_stream_strategy(draw: st.DrawFn) -> tuple[list[RawFill], list[int]]:
    """Generate multi-coin fill streams (including reversals, same-ms fills, and truncation)."""
    base_t = 1_763_000_000_000
    coins = ["BTC", "ETH"]
    sz_dec = {"BTC": 4, "ETH": 3}

    # Optionally start BTC truncated (startPosition != 0)
    btc_truncated = draw(st.booleans())
    pos = {
        "BTC": float(draw(st.sampled_from([-2.5, 3.0]))) if btc_truncated else 0.0,
        "ETH": 0.0,
    }
    n_events = draw(st.integers(min_value=1, max_value=35))
    fills: list[RawFill] = []
    cur_t = base_t
    tid = 1000

    for _ in range(n_events):
        # Allow zero delta time to exercise intra-millisecond sequencing.
        cur_t += draw(st.sampled_from([0, 0, 1_000, 15_000, 120_000, 86_400_000]))
        coin = draw(st.sampled_from(coins))
        cur = pos[coin]
        dec = sz_dec[coin]
        px = float(draw(st.integers(min_value=100, max_value=100_000)))
        fee = round(draw(st.floats(min_value=-5.0, max_value=50.0)), 4)
        crossed = draw(st.booleans())

        if cur == 0.0:
            is_long = draw(st.booleans())
            sz = round(draw(st.integers(min_value=1, max_value=50)) * 0.1, dec)
            dir_str = "Open Long" if is_long else "Open Short"
            new_pos = quantize_size(cur + (sz if is_long else -sz), dec)
            closed_pnl = 0.0
        else:
            action = draw(st.sampled_from(["add", "partial", "close", "flip"]))
            if action == "add":
                sz = round(draw(st.integers(min_value=1, max_value=30)) * 0.1, dec)
                dir_str = "Open Long" if cur > 0 else "Open Short"
                new_pos = quantize_size(cur + (sz if cur > 0 else -sz), dec)
                closed_pnl = 0.0
            elif action == "partial" and abs(cur) >= 0.2:
                max_steps = max(1, int(round(abs(cur) * 10)) - 1)
                sz = round(draw(st.integers(min_value=1, max_value=max_steps)) * 0.1, dec)
                dir_str = "Close Long" if cur > 0 else "Close Short"
                new_pos = quantize_size(cur + (-sz if cur > 0 else sz), dec)
                closed_pnl = round(draw(st.floats(min_value=-500.0, max_value=1000.0)), 2)
            elif action == "flip":
                extra = round(draw(st.integers(min_value=1, max_value=30)) * 0.1, dec)
                sz = round(abs(cur) + extra, dec)
                dir_str = "Long > Short" if cur > 0 else "Short > Long"
                new_pos = quantize_size(-extra if cur > 0 else extra, dec)
                closed_pnl = round(draw(st.floats(min_value=-500.0, max_value=1000.0)), 2)
            else:
                sz = round(abs(cur), dec)
                dir_str = "Close Long" if cur > 0 else "Close Short"
                new_pos = 0.0
                closed_pnl = round(draw(st.floats(min_value=-500.0, max_value=1000.0)), 2)

        fills.append(
            _raw_fill(
                coin=coin,
                t=cur_t,
                dir_str=dir_str,
                px=px,
                sz=sz,
                start_pos=cur,
                closed_pnl=closed_pnl,
                fee=fee,
                crossed=crossed,
                tid=tid,
            )
        )
        pos[coin] = new_pos
        tid += 1

    split_points = sorted(
        draw(
            st.lists(
                st.integers(min_value=0, max_value=len(fills)),
                min_size=1,
                max_size=5,
            )
        )
    )
    return fills, split_points


@settings(max_examples=100, deadline=None)
@given(data=_fill_stream_strategy())
def test_incremental_vs_batch_property_equivalence(
    data: tuple[list[RawFill], list[int]],
) -> None:
    raws, split_points = data
    sz_dec = {"BTC": 4, "ETH": 3}
    universe = {"BTC", "ETH"}

    batch = ingest_raw_fills_incremental(
        raws,
        USER_ADDR,
        main_perp_universe=universe,
        sz_decimals_by_coin=sz_dec,
    )

    cursor = 0
    recent_keys: list[tuple[int, int, str, str, str]] = []
    open_trades: dict[str, object] = {}
    closed_trades: list[ClosedTrade] = []
    fill_stats: dict[str, tuple[int, int]] = {}
    fill_notional: dict[str, float] = {}
    hist_start: int | None = None
    last_fill_t: int | None = None

    for end_idx in [*split_points, len(raws)]:
        # Simulate userFillsByTime(startTime=max(0, cursor - 60_000)): returns all fills
        # up to this checkpoint whose timestamp >= max(0, cursor - overlap_ms).
        api_start_time = max(0, cursor - 60_000) if cursor > 0 else 0
        chunk = [
            (seq_idx, fill)
            for seq_idx, fill in enumerate(raws[:end_idx])
            if fill.time >= api_start_time
        ]
        step = ingest_raw_fills_incremental(
            chunk,
            USER_ADDR,
            fills_cursor_scoring=cursor,
            recent_scoring_fill_keys=recent_keys,
            overlap_ms=60_000,
            main_perp_universe=universe,
            open_trades=open_trades,  # type: ignore[arg-type]
            closed_trades=closed_trades,
            fill_stats_days=fill_stats,
            fill_notional_days=fill_notional,
            sz_decimals_by_coin=sz_dec,
            fills_history_start=hist_start,
            last_fill_time=last_fill_t,
        )
        cursor = step.fills_cursor_scoring
        recent_keys = step.recent_scoring_fill_keys
        open_trades = step.trades.open_trades  # type: ignore[assignment]
        closed_trades = step.trades.closed_trades
        fill_stats = step.trades.fill_stats_days
        fill_notional = step.trades.fill_notional_days
        hist_start = step.trades.fills_history_start
        last_fill_t = step.trades.last_fill_time

    assert set(open_trades) == set(batch.trades.open_trades)
    for coin, b_state in batch.trades.open_trades.items():
        inc_state = open_trades[coin]
        assert inc_state == b_state or (
            inc_state.openTime == b_state.openTime  # type: ignore[attr-defined]
            and inc_state.size == pytest.approx(b_state.size)  # type: ignore[attr-defined]
            and inc_state.realized == pytest.approx(b_state.realized)  # type: ignore[attr-defined]
            and inc_state.fees == pytest.approx(b_state.fees)  # type: ignore[attr-defined]
            and inc_state.maxNotional == pytest.approx(b_state.maxNotional)  # type: ignore[attr-defined]
        )

    assert len(closed_trades) == len(batch.trades.closed_trades)
    for inc_tr, b_tr in zip(closed_trades, batch.trades.closed_trades, strict=True):
        assert inc_tr.coin == b_tr.coin
        assert inc_tr.open_t == b_tr.open_t
        assert inc_tr.close_t == b_tr.close_t
        assert inc_tr.side == b_tr.side
        assert inc_tr.net_pnl == pytest.approx(b_tr.net_pnl)
        assert inc_tr.max_notional == pytest.approx(b_tr.max_notional)

    assert fill_stats == batch.trades.fill_stats_days
    assert pytest.approx(fill_notional) == batch.trades.fill_notional_days
    assert hist_start == batch.trades.fills_history_start
    assert last_fill_t == batch.trades.last_fill_time

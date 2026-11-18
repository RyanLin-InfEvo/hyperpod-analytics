# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for UTC time helpers and Stage 1 candidate discovery / prefiltering."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperpod.config import load_config
from hyperpod.discovery import (
    estimate_subaccount_equity,
    expand_subaccounts,
    load_address_list,
    prefilter_leaderboard,
)
from hyperpod.models.api import (
    ClearinghouseStateResponse,
    LeaderboardResponse,
    LeaderboardRow,
    LeaderboardWindowPerf,
    MarginSummary,
    SpotBalance,
    SpotClearinghouseStateResponse,
    SubAccountItem,
)
from hyperpod.util.time import (
    DAY_MS,
    ms_to_utc_date,
    ms_to_utc_month,
    next_utc_day_start_ms,
    recent_utc_dates,
    utc_date_to_start_ms,
    utc_day_floor_ms,
)


def _lb_row(
    address: str,
    *,
    av: float,
    all_time_pnl: float,
    month_pnl: float,
    month_vlm: float,
    display_name: str | None = None,
) -> LeaderboardRow:
    zero = LeaderboardWindowPerf(pnl="0", roi="0", vlm="0")
    return LeaderboardRow(
        ethAddress=address,
        accountValue=f"{av}",
        windowPerformances=[
            ("day", zero),
            ("week", zero),
            ("month", LeaderboardWindowPerf(pnl=f"{month_pnl}", roi="0.1", vlm=f"{month_vlm}")),
            ("allTime", LeaderboardWindowPerf(pnl=f"{all_time_pnl}", roi="0.2", vlm="1000")),
        ],
        prize=0,
        displayName=display_name,
    )


def _subaccount(
    sub_addr: str,
    master_addr: str,
    *,
    ch_av: float,
    spot_usdc: float = 0.0,
    pm_enabled: bool | None = None,
    avail_after_maint: str | None = None,
    name: str = "sub",
) -> SubAccountItem:
    ms = MarginSummary(
        accountValue=f"{ch_av}",
        totalMarginUsed="0",
        totalNtlPos="0",
        totalRawUsd=f"{ch_av}",
    )
    ch = ClearinghouseStateResponse(
        assetPositions=[],
        crossMaintenanceMarginUsed="100.0",
        crossMarginSummary=ms,
        marginSummary=ms,
        time=1_763_078_400_000,
        withdrawable=f"{ch_av}",
    )
    balances = (
        [SpotBalance(coin="USDC", token=0, total=f"{spot_usdc}", hold="0", entryNtl="0")]
        if spot_usdc != 0.0
        else []
    )
    spot = SpotClearinghouseStateResponse(
        balances=balances,
        portfolioMarginEnabled=pm_enabled,
        tokenToAvailableAfterMaintenance=(
            [(0, avail_after_maint)] if avail_after_maint is not None else None
        ),
    )
    return SubAccountItem(
        name=name,
        subAccountUser=sub_addr,
        master=master_addr,
        clearinghouseState=ch,
        spotState=spot,
    )


def test_time_helpers_roundtrip_and_windows() -> None:
    ts = utc_date_to_start_ms("2026-11-14")
    assert ms_to_utc_date(ts) == "2026-11-14"
    assert ms_to_utc_month(ts) == "2026-11"
    mid = ts + 12 * 3_600_000 + 1234
    assert utc_day_floor_ms(mid) == ts
    assert next_utc_day_start_ms(mid) == ts + DAY_MS
    assert next_utc_day_start_ms(ts) == ts + DAY_MS

    dates_3 = recent_utc_dates(mid, 3)
    assert dates_3 == {"2026-11-12", "2026-11-13", "2026-11-14"}
    assert recent_utc_dates(mid, 0) == set()

    with pytest.raises(ValueError, match="ts_ms"):
        ms_to_utc_date(-1)


def test_load_address_list_comments_dedup_and_validation(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.txt"
    assert load_address_list(missing) == []

    addr_a = "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    addr_b = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    cfg_file = tmp_path / "seeds.txt"
    cfg_file.write_text(
        f"# header comment\n\n  {addr_a}  # inline comment\n{addr_a.lower()}\n{addr_b}\n",
        encoding="utf-8",
    )
    assert load_address_list(cfg_file) == [addr_a.lower(), addr_b]

    bad_file = tmp_path / "bad.txt"
    bad_file.write_text("0x1234\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid EVM address"):
        load_address_list(bad_file)


def _checksum_upper(addr: str) -> str:
    return "0x" + addr[2:].upper()


def test_prefilter_leaderboard_boundaries_seeds_and_blocklist() -> None:
    cfg = load_config()
    a_pass_exact = "0x1111111111111111111111111111111111111111"
    a_fail_av = "0x2222222222222222222222222222222222222222"
    a_fail_alltime_zero = "0x3333333333333333333333333333333333333333"
    a_fail_month_ratio_eq = "0x4444444444444444444444444444444444444444"
    a_fail_turnover = "0x5555555555555555555555555555555555555555"
    a_seed_bypass = "0x6666666666666666666666666666666666666666"
    a_blocklisted = "0x7777777777777777777777777777777777777777"
    a_active_wl = "0x8888888888888888888888888888888888888888"
    a_hex_case = "0xAbCdEf1111111111111111111111111111111111"

    lb = LeaderboardResponse(
        leaderboardRows=[
            # Exact boundary pass: AV=1_000_000, allTime>0, month.pnl > -200_000, vlm/av == 150
            _lb_row(
                _checksum_upper(a_pass_exact),
                av=1_000_000.0,
                all_time_pnl=0.01,
                month_pnl=-199_999.99,
                month_vlm=150_000_000.0,
                display_name="ExactBoundary",
            ),
            # Mixed-case checksummed address pass
            _lb_row(
                a_hex_case,
                av=1_500_000.0,
                all_time_pnl=10_000.0,
                month_pnl=1_000.0,
                month_vlm=2_000_000.0,
            ),
            # AV just below $1M
            _lb_row(
                a_fail_av,
                av=999_999.99,
                all_time_pnl=500_000.0,
                month_pnl=10_000.0,
                month_vlm=1_000_000.0,
            ),
            # allTime PnL == 0 fails strict > 0
            _lb_row(
                a_fail_alltime_zero,
                av=2_000_000.0,
                all_time_pnl=0.0,
                month_pnl=10_000.0,
                month_vlm=1_000_000.0,
            ),
            # month.pnl == -0.2 * av fails strict > -0.2 * av
            _lb_row(
                a_fail_month_ratio_eq,
                av=2_000_000.0,
                all_time_pnl=100_000.0,
                month_pnl=-400_000.0,
                month_vlm=1_000_000.0,
            ),
            # turnover 150.01 > 150 fails
            _lb_row(
                a_fail_turnover,
                av=1_000_000.0,
                all_time_pnl=100_000.0,
                month_pnl=10_000.0,
                month_vlm=150_010_000.0,
            ),
            # Seed candidate present in leaderboard but failing Stage 1
            _lb_row(
                a_seed_bypass,
                av=500_000.0,
                all_time_pnl=50_000.0,
                month_pnl=5_000.0,
                month_vlm=100_000.0,
                display_name="SeedWhale",
            ),
            # Blocklisted candidate that would otherwise pass Stage 1
            _lb_row(
                a_blocklisted,
                av=10_000_000.0,
                all_time_pnl=2_000_000.0,
                month_pnl=500_000.0,
                month_vlm=5_000_000.0,
            ),
        ]
    )

    res = prefilter_leaderboard(
        lb,
        cfg,
        seed_addresses=[
            _checksum_upper(a_seed_bypass),
            _checksum_upper(a_blocklisted),
            a_pass_exact,
        ],
        blocklist=[_checksum_upper(a_blocklisted)],
        active_whitelist_addresses=[a_active_wl, a_pass_exact],
    )

    assert res.total_leaderboard_rows == 8
    assert res.passed_leaderboard_count == 2
    assert res.seed_added_count == 1
    assert res.active_whitelist_added_count == 1
    assert res.blocklisted_count == 1
    assert res.rejected_counts == {
        "minAccountValue": 2,  # a_fail_av + a_seed_bypass
        "minPnlAllTime": 1,
        "minPnlMonthRatio": 1,
        "maxTurnoverMonth": 1,
        "blocklist": 1,
    }

    by_addr = {c.address: c for c in res.candidates}
    assert set(by_addr) == {a_pass_exact, a_hex_case.lower(), a_seed_bypass, a_active_wl}
    assert by_addr[a_pass_exact].source == "leaderboard"
    assert by_addr[a_pass_exact].display_name == "ExactBoundary"
    assert by_addr[a_seed_bypass].source == "seed"
    assert by_addr[a_seed_bypass].leaderboard_account_value == 500_000.0
    assert by_addr[a_active_wl].source == "active_whitelist"
    assert by_addr[a_active_wl].leaderboard_account_value is None


def test_expand_subaccounts_equity_and_exclusions() -> None:
    cfg = load_config()
    master = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    sub_perp_pass = "0xabcdef1111111111111111111111111111111111"
    sub_perp_fail = "0x2222222222222222222222222222222222222222"
    sub_pm_pass = "0x3333333333333333333333333333333333333333"
    sub_blocked = "0x4444444444444444444444444444444444444444"
    sub_existing = "0x5555555555555555555555555555555555555555"

    items = [
        _subaccount(_checksum_upper(sub_perp_pass), master, ch_av=1_250_000.0, name="main_sub"),
        _subaccount(sub_perp_fail, master, ch_av=999_999.0, spot_usdc=5_000_000.0, name="tiny"),
        _subaccount(
            sub_pm_pass,
            master,
            ch_av=100_000.0,
            spot_usdc=-200_000.0,
            pm_enabled=True,
            avail_after_maint="1500000.0",
            name="pm_sub",
        ),
        _subaccount(sub_blocked, master, ch_av=3_000_000.0, name="blocked"),
        _subaccount(sub_existing, master, ch_av=3_000_000.0, name="existing"),
    ]

    assert estimate_subaccount_equity(items[1]) == 999_999.0
    assert estimate_subaccount_equity(items[2]) == 1_500_100.0

    expanded = expand_subaccounts(
        items,
        cfg,
        blocklist=[sub_blocked],
        existing_addresses=[_checksum_upper(sub_existing)],
    )
    assert [c.address for c in expanded] == [sub_perp_pass, sub_pm_pass]
    assert all(c.source == "subAccount" and c.master == master for c in expanded)
    assert expanded[0].display_name == "main_sub"
    assert expanded[1].display_name == "pm_sub"

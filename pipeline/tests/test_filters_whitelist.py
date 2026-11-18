# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit and multi-day state-transition tests for hard gates, whitelist, criteria, and coins."""

from __future__ import annotations

import pytest

from hyperpod.config import load_config
from hyperpod.models.candidates import CandidateRow
from hyperpod.models.whitelist import WhitelistMetrics
from hyperpod.scoring.filters import evaluate_gates, evaluate_role_gate
from hyperpod.scoring.trades import ClosedTrade
from hyperpod.scoring.whitelist import (
    CandidateEvaluation,
    WhitelistStateRecord,
    compute_composite_scores,
    update_criteria_state,
    update_tracked_coins,
    update_whitelist_membership,
)
from hyperpod.util.time import DAY_MS, MIN_MS, utc_date_to_start_ms


def _metrics(
    *,
    account_value: float = 5_000_000.0,
    pnl_all_time: float = 1_500_000.0,
    roi_all_time: float = 0.45,
    pnl_month: float = 200_000.0,
    max_drawdown: float = 0.15,
    profit_factor: float | None = 2.5,
    profit_factor_robust: float | None = 1.9,
    win_rate: float = 0.60,
    n_trades: int = 40,
    history_days: float = 120.0,
    fills_per_day: float = 12.0,
    median_hold_minutes: float = 240.0,
    maker_ratio: float = 0.35,
    turnover_month: float = 6.0,
    funding_share: float = 0.05,
    top_win_share: float | None = 0.20,
    hedge_ratio: float | None = 0.0,
) -> WhitelistMetrics:
    return WhitelistMetrics(
        accountValue=account_value,
        equitySource="perp+spot",
        pnlAllTime=pnl_all_time,
        roiAllTime=roi_all_time,
        pnlMonth=pnl_month,
        maxDrawdown=max_drawdown,
        profitFactor=profit_factor,
        profitFactorRobust=profit_factor_robust,
        winRate=win_rate,
        nTrades=n_trades,
        historyDays=history_days,
        fillsPerDay=fills_per_day,
        medianHoldMinutes=median_hold_minutes,
        makerRatio=maker_ratio,
        turnoverMonth=turnover_month,
        fundingShare=funding_share,
        topWinShare=top_win_share,
        hedgeRatio=hedge_ratio,
    )


def _recent_trades(now_ms: int, count: int = 10) -> list[ClosedTrade]:
    return [
        ClosedTrade(
            coin="BTC",
            open_t=now_ms - (i + 2) * DAY_MS,
            close_t=now_ms - (i + 1) * DAY_MS,
            net_pnl=1000.0,
            max_notional=100_000.0,
            side="long",
        )
        for i in range(count)
    ]


def test_evaluate_role_and_all_hard_gates() -> None:
    cfg = load_config()
    now_ms = utc_date_to_start_ms("2026-11-15")
    trades_ok = _recent_trades(now_ms, count=5)

    assert evaluate_role_gate("user", cfg) == []
    assert evaluate_role_gate("subAccount", cfg) == []
    assert evaluate_role_gate("vault", cfg) == ["role"]
    assert evaluate_role_gate("agent", cfg) == ["role"]
    assert evaluate_role_gate("missing", cfg) == ["role"]

    # Clean passing candidate (including profitFactorRobust == None zero-loss edge case)
    assert (
        evaluate_gates(
            _metrics(profit_factor=None, profit_factor_robust=None),
            cfg,
            role="user",
            truncated=True,
            fills_history_start=now_ms - 8 * DAY_MS,
            last_fill_time=now_ms - MIN_MS,
            closed_trades=trades_ok,
            now_ms=now_ms,
        )
        == []
    )

    # Individual gate rejections:
    assert evaluate_gates(
        _metrics(fills_per_day=500.1), cfg, closed_trades=trades_ok, now_ms=now_ms
    ) == ["hft"]
    assert evaluate_gates(
        _metrics(median_hold_minutes=4.9), cfg, closed_trades=trades_ok, now_ms=now_ms
    ) == ["hft"]
    assert evaluate_gates(
        _metrics(),
        cfg,
        truncated=True,
        fills_history_start=now_ms - 6 * DAY_MS,
        last_fill_time=now_ms,
        closed_trades=trades_ok,
        now_ms=now_ms,
    ) == ["hft"]

    # MM requires BOTH makerRatio > 0.9 AND turnoverMonth > 30
    assert (
        evaluate_gates(
            _metrics(maker_ratio=0.95, turnover_month=30.0),
            cfg,
            closed_trades=trades_ok,
            now_ms=now_ms,
        )
        == []
    )
    assert evaluate_gates(
        _metrics(maker_ratio=0.91, turnover_month=31.0),
        cfg,
        closed_trades=trades_ok,
        now_ms=now_ms,
    ) == ["mm"]

    assert evaluate_gates(
        _metrics(funding_share=0.51), cfg, closed_trades=trades_ok, now_ms=now_ms
    ) == ["funding"]
    assert evaluate_gates(
        _metrics(hedge_ratio=0.81), cfg, closed_trades=trades_ok, now_ms=now_ms
    ) == ["hedge"]

    # Passive: trades closed > 90 days ago do not satisfy passive gate
    old_trades = _recent_trades(now_ms - 95 * DAY_MS, count=10)
    assert evaluate_gates(_metrics(), cfg, closed_trades=old_trades, now_ms=now_ms) == ["passive"]

    assert evaluate_gates(_metrics(n_trades=19), cfg, closed_trades=trades_ok, now_ms=now_ms) == [
        "sample"
    ]
    assert evaluate_gates(
        _metrics(history_days=29.9), cfg, closed_trades=trades_ok, now_ms=now_ms
    ) == ["sample"]
    assert evaluate_gates(
        _metrics(max_drawdown=0.46), cfg, closed_trades=trades_ok, now_ms=now_ms
    ) == ["mdd"]
    assert evaluate_gates(
        _metrics(profit_factor_robust=1.49), cfg, closed_trades=trades_ok, now_ms=now_ms
    ) == ["pf"]
    assert evaluate_gates(
        _metrics(top_win_share=0.51), cfg, closed_trades=trades_ok, now_ms=now_ms
    ) == ["lucky"]


def test_compute_composite_scores_handles_single_ties_and_null_pf() -> None:
    cfg = load_config()
    a0 = "0x1111111111111111111111111111111111111111"
    a1 = "0x2222222222222222222222222222222222222222"
    a2 = "0x3333333333333333333333333333333333333333"

    # Single member -> 1.0
    assert compute_composite_scores({a0: _metrics()}, cfg) == {a0: 1.0}

    # Three members: a2 has None profitFactorRobust (top rank = 1.0) and best stats overall
    scores = compute_composite_scores(
        {
            a0: _metrics(
                account_value=2_000_000,
                roi_all_time=0.20,
                max_drawdown=0.30,
                pnl_month=50_000,
                win_rate=0.50,
                profit_factor_robust=1.6,
            ),
            a1: _metrics(
                account_value=5_000_000,
                roi_all_time=0.40,
                max_drawdown=0.20,
                pnl_month=250_000,
                win_rate=0.60,
                profit_factor_robust=2.0,
            ),
            a2: _metrics(
                account_value=10_000_000,
                roi_all_time=0.80,
                max_drawdown=0.05,
                pnl_month=800_000,
                win_rate=0.80,
                profit_factor_robust=None,
            ),
        },
        cfg,
    )
    assert scores[a0] == pytest.approx(0.0)
    assert scores[a1] == pytest.approx(0.5)
    assert scores[a2] == pytest.approx(1.0)


def test_multiday_whitelist_hysteresis_criteria_bump_and_reentry() -> None:
    cfg = load_config()
    a0 = "0x1111111111111111111111111111111111111111"
    a1 = "0x2222222222222222222222222222222222222222"
    a2 = "0x3333333333333333333333333333333333333333"
    a3 = "0x4444444444444444444444444444444444444444"
    a4 = "0x5555555555555555555555555555555555555555"

    def ev_pass(addr: str, *, pos_btc: float = 100_000.0) -> CandidateEvaluation:
        return CandidateEvaluation(
            address=addr,
            role="user",
            coverage_start=utc_date_to_start_ms("2026-10-01"),
            metrics=_metrics(),
            reject_reasons=[],
            open_positions_notional={"BTC": pos_btc},
        )

    def ev_fail(addr: str) -> CandidateEvaluation:
        return CandidateEvaluation(
            address=addr,
            role="user",
            coverage_start=utc_date_to_start_ms("2026-10-01"),
            metrics=_metrics(profit_factor_robust=1.1),
            reject_reasons=["pf"],
            open_positions_notional={"BTC": 50_000.0},
        )

    d1 = utc_date_to_start_ms("2026-11-10")
    d2 = utc_date_to_start_ms("2026-11-11")
    d3 = utc_date_to_start_ms("2026-11-12")
    d4 = utc_date_to_start_ms("2026-11-13")
    d5 = utc_date_to_start_ms("2026-11-14")
    d6 = utc_date_to_start_ms("2026-11-15")

    # Day 1: 4 addresses pass -> id 0, 1, 2, 3 under criteriaVersion=1
    r1 = update_whitelist_membership(
        None,
        [ev_pass(a0), ev_pass(a1), ev_pass(a2), ev_pass(a3)],
        cfg,
        eval_ts_ms=d1,
    )
    assert r1.criteria.current.version == 1
    assert [a.id for a in r1.whitelist.addresses] == [0, 1, 2, 3]
    assert r1.newly_entered == [a0, a1, a2, a3]

    # Day 2: a2 fails 1st day (failStreak=1 < 3 -> stays active)
    r2 = update_whitelist_membership(
        r1.state,
        [ev_pass(a0), ev_pass(a1), ev_fail(a2), ev_pass(a3)],
        cfg,
        eval_ts_ms=d2,
    )
    assert r2.state.addresses[a2].failStreak == 1
    assert r2.state.addresses[a2].is_active
    assert r2.newly_exited == []

    # Day 3: a2 recovers -> failStreak resets to 0; a3 fails 1st day
    r3 = update_whitelist_membership(
        r2.state,
        [ev_pass(a0), ev_pass(a1), ev_pass(a2), ev_fail(a3)],
        cfg,
        eval_ts_ms=d3,
    )
    assert r3.state.addresses[a2].failStreak == 0
    assert r3.state.addresses[a3].failStreak == 1

    # Day 4: criteria threshold changes (pf.minRobust 1.5 -> 1.6 -> criteriaVersion=2);
    # a3 fails 2nd day
    cfg_v2 = cfg.model_copy(deep=True)
    cfg_v2.gates.pf.minRobust.value = 1.6
    r4 = update_whitelist_membership(
        r3.state,
        [ev_pass(a0), ev_pass(a1), ev_pass(a2), ev_fail(a3)],
        cfg_v2,
        eval_ts_ms=d4,
    )
    assert r4.criteria.current.version == 2
    assert len(r4.criteria.history) == 1
    assert r4.criteria.history[0].version == 1
    assert [c.key for c in r4.criteria.history[0].changes] == ["pf.minRobust"]
    assert r4.criteria.history[0].changes[0].from_ == 1.5
    assert r4.criteria.history[0].changes[0].to == 1.6
    assert r4.state.addresses[a0].membership == [(d1, d4, 1), (d4, None, 2)]
    assert r4.state.addresses[a3].failStreak == 2
    assert r4.state.addresses[a3].is_active

    # Day 5: a3 fails 3rd consecutive day -> exits; new a4 enters with id=4 (a3 keeps id=3)
    r5 = update_whitelist_membership(
        r4.state,
        [ev_pass(a0), ev_pass(a1), ev_pass(a2), ev_fail(a3), ev_pass(a4)],
        cfg_v2,
        eval_ts_ms=d5,
    )
    assert r5.newly_exited == [a3]
    assert r5.newly_entered == [a4]
    assert not r5.state.addresses[a3].is_active
    assert r5.state.addresses[a3].id == 3
    assert r5.state.addresses[a3].membership[-1][1] == d5
    assert r5.state.addresses[a4].id == 4
    assert r5.active_count == 4

    # Day 6: a3 passes again -> re-enters preserving id=3 and appending a new membership interval
    r6 = update_whitelist_membership(
        r5.state,
        [ev_pass(a0), ev_pass(a1), ev_pass(a2), ev_pass(a3), ev_pass(a4)],
        cfg_v2,
        eval_ts_ms=d6,
    )
    assert r6.newly_entered == [a3]
    assert r6.state.addresses[a3].id == 3
    assert r6.state.addresses[a3].failStreak == 0
    assert r6.state.addresses[a3].membership[-1] == (d6, None, 2)
    assert len(r6.whitelist.addresses) == 5


def test_dynamic_coin_lifecycle_sticky_cap_and_reentry() -> None:
    cfg = load_config().model_copy(deep=True)
    cfg.coins.dynamicTopN.value = 2
    cfg.coins.stickyDays.value = 2
    cfg.coins.maxTracked.value = 6  # 4 pinned + up to 2 dynamic

    d1 = utc_date_to_start_ms("2026-11-01")
    coins, sticky, queue, last_d = update_tracked_coins(
        [],
        {},
        [],
        active_coin_notionals={"BTC": 1e7, "DOGE": 3e6, "WIF": 2e6, "ARB": 1e6},
        cfg=cfg,
        eval_ts_ms=d1,
    )
    active = {c.coin: c for c in coins if c.trackedTo is None}
    assert set(active) == {"BTC", "ETH", "SOL", "HYPE", "DOGE", "WIF"}
    assert active["DOGE"].trackedFrom == d1
    assert queue == ["DOGE", "WIF"]
    assert last_d == "2026-11-01"

    # Day 2: WIF drops out of top 2 (SUI replaces it -> 4 pinned + DOGE + WIF + SUI = 7 > cap 6).
    # WIF has 0 notional -> evicted immediately at 00:00 next day; SUI enters!
    d2 = d1 + DAY_MS
    coins, sticky, queue, last_d = update_tracked_coins(
        coins,
        sticky,
        queue,
        active_coin_notionals={"DOGE": 3e6, "SUI": 2.5e6},
        cfg=cfg,
        eval_ts_ms=d2,
        last_update_date=last_d,
    )
    active = {c.coin: c for c in coins if c.trackedTo is None}
    assert set(active) == {"BTC", "ETH", "SOL", "HYPE", "DOGE", "SUI"}
    assert active["SUI"].trackedFrom == d2 + DAY_MS
    closed_wif = [c for c in coins if c.coin == "WIF" and c.trackedTo is not None]
    assert len(closed_wif) == 1
    assert closed_wif[0].trackedTo == d2 + DAY_MS

    # Now expand maxTracked=7 and simulate DOGE dropping out for 3 days (stickyDays=2):
    # Day 3: sticky=1 (stays), Day 4: sticky=2 (stays), Day 5: sticky=3 > 2 -> stops!
    cfg.coins.maxTracked.value = 7
    for offset in (2, 3, 4):
        d = d1 + offset * DAY_MS
        coins, sticky, queue, last_d = update_tracked_coins(
            coins,
            sticky,
            queue,
            active_coin_notionals={"SUI": 5e6, "ARB": 4e6},
            cfg=cfg,
            eval_ts_ms=d,
            last_update_date=last_d,
        )
    assert all(c.trackedTo is None for c in coins if c.coin == "DOGE") is False
    doge_tracks = [c for c in coins if c.coin == "DOGE"]
    assert len(doge_tracks) == 1
    assert doge_tracks[0].trackedTo == d1 + 5 * DAY_MS

    # Day 6: DOGE returns to top 2 -> opens a second CoinTrack segment
    d6 = d1 + 5 * DAY_MS
    coins, sticky, queue, last_d = update_tracked_coins(
        coins,
        sticky,
        queue,
        active_coin_notionals={"DOGE": 9e6, "SUI": 5e6},
        cfg=cfg,
        eval_ts_ms=d6,
        last_update_date=last_d,
    )
    doge_tracks = [c for c in coins if c.coin == "DOGE"]
    assert len(doge_tracks) == 2
    assert doge_tracks[1].trackedFrom == d6 + DAY_MS
    assert doge_tracks[1].trackedTo is None


def test_safety_cap_warning_without_truncation() -> None:
    cfg = load_config().model_copy(deep=True)
    cfg.membership.safetyCapWarning.value = 3
    now_ms = utc_date_to_start_ms("2026-11-15")
    evals = [
        CandidateEvaluation(
            address=f"0x{idx + 1:040x}",
            role="user",
            coverage_start=now_ms - 60 * DAY_MS,
            metrics=_metrics(),
            reject_reasons=[],
        )
        for idx in range(4)
    ]
    res = update_whitelist_membership(WhitelistStateRecord(), evals, cfg, eval_ts_ms=now_ms)
    assert res.active_count == 4
    assert len(res.whitelist.addresses) == 4
    assert any("safetyCapWarning" in w for w in res.warnings)

    # Also validate update_criteria_state directly for rejectedBy filtering
    crit = update_criteria_state(
        cfg,
        None,
        eval_ts_ms=now_ms,
        candidates=[
            CandidateRow(
                idx=0,
                passed=True,
                rejectReasons=[],
                fillsPerDay=10.0,
                makerRatio=0.3,
                medianHoldMinutes=100.0,
                maxDrawdown=0.1,
                profitFactorRobust=2.0,
                turnoverMonth=3.0,
            )
        ],
    )
    assert crit.current.stats.passed == 1
    assert crit.current.stats.rejectedBy == {}


def test_blocklisted_evaluation_is_excluded_from_candidates_and_stats() -> None:
    cfg = load_config()
    now_ms = utc_date_to_start_ms("2026-11-15")
    a_pass = "0x1111111111111111111111111111111111111111"
    a_blocked = "0x2222222222222222222222222222222222222222"

    res = update_whitelist_membership(
        None,
        [
            CandidateEvaluation(address=a_pass, role="user", metrics=_metrics()),
            CandidateEvaluation(address=a_blocked, role="user", metrics=_metrics()),
        ],
        cfg,
        eval_ts_ms=now_ms,
        blocklist=[a_blocked],
    )
    assert [a.address for a in res.whitelist.addresses] == [a_pass]
    assert len(res.candidates.candidates) == 1
    assert res.criteria.current.stats.candidates == 1
    assert res.criteria.current.stats.passed == 1


def test_same_day_dynamic_coin_eviction_does_not_emit_zero_duration_track() -> None:
    cfg = load_config().model_copy(deep=True)
    cfg.coins.dynamicTopN.value = 1
    cfg.coins.maxTracked.value = 5  # 4 pinned + 1 dynamic

    d1 = utc_date_to_start_ms("2026-11-01")
    d2 = d1 + DAY_MS
    # Day 1 initializes DOGE as dynamic coin.
    coins, sticky, queue, last_d = update_tracked_coins(
        [],
        {},
        [],
        active_coin_notionals={"DOGE": 3e6},
        cfg=cfg,
        eval_ts_ms=d1,
    )
    # Day 2 run 1: SUI beats DOGE -> SUI scheduled for d2 + DAY_MS, DOGE evicted? Wait:
    # Expand maxTracked=6 for run 1 so SUI is scheduled for d2 + DAY_MS alongside DOGE.
    cfg.coins.maxTracked.value = 6
    coins, sticky, queue, last_d = update_tracked_coins(
        coins,
        sticky,
        queue,
        active_coin_notionals={"SUI": 4e6, "DOGE": 3e6},
        cfg=cfg,
        eval_ts_ms=d2 + 3_600_000,
        last_update_date=last_d,
    )
    assert any(c.coin == "SUI" and c.trackedFrom == d2 + DAY_MS for c in coins)

    # Day 2 same-day re-run with cap tightened back to 5 and ARB overtaking SUI:
    # SUI hasn't started yet (trackedFrom == d2 + DAY_MS == effective_ts) -> must be removed,
    # NOT closed with trackedFrom == trackedTo!
    cfg.coins.maxTracked.value = 5
    coins, sticky, queue, last_d = update_tracked_coins(
        coins,
        sticky,
        queue,
        active_coin_notionals={"ARB": 9e6},
        cfg=cfg,
        eval_ts_ms=d2 + 7_200_000,
        last_update_date=last_d,
    )
    assert all(c.trackedTo is None or c.trackedTo > c.trackedFrom for c in coins)
    assert [c for c in coins if c.coin == "SUI"] == []

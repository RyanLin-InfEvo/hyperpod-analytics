# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-calculated unit tests (>= 3 per metric) for §5.4 scoring metrics."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from hyperpod.models.api import (
    AssetPositionDetail,
    AssetPositionItem,
    ClearinghouseStateResponse,
    CumFunding,
    MarginSummary,
    PortfolioResponse,
    PortfolioWindowMetrics,
    PositionLeverage,
    SpotBalance,
    SpotClearinghouseStateResponse,
)
from hyperpod.scoring.metrics import (
    build_whitelist_metrics,
    compute_equity,
    compute_fills_per_day,
    compute_funding_share,
    compute_hedge_ratio,
    compute_history_days,
    compute_maker_ratio,
    compute_max_drawdown,
    compute_median_hold_minutes,
    compute_n_trades,
    compute_profit_factor,
    compute_profit_factor_robust,
    compute_top_win_share,
    compute_turnover_month,
    compute_win_rate,
    derive_portfolio_fallbacks,
)
from hyperpod.scoring.trades import ClosedTrade
from hyperpod.util.time import DAY_MS, MIN_MS, utc_date_to_start_ms

FIXTURES_API = Path(__file__).resolve().parents[2] / "fixtures" / "api"


def _trade(net_pnl: float, *, open_t: int = 1000, hold_min: int = 60) -> ClosedTrade:
    return ClosedTrade(
        coin="BTC",
        open_t=open_t,
        close_t=open_t + hold_min * MIN_MS,
        net_pnl=net_pnl,
        max_notional=100_000.0,
        side="long",
    )


def _portfolio_window(
    points: list[tuple[int, float, float]], vlm: float = 0.0
) -> PortfolioResponse:
    av_hist = [(t, f"{av}") for t, av, _ in points]
    pnl_hist = [(t, f"{pnl}") for t, _, pnl in points]
    win = PortfolioWindowMetrics(accountValueHistory=av_hist, pnlHistory=pnl_hist, vlm=f"{vlm}")
    return PortfolioResponse(
        windows={
            "day": win,
            "week": win,
            "month": win,
            "allTime": win,
            "perpDay": win,
            "perpWeek": win,
            "perpMonth": win,
            "perpAllTime": win,
        }
    )


def _clearinghouse(
    ch_av: float,
    *,
    c_mm: float = 0.0,
    positions: Sequence[tuple[str, float, float, float]] = (),
) -> ClearinghouseStateResponse:
    ms = MarginSummary(
        accountValue=f"{ch_av}",
        totalMarginUsed="0",
        totalNtlPos="0",
        totalRawUsd=f"{ch_av}",
    )
    items = [
        AssetPositionItem(
            type="oneWay",
            position=AssetPositionDetail(
                coin=coin,
                cumFunding=CumFunding(allTime="0", sinceOpen="0", sinceChange="0"),
                entryPx=f"{px}",
                leverage=PositionLeverage(type="cross", value=5),
                marginUsed="100",
                maxLeverage=20,
                positionValue=f"{ntl}",
                returnOnEquity="0",
                szi=f"{szi}",
                unrealizedPnl="0",
            ),
        )
        for coin, szi, px, ntl in positions
    ]
    return ClearinghouseStateResponse(
        assetPositions=items,
        crossMaintenanceMarginUsed=f"{c_mm}",
        crossMarginSummary=ms,
        marginSummary=ms,
        time=1_763_078_400_000,
        withdrawable="0",
    )


def _spot(
    balances: Sequence[tuple[str, int, float]] = (),
    *,
    avail_after_maint_usdc: float | None = None,
) -> SpotClearinghouseStateResponse:
    return SpotClearinghouseStateResponse(
        balances=[
            SpotBalance(coin=c, token=tok, total=f"{tot}", hold="0", entryNtl="0")
            for c, tok, tot in balances
        ],
        tokenToAvailableAfterMaintenance=(
            [(0, f"{avail_after_maint_usdc}")] if avail_after_maint_usdc is not None else None
        ),
    )


def test_n_trades_and_win_rate_cases() -> None:
    # 1. Empty trades
    assert compute_n_trades([]) == 0
    assert compute_win_rate([]) == 0.0
    # 2. Breakeven (0.0) does not count as > 0 win
    trades_mixed = [_trade(500.0), _trade(0.0), _trade(-200.0), _trade(100.0)]
    assert compute_n_trades(trades_mixed) == 4
    assert compute_win_rate(trades_mixed) == pytest.approx(0.5)
    # 3. All losing
    trades_loss = [_trade(-10.0), _trade(-20.0)]
    assert compute_n_trades(trades_loss) == 2
    assert compute_win_rate(trades_loss) == 0.0


def test_profit_factor_and_robust_cases() -> None:
    # 1. No winning trades -> 0.0 for both
    assert compute_profit_factor([]) == 0.0
    assert compute_profit_factor_robust([]) == 0.0
    assert compute_profit_factor([_trade(-50.0)]) == 0.0
    assert compute_profit_factor_robust([_trade(-50.0)]) == 0.0

    # 2. Single winning trade, zero loss -> PF is None (inf), robust removes the only trade -> 0.0
    assert compute_profit_factor([_trade(1000.0)]) is None
    assert compute_profit_factor_robust([_trade(1000.0)]) == 0.0

    # 3. Two winning trades, zero loss -> both PF and robust are None
    assert compute_profit_factor([_trade(1000.0), _trade(200.0)]) is None
    assert compute_profit_factor_robust([_trade(1000.0), _trade(200.0)]) is None

    # 4. Mixed wins & losses: W=1500 (1000+300+200), L=250 -> PF=6.0; remove 1000 -> 500/250=2.0
    trades = [_trade(1000.0), _trade(300.0), _trade(200.0), _trade(-150.0), _trade(-100.0)]
    assert compute_profit_factor(trades) == pytest.approx(6.0)
    assert compute_profit_factor_robust(trades) == pytest.approx(2.0)


def test_top_win_share_cases() -> None:
    # 1. No wins
    assert compute_top_win_share([_trade(-100.0), _trade(0.0)]) == 0.0
    # 2. Single win
    assert compute_top_win_share([_trade(400.0), _trade(-50.0)]) == pytest.approx(1.0)
    # 3. Multiple wins: max=600, total W=1000 -> 0.60
    assert compute_top_win_share([_trade(600.0), _trade(300.0), _trade(100.0)]) == pytest.approx(
        0.6
    )


def test_max_drawdown_cases() -> None:
    # 1. Empty / all non-positive AV points -> 0.0
    assert compute_max_drawdown(_portfolio_window([])) == 0.0
    assert compute_max_drawdown(_portfolio_window([(1, 0.0, 0.0), (2, 0.0, -50.0)])) == 0.0

    # 2. Monotonic climb -> 0.0
    pf_climb = _portfolio_window([(1, 100_000, 0), (2, 120_000, 20_000), (3, 150_000, 50_000)])
    assert compute_max_drawdown(pf_climb) == 0.0

    # 3. Withdrawal invariance: AV drops from 10M to 2.5M on withdrawal while PnL rises,
    # then PnL drops 500k from peak 5.5M -> 5.0M relative to AV at peak (2.5M) => DD = 0.20
    pf_wd = _portfolio_window(
        [
            (1, 10_000_000, 5_000_000),  # peak PnL = 5M, AV_argmax = 10M
            (2, 2_500_000, 5_500_000),  # withdrew 8M, PnL rose to 5.5M (new peak, peak_cap=2.5M)
            (3, 2_000_000, 5_000_000),  # DD = (5.5M - 5.0M) / 2.5M = 0.20
        ]
    )
    assert compute_max_drawdown(pf_wd) == pytest.approx(0.20)

    # 4. Opening (AV=0, PnL=0) + day-1 loss -> captures true loss from initial principal
    pf_open_loss = _portfolio_window([(1, 0.0, 0.0), (2, 1_000.0, -100.0), (3, 500.0, -600.0)])
    assert compute_max_drawdown(pf_open_loss) == pytest.approx(600.0 / 1_100.0)
    pf_open_50 = _portfolio_window([(1, 0.0, 0.0), (2, 900.0, -100.0), (3, 500.0, -500.0)])
    assert compute_max_drawdown(pf_open_50) == pytest.approx(0.50)

    # 5. Small test -> $10M deposit -> 20% loss -> full withdrawal (AV=0) -> 0.20
    pf_scale_withdraw = _portfolio_window(
        [
            (1, 0.0, 0.0),
            (2, 100.0, 10.0),
            (3, 10_000_100.0, 10.0),
            (4, 8_000_100.0, -1_999_990.0),
            (5, 0.0, -1_999_990.0),
        ]
    )
    assert compute_max_drawdown(pf_scale_withdraw) == pytest.approx(2_000_000.0 / 10_000_100.0)

    # 6. Real 90% blowup ($1M -> $100k) -> next-week $10M top-up -> retains 0.90
    pf_blowup = _portfolio_window(
        [
            (1, 0.0, 0.0),
            (2, 1_000_000.0, 50_000.0),
            (3, 100_000.0, -850_000.0),
            (4, 10_100_000.0, -850_000.0),
        ]
    )
    assert compute_max_drawdown(pf_blowup) == pytest.approx(0.90)

    # 7. Negative intermediate AV clamped strictly within [0.0, 1.0]
    pf_neg_av = _portfolio_window(
        [
            (1, 100.0, 100.0),
            (2, -50.0, -50.0),  # dd_usd=150, max(av,0)+dd_usd=150 -> DD=1.0
        ]
    )
    res_neg = compute_max_drawdown(pf_neg_av)
    assert 0.0 <= res_neg <= 1.0
    assert res_neg == pytest.approx(1.0)

    # 8. Real API fixtures: 0x1f250df5 (day-1 loss) & 0xfc27136e ($1.42 dust scale-up)
    pf_1f2 = PortfolioResponse.model_validate(
        json.loads((FIXTURES_API / "portfolio_0x1f250df5.json").read_text(encoding="utf-8"))
    )
    assert compute_max_drawdown(pf_1f2) == pytest.approx(0.9567, abs=1e-3)

    pf_fc2 = PortfolioResponse.model_validate(
        json.loads((FIXTURES_API / "portfolio_0xfc27136e.json").read_text(encoding="utf-8"))
    )
    assert compute_max_drawdown(pf_fc2) == pytest.approx(0.3844, abs=1e-3)

    # 9. Initial day-1 loss from (0,0) followed by new peak and 25% drawdown
    pf_recover = _portfolio_window(
        [
            (1, 0.0, 0.0),
            (2, 1_000_000.0, -100_000.0),  # initial loss DD = 100k / 1.1M = 0.0909
            (3, 2_000_000.0, 500_000.0),  # new peak with AV = 2M
            (4, 1_500_000.0, 0.0),  # DD = 500k / 2M = 0.25
        ]
    )
    assert compute_max_drawdown(pf_recover) == pytest.approx(0.25)


def test_history_days_and_median_hold_minutes_cases() -> None:
    now = 1_763_000_000_000 + 45 * DAY_MS + DAY_MS // 2
    # historyDays: None, exact 0, 45.5 days
    assert compute_history_days(None, now) == 0.0
    assert compute_history_days(now, now) == 0.0
    assert compute_history_days(1_763_000_000_000, now) == pytest.approx(45.5)

    # medianHoldMinutes: empty, odd, even
    assert compute_median_hold_minutes([]) == 0.0
    assert compute_median_hold_minutes(
        [_trade(1, hold_min=10), _trade(1, hold_min=90), _trade(1, hold_min=30)]
    ) == pytest.approx(30.0)
    assert compute_median_hold_minutes(
        [
            _trade(1, hold_min=10),
            _trade(1, hold_min=20),
            _trade(1, hold_min=40),
            _trade(1, hold_min=100),
        ]
    ) == pytest.approx(30.0)


def test_fills_per_day_and_maker_ratio_cases() -> None:
    now_ms = utc_date_to_start_ms("2026-11-30") + 12 * 3_600_000
    # 1. Empty stats
    assert compute_fills_per_day({}, now_ms, window_days=30) == 0.0
    assert compute_maker_ratio({}, now_ms, window_days=30) == 0.0

    # 2. Only out-of-window data (> 30 days ago)
    old_stats = {"2026-10-15": (100, 50)}
    assert compute_fills_per_day(old_stats, now_ms, window_days=30) == 0.0
    assert compute_maker_ratio(old_stats, now_ms, window_days=30) == 0.0

    # 3. Active days only denominator (zero-fill day ignored) + maker ratio across 30d window
    mixed_stats = {
        "2026-10-31": (999, 999),  # outside 30-day window (2026-11-01..2026-11-30)
        "2026-11-01": (30, 15),
        "2026-11-10": (0, 0),  # inactive UTC day must not dilute fillsPerDay denominator
        "2026-11-30": (10, 5),
    }
    assert compute_fills_per_day(mixed_stats, now_ms, window_days=30) == pytest.approx(20.0)
    assert compute_maker_ratio(mixed_stats, now_ms, window_days=30) == pytest.approx(20 / 40)


def test_turnover_month_and_funding_share_cases() -> None:
    now_ms = utc_date_to_start_ms("2026-11-30")
    pf = _portfolio_window([(1, 1_000_000, 10_000)], vlm=9_000_000.0)

    # turnoverMonth: 1) AV <= 0 -> 0.0; 2) leaderboard vlm; 3) 30d notional fallback; 4) portfolio
    assert compute_turnover_month(0.0, leaderboard_vlm_month=5_000_000.0) == 0.0
    assert compute_turnover_month(2_000_000.0, leaderboard_vlm_month=12_000_000.0) == pytest.approx(
        6.0
    )
    assert compute_turnover_month(
        2_000_000.0,
        fill_notional_days={"2026-10-01": 99_000_000.0, "2026-11-29": 8_000_000.0},
        now_ms=now_ms,
        window_days=30,
    ) == pytest.approx(4.0)
    assert compute_turnover_month(3_000_000.0, portfolio=pf) == pytest.approx(3.0)

    # fundingShare: 1) zero; 2) alternating net cancellation; 3) pnlAllTime < 1 floor
    assert compute_funding_share(0.0, 500_000.0) == 0.0
    assert compute_funding_share(-20_000.0, 400_000.0) == pytest.approx(0.05)
    assert compute_funding_share(-12.5, 0.25) == pytest.approx(12.5)


def test_hedge_ratio_cases() -> None:
    # 1. Long-only or no spot -> 0.0
    ch_long = _clearinghouse(1_000_000.0, positions=[("BTC", 2.0, 90_000.0, 180_000.0)])
    assert compute_hedge_ratio(ch_long, _spot([("UBTC", 100, 5.0)])) == 0.0
    ch_short_btc = _clearinghouse(1_000_000.0, positions=[("BTC", -2.0, 90_000.0, 180_000.0)])
    assert compute_hedge_ratio(ch_short_btc, None) == 0.0

    # 2. Exact Unit-alias spot hedge + HIP-3 short ignored
    ch_multi = _clearinghouse(
        2_000_000.0,
        positions=[
            ("BTC", -2.0, 90_000.0, 180_000.0),  # 1.0 UBTC spot -> 50% hedged, notional 180k
            (
                "FARTCOIN",
                -100_000.0,
                1.2,
                120_000.0,
            ),  # 150k UFART spot -> 100% capped, notional 120k
            ("xyz:NVDA", -500.0, 100.0, 50_000.0),  # HIP-3 excluded
        ],
    )
    spot = _spot([("UBTC", 197, 1.0), ("UFART", 269, 150_000.0)])
    expected = (180_000.0 * 0.5 + 120_000.0 * 1.0) / 300_000.0
    assert compute_hedge_ratio(ch_multi, spot) == pytest.approx(expected)

    # 3. Unhedged perp short -> 0.0
    assert compute_hedge_ratio(ch_short_btc, _spot([("USDC", 0, 500_000.0)])) == 0.0


def test_equity_and_portfolio_fallbacks_cases() -> None:
    ch = _clearinghouse(1_000_000.0, c_mm=50_000.0)

    # equity 1) disabled ignores spot USDC; 2) unified non-double-counting + idle spot;
    # 3) portfolioMargin negative USDC; 4) unrecognized abstraction warns
    eq_dis, src_dis, w_dis = compute_equity(ch, "disabled", _spot([("USDC", 0, 5_000_000.0)]))
    assert (eq_dis, src_dis, w_dis) == (1_000_000.0, "perp", [])

    eq_uni, src_uni, w_uni = compute_equity(ch, "unifiedAccount", _spot([("USDC", 0, 1_400_000.0)]))
    assert (eq_uni, src_uni, w_uni) == (1_400_000.0, "perp+spot", [])

    eq_pm, src_pm, _ = compute_equity(
        ch,
        "portfolioMargin",
        _spot([("USDC", 0, -300_000.0)], avail_after_maint_usdc=2_200_000.0),
    )
    assert (eq_pm, src_pm) == (2_250_000.0, "perp+spot")

    _, src_unk, w_unk = compute_equity(ch, "futureMode", _spot([("USDC", 0, 1_100_000.0)]))
    assert src_unk == "perp+spot"
    assert len(w_unk) == 1

    # Portfolio fallbacks (缺欄推算)
    pf_all = PortfolioWindowMetrics(
        accountValueHistory=[(1, "1000000"), (2, "1800000")],
        pnlHistory=[(1, "0"), (2, "600000")],  # peak net principal = max(1M, 1.2M) = 1.2M
        vlm="5000000",
    )
    pf_month = PortfolioWindowMetrics(
        accountValueHistory=[(1, "1700000"), (2, "1800000")],
        pnlHistory=[(1, "0"), (2, "150000")],
        vlm="2000000",
    )
    pf = PortfolioResponse(
        windows={
            "day": pf_month,
            "week": pf_month,
            "month": pf_month,
            "allTime": pf_all,
            "perpDay": pf_month,
            "perpWeek": pf_month,
            "perpMonth": pf_month,
            "perpAllTime": pf_all,
        }
    )
    # 1) Leaderboard present -> uses leaderboard fields
    assert derive_portfolio_fallbacks(
        pf,
        1_800_000.0,
        leaderboard_pnl_all_time=999.0,
        leaderboard_roi_all_time=0.55,
        leaderboard_pnl_month=111.0,
    ) == (999.0, 0.55, 111.0)
    # 2) Absent -> derives from perpAllTime / perpMonth (600k / 1.2M = 0.50)
    pnl_all, roi_all, pnl_m = derive_portfolio_fallbacks(pf, 1_800_000.0)
    assert pnl_all == pytest.approx(600_000.0)
    assert roi_all == pytest.approx(0.50)
    assert pnl_m == pytest.approx(150_000.0)
    # 3) Negative net principal -> uses accountValue denominator fallback
    pf_neg_principal = _portfolio_window([(1, 100_000.0, 500_000.0)], vlm=100_000.0)
    pnl_a3, roi_a3, _ = derive_portfolio_fallbacks(pf_neg_principal, 2_000_000.0)
    assert pnl_a3 == pytest.approx(500_000.0)
    assert roi_a3 == pytest.approx(0.25)


def test_build_whitelist_metrics_with_real_fixture() -> None:
    ch = ClearinghouseStateResponse.model_validate(
        json.loads((FIXTURES_API / "clearinghouseState_0xbf732ea0.json").read_text("utf-8"))
    )
    spot = SpotClearinghouseStateResponse.model_validate(
        json.loads((FIXTURES_API / "spotClearinghouseState_0xbf732ea0.json").read_text("utf-8"))
    )
    pf = PortfolioResponse.model_validate(
        json.loads((FIXTURES_API / "portfolio_0xbf732ea0.json").read_text("utf-8"))
    )
    abstraction = json.loads((FIXTURES_API / "userAbstraction_0xbf732ea0.json").read_text("utf-8"))
    now_ms = utc_date_to_start_ms("2026-11-15")
    trades = [_trade(50_000.0, hold_min=180), _trade(30_000.0, hold_min=240), _trade(-10_000.0)]

    metrics, warnings = build_whitelist_metrics(
        clearinghouse=ch,
        abstraction=abstraction,
        spot=spot,
        portfolio=pf,
        closed_trades=trades,
        fill_stats_days={"2026-11-14": (20, 8), "2026-11-15": (10, 4)},
        fill_notional_days={"2026-11-14": 4_000_000.0},
        fills_history_start=now_ms - 100 * DAY_MS,
        cum_funding_net=-20_000.0,
        now_ms=now_ms,
        main_perp_universe={"BTC", "ETH", "SOL", "HYPE"},
    )
    assert warnings == []
    assert metrics.equitySource == "perp+spot"
    assert metrics.accountValue > 1_000_000.0
    assert metrics.nTrades == 3
    assert metrics.profitFactor == pytest.approx(8.0)
    assert metrics.profitFactorRobust == pytest.approx(3.0)
    assert metrics.fillsPerDay == pytest.approx(15.0)
    assert metrics.makerRatio == pytest.approx(12 / 30)

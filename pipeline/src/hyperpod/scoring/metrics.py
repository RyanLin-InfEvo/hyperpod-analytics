# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stage 2 scoring metrics and boundary rules (§5.4, ADR-0001, ADR-0003)."""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence

from hyperpod.client.info_api import compute_account_equity, is_main_perp_coin
from hyperpod.models.api import (
    ClearinghouseStateResponse,
    PortfolioResponse,
    PortfolioWindowMetrics,
    SpotClearinghouseStateResponse,
)
from hyperpod.models.whitelist import EquitySource, WhitelistMetrics
from hyperpod.scoring.trades import ClosedTrade
from hyperpod.util.time import DAY_MS, MIN_MS, recent_utc_dates

_SPOT_ALIASES: dict[str, tuple[str, ...]] = {
    "FARTCOIN": ("FARTCOIN", "UFART"),
    "XRP": ("XRP", "FXRP", "UXRP"),
    "kBONK": ("kBONK", "BONK", "UBONK"),
    "kPEPE": ("kPEPE", "PEPE", "UPEPE"),
}


def compute_n_trades(closed_trades: Sequence[ClosedTrade]) -> int:
    """Return the count of completed round-trip trades (§5.4)."""
    return len(closed_trades)


def compute_win_rate(closed_trades: Sequence[ClosedTrade]) -> float:
    """Return ``count(netPnl > 0) / nTrades``, or ``0.0`` when ``nTrades == 0`` (§5.4)."""
    n = len(closed_trades)
    if n == 0:
        return 0.0
    wins = sum(1 for trade in closed_trades if trade.net_pnl > 0.0)
    return wins / n


def compute_profit_factor(closed_trades: Sequence[ClosedTrade]) -> float | None:
    """Return ``W / L`` where ``None`` represents infinite profit factor (``W > 0, L == 0``).

    Returns ``0.0`` when gross winning PnL ``W == 0`` (§5.4).
    """
    gross_win = sum(t.net_pnl for t in closed_trades if t.net_pnl > 0.0)
    if gross_win <= 0.0:
        return 0.0
    gross_loss_abs = abs(sum(t.net_pnl for t in closed_trades if t.net_pnl < 0.0))
    if gross_loss_abs == 0.0:
        return None
    return gross_win / gross_loss_abs


def compute_profit_factor_robust(closed_trades: Sequence[ClosedTrade]) -> float | None:
    """Recompute profit factor after removing the single largest winning trade (§5.4)."""
    win_indices = [i for i, trade in enumerate(closed_trades) if trade.net_pnl > 0.0]
    if not win_indices:
        return 0.0
    max_win_idx = max(win_indices, key=lambda idx: closed_trades[idx].net_pnl)
    remaining = [trade for idx, trade in enumerate(closed_trades) if idx != max_win_idx]
    if not remaining:
        return 0.0
    return compute_profit_factor(remaining)


def compute_top_win_share(closed_trades: Sequence[ClosedTrade]) -> float:
    """Return ``max(netPnl > 0) / W``, or ``0.0`` when ``W == 0`` (§5.4)."""
    wins = [trade.net_pnl for trade in closed_trades if trade.net_pnl > 0.0]
    gross_win = sum(wins)
    if gross_win <= 0.0:
        return 0.0
    return min(1.0, max(0.0, max(wins) / gross_win))


def compute_max_drawdown(portfolio: PortfolioResponse | PortfolioWindowMetrics) -> float:
    """Compute scale-normalized ``perpAllTime`` PnL drawdown bounded in ``[0.0, 1.0]`` (§5.4).

    Implements Proposal 0002 (Option A1):
    - Only ``AV > 0`` samples can establish a new PnL high-water mark (``peak_pnl``).
    - If an opening ``(AV <= 0, PnL == 0)`` origin precedes the first funded point and that
      first funded point already has ``PnL < 0``, anchor ``peak_pnl = 0.0`` with
      ``peak_cap = AV + (-PnL)`` so opening losses are never skipped as ``0.0``.
    - From each valid high-water mark ``argmax``, track the running maximum effective capital
      ``peak_cap_t = max_{argmax <= u <= t}(max(AV_u, 0) + (peak_pnl - PnL_u))`` and compute
      ``DD_t = (peak_pnl - PnL_t) / peak_cap_t``, preventing scale-up amplification and
      withdrawal-to-zero false blowups.
    """
    window = (
        portfolio.window("perpAllTime") if isinstance(portfolio, PortfolioResponse) else portfolio
    )
    peak_pnl: float | None = None
    peak_cap = 0.0
    max_dd = 0.0
    saw_zero_origin = False

    for (_, av_str), (_, pnl_str) in zip(
        window.accountValueHistory,
        window.pnlHistory,
        strict=True,
    ):
        av = float(av_str)
        pnl = float(pnl_str)
        if peak_pnl is None:
            if av <= 0.0:
                if pnl == 0.0:
                    saw_zero_origin = True
                continue
            if saw_zero_origin and pnl < 0.0:
                peak_pnl = 0.0
                dd_usd = -pnl
                peak_cap = av + dd_usd
                if peak_cap > 0.0:
                    max_dd = max(max_dd, min(1.0, dd_usd / peak_cap))
            else:
                peak_pnl = pnl
                peak_cap = av
            continue

        if av > 0.0 and pnl > peak_pnl:
            peak_pnl = pnl
            peak_cap = av
            continue

        dd_usd = max(0.0, peak_pnl - pnl)
        eff_cap = max(av, 0.0) + dd_usd
        if eff_cap > peak_cap:
            peak_cap = eff_cap
        if peak_cap > 0.0:
            max_dd = max(max_dd, min(1.0, dd_usd / peak_cap))

    return min(1.0, max(0.0, max_dd))


def compute_history_days(fills_history_start: int | None, now_ms: int) -> float:
    """Return elapsed days since earliest available main-perp fill, or ``0.0`` if none (§5.4)."""
    if fills_history_start is None:
        return 0.0
    return max(0.0, (now_ms - fills_history_start) / DAY_MS)


def compute_fills_per_day(
    fill_stats_days: Mapping[str, tuple[int, int]],
    now_ms: int,
    window_days: int = 30,
) -> float:
    """Return average main-perp fills per active UTC day over the last ``window_days`` (§5.4)."""
    valid_dates = recent_utc_dates(now_ms, window_days)
    active_counts = [
        n_fills
        for day_str, (n_fills, _) in fill_stats_days.items()
        if day_str in valid_dates and n_fills > 0
    ]
    if not active_counts:
        return 0.0
    return sum(active_counts) / len(active_counts)


def compute_median_hold_minutes(closed_trades: Sequence[ClosedTrade]) -> float:
    """Return median holding duration in minutes across completed trades (§5.4)."""
    if not closed_trades:
        return 0.0
    holds = [max(0.0, (trade.close_t - trade.open_t) / MIN_MS) for trade in closed_trades]
    return float(statistics.median(holds))


def compute_maker_ratio(
    fill_stats_days: Mapping[str, tuple[int, int]],
    now_ms: int,
    window_days: int = 30,
) -> float:
    """Return fraction of ``crossed == false`` fills over the last ``window_days`` (§5.4)."""
    valid_dates = recent_utc_dates(now_ms, window_days)
    total_fills = 0
    total_maker = 0
    for day_str, (n_fills, n_maker) in fill_stats_days.items():
        if day_str in valid_dates and n_fills > 0:
            total_fills += n_fills
            total_maker += max(0, min(n_maker, n_fills))
    if total_fills <= 0:
        return 0.0
    return min(1.0, max(0.0, total_maker / total_fills))


def compute_turnover_month(
    account_value: float,
    *,
    leaderboard_vlm_month: float | None = None,
    fill_notional_days: Mapping[str, float] | None = None,
    now_ms: int | None = None,
    window_days: int = 30,
    portfolio: PortfolioResponse | None = None,
) -> float:
    """Compute monthly volume turnover ratio ``vlmMonth / accountValue`` (§5.4)."""
    if account_value <= 0.0:
        return 0.0
    if leaderboard_vlm_month is not None:
        return max(0.0, leaderboard_vlm_month / account_value)
    if fill_notional_days is not None and now_ms is not None:
        valid_dates = recent_utc_dates(now_ms, window_days)
        vlm_30d = sum(
            val
            for day_str, val in fill_notional_days.items()
            if day_str in valid_dates and val > 0.0
        )
        return max(0.0, vlm_30d / account_value)
    if portfolio is not None:
        return max(0.0, float(portfolio.window("perpMonth").vlm) / account_value)
    return 0.0


def compute_funding_share(cum_funding_net: float, pnl_all_time: float) -> float:
    """Compute ``|cumFundingNet| / max(|pnlAllTime|, 1.0)`` (§5.4, ADR-0003)."""
    return abs(cum_funding_net) / max(abs(pnl_all_time), 1.0)


def _spot_symbol_candidates(perp_coin: str) -> tuple[str, ...]:
    if perp_coin in _SPOT_ALIASES:
        return _SPOT_ALIASES[perp_coin]
    candidates = [perp_coin, f"U{perp_coin}", f"F{perp_coin}"]
    if perp_coin.startswith("k") and len(perp_coin) > 1:
        base = perp_coin[1:]
        candidates.extend((base, f"U{base}", f"F{base}"))
    return tuple(dict.fromkeys(candidates))


def compute_hedge_ratio(
    clearinghouse: ClearinghouseStateResponse,
    spot: SpotClearinghouseStateResponse | None,
    *,
    main_perp_universe: set[str] | None = None,
) -> float:
    """Compute notional-weighted spot coverage across open main-perp short positions (§5.4)."""
    if spot is None or not spot.balances:
        return 0.0

    spot_totals: dict[str, float] = {}
    for bal in spot.balances:
        qty = float(bal.total)
        if qty > 0.0:
            spot_totals[bal.coin] = spot_totals.get(bal.coin, 0.0) + qty

    if not spot_totals:
        return 0.0

    weighted_cov = 0.0
    total_notional = 0.0
    for item in clearinghouse.assetPositions:
        pos = item.position
        if not is_main_perp_coin(pos.coin, main_perp_universe):
            continue
        szi = float(pos.szi)
        if szi >= 0.0:
            continue
        abs_szi = abs(szi)
        notional = abs(float(pos.positionValue))
        if notional <= 0.0 and pos.entryPx is not None:
            notional = abs_szi * float(pos.entryPx)
        if notional <= 0.0:
            continue
        spot_qty = sum(spot_totals.get(sym, 0.0) for sym in _spot_symbol_candidates(pos.coin))
        cov = min(spot_qty, abs_szi) / abs_szi
        weighted_cov += notional * cov
        total_notional += notional

    if total_notional <= 0.0:
        return 0.0
    return min(1.0, max(0.0, weighted_cov / total_notional))


def compute_equity(
    clearinghouse: ClearinghouseStateResponse,
    abstraction: str,
    spot: SpotClearinghouseStateResponse | None = None,
) -> tuple[float, EquitySource, list[str]]:
    """Compute account equity and ``equitySource`` across abstraction modes (ADR-0001)."""
    return compute_account_equity(clearinghouse, abstraction, spot)


def derive_portfolio_fallbacks(
    portfolio: PortfolioResponse,
    account_value: float,
    *,
    leaderboard_pnl_all_time: float | None = None,
    leaderboard_roi_all_time: float | None = None,
    leaderboard_pnl_month: float | None = None,
) -> tuple[float, float, float]:
    """Return ``(pnlAllTime, roiAllTime, pnlMonth)`` with portfolio fallbacks (§5.4)."""
    perp_all = portfolio.window("perpAllTime")
    perp_month = portfolio.window("perpMonth")

    pnl_all_time = (
        leaderboard_pnl_all_time
        if leaderboard_pnl_all_time is not None
        else (float(perp_all.pnlHistory[-1][1]) if perp_all.pnlHistory else 0.0)
    )
    pnl_month = (
        leaderboard_pnl_month
        if leaderboard_pnl_month is not None
        else (float(perp_month.pnlHistory[-1][1]) if perp_month.pnlHistory else 0.0)
    )
    if leaderboard_roi_all_time is not None:
        roi_all_time = leaderboard_roi_all_time
    else:
        net_principals = [
            float(av_str) - float(pnl_str)
            for (_, av_str), (_, pnl_str) in zip(
                perp_all.accountValueHistory,
                perp_all.pnlHistory,
                strict=True,
            )
            if (float(av_str) - float(pnl_str)) > 0.0
        ]
        denom = (
            max(net_principals)
            if net_principals
            else (account_value if account_value > 0.0 else 0.0)
        )
        roi_all_time = (pnl_all_time / denom) if denom > 0.0 else 0.0

    return pnl_all_time, roi_all_time, pnl_month


def build_whitelist_metrics(
    *,
    clearinghouse: ClearinghouseStateResponse,
    abstraction: str,
    spot: SpotClearinghouseStateResponse | None,
    portfolio: PortfolioResponse,
    closed_trades: Sequence[ClosedTrade],
    fill_stats_days: Mapping[str, tuple[int, int]],
    fill_notional_days: Mapping[str, float],
    fills_history_start: int | None,
    cum_funding_net: float,
    now_ms: int,
    fills_window_days: int = 30,
    main_perp_universe: set[str] | None = None,
    leaderboard_pnl_all_time: float | None = None,
    leaderboard_roi_all_time: float | None = None,
    leaderboard_pnl_month: float | None = None,
    leaderboard_vlm_month: float | None = None,
) -> tuple[WhitelistMetrics, list[str]]:
    """Compute and validate all §5.4 metrics into a ``WhitelistMetrics`` contract model."""
    equity, equity_source, warnings = compute_equity(clearinghouse, abstraction, spot)
    account_value = max(0.0, equity)
    pnl_all_time, roi_all_time, pnl_month = derive_portfolio_fallbacks(
        portfolio,
        account_value,
        leaderboard_pnl_all_time=leaderboard_pnl_all_time,
        leaderboard_roi_all_time=leaderboard_roi_all_time,
        leaderboard_pnl_month=leaderboard_pnl_month,
    )

    metrics = WhitelistMetrics(
        accountValue=account_value,
        equitySource=equity_source,
        pnlAllTime=pnl_all_time,
        roiAllTime=roi_all_time,
        pnlMonth=pnl_month,
        maxDrawdown=compute_max_drawdown(portfolio),
        profitFactor=compute_profit_factor(closed_trades),
        profitFactorRobust=compute_profit_factor_robust(closed_trades),
        winRate=compute_win_rate(closed_trades),
        nTrades=compute_n_trades(closed_trades),
        historyDays=compute_history_days(fills_history_start, now_ms),
        fillsPerDay=compute_fills_per_day(fill_stats_days, now_ms, fills_window_days),
        medianHoldMinutes=compute_median_hold_minutes(closed_trades),
        makerRatio=compute_maker_ratio(fill_stats_days, now_ms, fills_window_days),
        turnoverMonth=compute_turnover_month(
            account_value,
            leaderboard_vlm_month=leaderboard_vlm_month,
            fill_notional_days=fill_notional_days,
            now_ms=now_ms,
            window_days=fills_window_days,
            portfolio=portfolio,
        ),
        fundingShare=compute_funding_share(cum_funding_net, pnl_all_time),
        topWinShare=compute_top_win_share(closed_trades),
        hedgeRatio=compute_hedge_ratio(
            clearinghouse,
            spot,
            main_perp_universe=main_perp_universe,
        ),
    )
    return metrics, warnings

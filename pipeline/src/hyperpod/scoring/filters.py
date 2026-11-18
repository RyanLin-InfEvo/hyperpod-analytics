# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stage 2 hard-gate evaluation rules (§5.5)."""

from __future__ import annotations

from collections.abc import Sequence

from hyperpod.config import PipelineConfig
from hyperpod.models.api import AccountRole
from hyperpod.models.common import RejectReason
from hyperpod.models.whitelist import WhitelistMetrics
from hyperpod.scoring.trades import ClosedTrade
from hyperpod.util.time import DAY_MS


def evaluate_role_gate(role: AccountRole, cfg: PipelineConfig) -> list[RejectReason]:
    """Evaluate the Stage 2 step-1 ``userRole`` exclusion gate (§5.1, §5.5)."""
    if role in cfg.gates.excludeRoles.value or (
        cfg.membership.excludeVaults.value and role == "vault"
    ):
        return ["role"]
    return []


def evaluate_gates(
    metrics: WhitelistMetrics,
    cfg: PipelineConfig,
    *,
    role: AccountRole = "user",
    truncated: bool = False,
    fills_history_start: int | None = None,
    last_fill_time: int | None = None,
    closed_trades: Sequence[ClosedTrade] = (),
    now_ms: int = 0,
    include_funding_gate: bool = True,
) -> list[RejectReason]:
    """Evaluate all §5.5 hard gates and return ordered rejection reasons."""
    reasons: list[RejectReason] = []

    if evaluate_role_gate(role, cfg):
        reasons.append("role")

    hft_truncated_fail = False
    if truncated:
        if (
            fills_history_start is not None
            and last_fill_time is not None
            and last_fill_time >= fills_history_start
        ):
            span_days = (last_fill_time - fills_history_start) / DAY_MS
        else:
            span_days = 0.0
        hft_truncated_fail = span_days < cfg.gates.hft.minTruncatedSpanDays.value

    if (
        metrics.fillsPerDay > cfg.gates.hft.fillsPerDay.value
        or metrics.medianHoldMinutes < cfg.gates.hft.medianHoldMinutes.value
        or hft_truncated_fail
    ):
        reasons.append("hft")

    if (
        metrics.makerRatio > cfg.gates.mm.makerRatio.value
        and metrics.turnoverMonth > cfg.gates.mm.turnoverMonth.value
    ):
        reasons.append("mm")

    if include_funding_gate and metrics.fundingShare > cfg.gates.funding.maxShare.value:
        reasons.append("funding")

    hedge_ratio = metrics.hedgeRatio if metrics.hedgeRatio is not None else 0.0
    if hedge_ratio > cfg.gates.hedge.maxRatio.value:
        reasons.append("hedge")

    passive_cutoff_ms = now_ms - cfg.scoring.passiveWindowDays.value * DAY_MS
    recent_trades = sum(1 for trade in closed_trades if trade.close_t >= passive_cutoff_ms)
    if recent_trades < cfg.gates.passive.minRecentTrades90d.value:
        reasons.append("passive")

    if (
        metrics.nTrades < cfg.gates.sample.minTrades.value
        or metrics.historyDays < cfg.gates.sample.minHistoryDays.value
    ):
        reasons.append("sample")

    if metrics.maxDrawdown > cfg.gates.mdd.maxDrawdown.value:
        reasons.append("mdd")

    if (
        metrics.profitFactorRobust is not None
        and metrics.profitFactorRobust < cfg.gates.pf.minRobust.value
    ):
        reasons.append("pf")

    top_win_share = metrics.topWinShare if metrics.topWinShare is not None else 0.0
    if top_win_share > cfg.gates.lucky.maxTopWinShare.value:
        reasons.append("lucky")

    return reasons

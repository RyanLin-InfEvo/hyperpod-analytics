# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared Stage 1 discovery and Stage 2 backfill/scoring runners for CLI workflows."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from hyperpod.client.info_api import InfoApiClient, InfoApiError, is_main_perp_coin
from hyperpod.client.leaderboard import DEFAULT_LEADERBOARD_URL, LeaderboardClient
from hyperpod.client.ratelimit import WeightRateLimiter
from hyperpod.config import PipelineConfig
from hyperpod.discovery import (
    Stage1Candidate,
    expand_subaccounts,
    load_address_list,
    prefilter_leaderboard,
)
from hyperpod.models.common import RejectReason
from hyperpod.models.whitelist import WhitelistMetrics
from hyperpod.scoring.filters import evaluate_gates, evaluate_role_gate
from hyperpod.scoring.metrics import (
    build_whitelist_metrics,
    compute_fills_per_day,
    compute_funding_share,
    compute_history_days,
    compute_maker_ratio,
    compute_median_hold_minutes,
    compute_n_trades,
    compute_profit_factor,
    compute_profit_factor_robust,
    compute_top_win_share,
    compute_turnover_month,
    compute_win_rate,
)
from hyperpod.scoring.trades import ClosedTrade, ingest_raw_fills_incremental
from hyperpod.scoring.whitelist import (
    CandidateEvaluation,
    WhitelistUpdateResult,
    update_whitelist_membership,
)
from hyperpod.storage.state_store import (
    AddressDailyState,
    BackfillCursorFile,
    DiscoveredCandidateRecord,
    DiscoveredCandidatesFile,
    FillStatsState,
    StateStore,
)
from hyperpod.util.time import DAY_MS, now_utc_ms


def _to_discovered_record(cand: Stage1Candidate) -> DiscoveredCandidateRecord:
    return DiscoveredCandidateRecord(
        address=cand.address,
        source=cand.source,
        displayName=cand.display_name,
        master=cand.master,
        leaderboardAccountValue=cand.leaderboard_account_value,
        leaderboardPnlAllTime=cand.leaderboard_pnl_all_time,
        leaderboardRoiAllTime=cand.leaderboard_roi_all_time,
        leaderboardPnlMonth=cand.leaderboard_pnl_month,
        leaderboardVlmMonth=cand.leaderboard_vlm_month,
    )


def validate_output_against_schema(payload: dict[str, Any], schema_path: Path) -> None:
    """Validate serialized output JSON against its draft 2020-12 JSON Schema."""
    if not schema_path.is_file():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(payload)


def _default_schemas_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "schemas"


async def run_discover(
    cfg: PipelineConfig,
    store: StateStore,
    *,
    seed_file: Path,
    blocklist_file: Path,
    leaderboard_url: str = DEFAULT_LEADERBOARD_URL,
    now_ms: int | None = None,
    http_client: httpx.AsyncClient | None = None,
    rate_limiter: WeightRateLimiter | None = None,
) -> DiscoveredCandidatesFile:
    """Fetch leaderboard, run Stage 1 prefilter + seed/active merges, and persist queue."""
    eval_ts = now_ms if now_ms is not None else now_utc_ms()
    seeds = load_address_list(seed_file)
    blocklist = load_address_list(blocklist_file)
    prior_wl = store.load_whitelist_state()
    active_wl = (
        [m.address for m in prior_wl.addresses.values() if m.is_active]
        if prior_wl is not None
        else []
    )

    limiter = rate_limiter or WeightRateLimiter(
        budget_per_min=cfg.ratelimit.ciBudgetPerMin.value,
        max_retries=cfg.ratelimit.maxRetries.value,
        halve_cooldown_seconds=float(cfg.ratelimit.halveCooldownSeconds.value),
    )
    async with LeaderboardClient(
        leaderboard_url=leaderboard_url,
        max_retries=min(3, cfg.ratelimit.maxRetries.value),
        rate_limiter=limiter,
        http_client=http_client,
    ) as lb_client:
        lb_res = await lb_client.fetch(cache_path=store.leaderboard_cache_path)

    pf_res = prefilter_leaderboard(
        lb_res.response,
        cfg,
        seed_addresses=seeds,
        blocklist=blocklist,
        active_whitelist_addresses=active_wl,
    )
    records = [_to_discovered_record(c) for c in pf_res.candidates]
    discovered = DiscoveredCandidatesFile(
        generatedAt=eval_ts,
        totalLeaderboardRows=pf_res.total_leaderboard_rows,
        passedLeaderboardCount=pf_res.passed_leaderboard_count,
        seedAddedCount=pf_res.seed_added_count,
        activeWhitelistAddedCount=pf_res.active_whitelist_added_count,
        blocklistedCount=pf_res.blocklisted_count,
        usedLeaderboardFallback=lb_res.used_fallback,
        rejectedCounts=pf_res.rejected_counts,
        warnings=list(lb_res.warnings),
        candidates=records,
    )
    store.save_discovered_candidates(discovered)
    store.append_run_log(
        "daily",
        {
            "job": "discover",
            "ts": eval_ts,
            "totalLeaderboardRows": discovered.totalLeaderboardRows,
            "candidates": len(discovered.candidates),
            "usedFallback": discovered.usedLeaderboardFallback,
            "warnings": discovered.warnings,
        },
        max_entries=cfg.stateRetention.runsLogMaxEntries.value,
    )
    return discovered


async def evaluate_candidate_live(
    cand: DiscoveredCandidateRecord,
    *,
    client: InfoApiClient,
    store: StateStore,
    cfg: PipelineConfig,
    main_perp_universe: set[str],
    sz_decimals_by_coin: dict[str, int],
    active_whitelist_set: set[str],
    blocklist_set: set[str],
    known_queue_addresses: set[str],
    now_ms: int,
    max_fill_pages: int | None = None,
    max_funding_pages: int | None = None,
) -> tuple[CandidateEvaluation, list[DiscoveredCandidateRecord], list[str]]:
    """Execute §5.1 tiered Stage 2 API collection, trade update, and hard-gate scoring."""
    addr = cand.address.lower()
    state = store.get_address_state(addr) or AddressDailyState(address=addr)
    if cand.master is not None and state.master is None:
        state.master = cand.master.lower()
    if cand.displayName is not None and state.label is None:
        state.label = cand.displayName

    warnings: list[str] = []

    # Step 1: userRole (permanently cached once known, except transient "missing")
    if state.role is None or state.role == "missing":
        role_resp = await client.fetch_user_role(addr)
        state.role = role_resp.role
        if role_resp.data is not None and role_resp.data.master is not None:
            state.master = role_resp.data.master.lower()
    assert state.role is not None
    resolved_role = state.role

    role_reasons = evaluate_role_gate(resolved_role, cfg)
    if role_reasons:
        state.lastScore = {
            "evaluatedAt": now_ms,
            "metrics": None,
            "rejectReasons": list(role_reasons),
            "openPositionsNotional": {},
            "truncated": state.truncated,
        }
        store.put_address_state(state)
        return (
            CandidateEvaluation(
                address=addr,
                role=state.role,
                master=state.master,
                label=state.label,
                reject_reasons=role_reasons,
            ),
            [],
            warnings,
        )

    # Step 2: subAccounts (weekly refresh for role == "user"; always expand from cache)
    refresh_ms = cfg.scoring.subAccountRefreshDays.value * DAY_MS
    if state.role == "user" and (
        state.subAccountsUpdatedAt is None or (now_ms - state.subAccountsUpdatedAt) >= refresh_ms
    ):
        sub_items = await client.fetch_sub_accounts(addr)
        state.subAccounts = sorted({item.subAccountUser.lower() for item in sub_items})
        state.subAccountsUpdatedAt = now_ms
        all_eligible = expand_subaccounts(sub_items, cfg)
        state.eligibleSubAccounts = [_to_discovered_record(item) for item in all_eligible]

    expanded_records = [
        rec.model_copy(deep=True)
        for rec in state.eligibleSubAccounts
        if rec.address.lower() not in blocklist_set
        and rec.address.lower() not in known_queue_addresses
    ]

    # Step 3: abstraction, clearinghouseState, conditional spotClearinghouseState, portfolio
    abstraction = await client.fetch_user_abstraction(addr)
    state.abstraction = abstraction
    clearinghouse = await client.fetch_clearinghouse_state(addr)
    has_short = any(
        is_main_perp_coin(ap.position.coin, main_perp_universe) and float(ap.position.szi) < 0.0
        for ap in clearinghouse.assetPositions
    )
    spot = (
        await client.fetch_spot_clearinghouse_state(addr)
        if (abstraction not in {"disabled", "default"} or has_short)
        else None
    )
    portfolio = await client.fetch_portfolio(addr)

    # Step 4: incremental userFillsByTime + trade reconstruction
    fills_res = await client.fetch_user_fills_by_time(
        addr,
        start_time=state.fillsCursorScoring,
        overlap_ms=cfg.ratelimit.fillOverlapMs.value,
        max_pages=max_fill_pages,
        seed_keys=state.recentScoringFillKeys,
        main_perp_universe=main_perp_universe,
    )
    warnings.extend(fills_res.warnings)

    ing = ingest_raw_fills_incremental(
        fills_res.fills,
        addr,
        fills_cursor_scoring=state.fillsCursorScoring,
        recent_scoring_fill_keys=state.recentScoringFillKeys,
        overlap_ms=cfg.ratelimit.fillOverlapMs.value,
        main_perp_universe=main_perp_universe,
        open_trades=state.openTrades,
        closed_trades=state.closedTrades,
        fill_stats_days=state.fillStats.days,
        fill_notional_days=state.fillStats.notionalDays,
        sz_decimals_by_coin=sz_decimals_by_coin,
        fills_history_start=state.fillsHistoryStart,
        last_fill_time=state.lastFillTime,
    )
    warnings.extend(ing.warnings)

    state.fillsCursorScoring = ing.fills_cursor_scoring
    state.lastFillFetchAt = now_ms
    state.recentScoringFillKeys = ing.recent_scoring_fill_keys
    state.fillsHistoryStart = ing.trades.fills_history_start
    state.lastFillTime = ing.trades.last_fill_time
    if fills_res.truncated_10k or ing.trades.saw_truncated_position:
        state.truncated = True
    state.openTrades = ing.trades.open_trades
    state.closedTrades = [trade.to_tuple() for trade in ing.trades.closed_trades]
    state.fillStats = FillStatsState(
        days=ing.trades.fill_stats_days,
        notionalDays=ing.trades.fill_notional_days,
    )

    closed_models = ing.trades.closed_trades
    metrics, metric_warnings = build_whitelist_metrics(
        clearinghouse=clearinghouse,
        abstraction=abstraction,
        spot=spot,
        portfolio=portfolio,
        closed_trades=closed_models,
        fill_stats_days=state.fillStats.days,
        fill_notional_days=state.fillStats.notionalDays,
        fills_history_start=state.fillsHistoryStart,
        cum_funding_net=state.cumFundingNet,
        now_ms=now_ms,
        fills_window_days=cfg.scoring.fillsWindowDays.value,
        main_perp_universe=main_perp_universe,
        leaderboard_pnl_all_time=cand.leaderboardPnlAllTime,
        leaderboard_roi_all_time=cand.leaderboardRoiAllTime,
        leaderboard_pnl_month=cand.leaderboardPnlMonth,
        leaderboard_vlm_month=cand.leaderboardVlmMonth,
    )
    warnings.extend(metric_warnings)

    pre_funding_reasons = evaluate_gates(
        metrics,
        cfg,
        role=resolved_role,
        truncated=state.truncated,
        fills_history_start=state.fillsHistoryStart,
        last_fill_time=state.lastFillTime,
        closed_trades=closed_models,
        now_ms=now_ms,
        include_funding_gate=False,
    )

    # Step 5: userFunding ONLY when passing steps 1-4 or already in active whitelist (§5.1)
    reasons: list[RejectReason]
    if not pre_funding_reasons or addr in active_whitelist_set:
        start_funding = state.lastFundingTime if state.lastFundingTime is not None else 0
        funding_res = await client.fetch_user_funding(
            addr,
            start_time=start_funding,
            max_pages=max_funding_pages,
            seed_last_funding_coins=state.lastFundingCoins,
            main_perp_universe=main_perp_universe,
        )
        state.cumFundingNet += funding_res.cum_funding_net
        state.cumFundingAbs += funding_res.cum_funding_abs
        if funding_res.last_funding_time is not None:
            state.lastFundingTime = funding_res.last_funding_time
            state.lastFundingCoins = funding_res.last_funding_coins

        metrics, funding_metric_warnings = build_whitelist_metrics(
            clearinghouse=clearinghouse,
            abstraction=abstraction,
            spot=spot,
            portfolio=portfolio,
            closed_trades=closed_models,
            fill_stats_days=state.fillStats.days,
            fill_notional_days=state.fillStats.notionalDays,
            fills_history_start=state.fillsHistoryStart,
            cum_funding_net=state.cumFundingNet,
            now_ms=now_ms,
            fills_window_days=cfg.scoring.fillsWindowDays.value,
            main_perp_universe=main_perp_universe,
            leaderboard_pnl_all_time=cand.leaderboardPnlAllTime,
            leaderboard_roi_all_time=cand.leaderboardRoiAllTime,
            leaderboard_pnl_month=cand.leaderboardPnlMonth,
            leaderboard_vlm_month=cand.leaderboardVlmMonth,
        )
        warnings.extend(funding_metric_warnings)
        reasons = evaluate_gates(
            metrics,
            cfg,
            role=resolved_role,
            truncated=state.truncated,
            fills_history_start=state.fillsHistoryStart,
            last_fill_time=state.lastFillTime,
            closed_trades=closed_models,
            now_ms=now_ms,
            include_funding_gate=True,
        )
    else:
        reasons = pre_funding_reasons

    open_positions_notional: dict[str, float] = {}
    for ap in clearinghouse.assetPositions:
        pos = ap.position
        if not is_main_perp_coin(pos.coin, main_perp_universe):
            continue
        if abs(float(pos.szi)) <= 0.0:
            continue
        ntl = abs(float(pos.positionValue))
        if ntl > 0.0:
            open_positions_notional[pos.coin] = open_positions_notional.get(pos.coin, 0.0) + ntl

    state.lastScore = {
        "evaluatedAt": now_ms,
        "metrics": metrics.model_dump(),
        "rejectReasons": list(reasons),
        "openPositionsNotional": open_positions_notional,
        "truncated": state.truncated,
    }
    store.put_address_state(state)

    evaluation = CandidateEvaluation(
        address=addr,
        role=state.role,
        master=state.master,
        sub_accounts=list(state.subAccounts),
        label=state.label,
        coverage_start=state.fillsHistoryStart or now_ms,
        abstraction=state.abstraction,
        metrics=metrics,
        reject_reasons=reasons,
        open_positions_notional=open_positions_notional,
    )
    return evaluation, expanded_records, warnings


@dataclass
class BackfillRunSummary:
    """Summary returned by ``hyperpod backfill``."""

    completed: bool
    processed_this_run: int
    total_processed: int
    total_candidates: int
    next_index: int
    expanded_subaccounts: int
    errors: int
    budget_exhausted: bool
    warnings: list[str]
    rate_limit_stats: dict[str, object]


async def run_backfill(
    cfg: PipelineConfig,
    store: StateStore,
    *,
    seed_file: Path,
    blocklist_file: Path,
    budget_minutes: float | None = None,
    checkpoint_every: int = 20,
    limit: int | None = None,
    restart: bool = False,
    refresh_discover: bool = False,
    local_budget: bool = True,
    max_fill_pages: int | None = None,
    max_funding_pages: int | None = None,
    leaderboard_url: str = DEFAULT_LEADERBOARD_URL,
    info_url: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    rate_limiter: WeightRateLimiter | None = None,
) -> BackfillRunSummary:
    """Run resumable Stage 2 candidate backfill with 20-address checkpoints and time budget."""
    start_wall = time.monotonic()
    now_ms = now_utc_ms()
    blocklist_set = set(load_address_list(blocklist_file))
    prior_wl = store.load_whitelist_state()
    active_wl_set = (
        {m.address for m in prior_wl.addresses.values() if m.is_active}
        if prior_wl is not None
        else set()
    )

    budget_per_min = (
        cfg.ratelimit.localBudgetPerMin.value
        if local_budget
        else cfg.ratelimit.ciBudgetPerMin.value
    )
    limiter = rate_limiter or WeightRateLimiter(
        budget_per_min=budget_per_min,
        max_retries=cfg.ratelimit.maxRetries.value,
        halve_cooldown_seconds=float(cfg.ratelimit.halveCooldownSeconds.value),
    )

    existing_cursor = None if restart else store.load_backfill_cursor()
    if existing_cursor is not None and not existing_cursor.completed:
        cursor = existing_cursor
    else:
        discovered = None if refresh_discover else store.load_discovered_candidates()
        if discovered is None:
            discovered = await run_discover(
                cfg,
                store,
                seed_file=seed_file,
                blocklist_file=blocklist_file,
                leaderboard_url=leaderboard_url,
                now_ms=now_ms,
                http_client=http_client,
                rate_limiter=limiter,
            )
        cursor = BackfillCursorFile(
            startedAt=now_ms,
            updatedAt=now_ms,
            nextIndex=0,
            totalCandidates=len(discovered.candidates),
            processedCount=0,
            completed=len(discovered.candidates) == 0,
            queue=list(discovered.candidates),
            expandedSubaccounts=0,
        )
        store.save_backfill_cursor(cursor)

    known_queue_addresses = {c.address.lower() for c in cursor.queue}
    processed_this_run = 0
    errors = 0
    budget_exhausted = False
    warnings: list[str] = []

    client_kwargs: dict[str, Any] = {
        "rate_limiter": limiter,
        "max_concurrency": cfg.ratelimit.maxConcurrency.value,
        "http_client": http_client,
    }
    if info_url is not None:
        client_kwargs["info_url"] = info_url

    async with InfoApiClient(**client_kwargs) as client:
        meta = await client.fetch_meta()
        main_perp_universe = {asset.name for asset in meta.universe}
        sz_decimals_by_coin = {asset.name: asset.szDecimals for asset in meta.universe}

        while cursor.nextIndex < len(cursor.queue):
            if limit is not None and processed_this_run >= limit:
                break
            if budget_minutes is not None:
                elapsed_min = (time.monotonic() - start_wall) / 60.0
                if elapsed_min >= budget_minutes:
                    budget_exhausted = True
                    break

            cand = cursor.queue[cursor.nextIndex]
            if cand.address.lower() in blocklist_set:
                cursor.nextIndex += 1
                cursor.processedCount += 1
                cursor.totalCandidates = len(cursor.queue)
                cursor.updatedAt = now_utc_ms()
                continue
            try:
                _, expanded, item_warnings = await evaluate_candidate_live(
                    cand,
                    client=client,
                    store=store,
                    cfg=cfg,
                    main_perp_universe=main_perp_universe,
                    sz_decimals_by_coin=sz_decimals_by_coin,
                    active_whitelist_set=active_wl_set,
                    blocklist_set=blocklist_set,
                    known_queue_addresses=known_queue_addresses,
                    now_ms=now_utc_ms(),
                    max_fill_pages=max_fill_pages,
                    max_funding_pages=max_funding_pages,
                )
                warnings.extend(item_warnings)
                for sub_rec in expanded:
                    if sub_rec.address not in known_queue_addresses:
                        known_queue_addresses.add(sub_rec.address)
                        cursor.queue.append(sub_rec)
                        cursor.expandedSubaccounts += 1
            except (InfoApiError, ValueError) as exc:
                errors += 1
                warnings.append(f"Backfill error on {cand.address}: {exc}")

            cursor.nextIndex += 1
            cursor.processedCount += 1
            cursor.totalCandidates = len(cursor.queue)
            cursor.updatedAt = now_utc_ms()
            processed_this_run += 1

            if processed_this_run % checkpoint_every == 0:
                store.flush_dirty_shards()
                store.save_backfill_cursor(cursor)

    if cursor.nextIndex >= len(cursor.queue):
        cursor.completed = True

    store.flush_dirty_shards()
    store.save_backfill_cursor(cursor)

    rl_stats = limiter.stats_dict()
    store.append_run_log(
        "daily",
        {
            "job": "backfill",
            "ts": cursor.updatedAt,
            "completed": cursor.completed,
            "processedThisRun": processed_this_run,
            "totalProcessed": cursor.processedCount,
            "totalCandidates": cursor.totalCandidates,
            "expandedSubaccounts": cursor.expandedSubaccounts,
            "errors": errors,
            "budgetExhausted": budget_exhausted,
            "rateLimitStats": rl_stats,
        },
        max_entries=cfg.stateRetention.runsLogMaxEntries.value,
    )
    return BackfillRunSummary(
        completed=cursor.completed,
        processed_this_run=processed_this_run,
        total_processed=cursor.processedCount,
        total_candidates=cursor.totalCandidates,
        next_index=cursor.nextIndex,
        expanded_subaccounts=cursor.expandedSubaccounts,
        errors=errors,
        budget_exhausted=budget_exhausted,
        warnings=warnings,
        rate_limit_stats=rl_stats,
    )


def _evaluate_candidate_offline(
    state: AddressDailyState,
    cfg: PipelineConfig,
    *,
    now_ms: int,
    leaderboard_vlm_month: float | None = None,
) -> CandidateEvaluation:
    role = state.role or "user"
    role_reasons = evaluate_role_gate(role, cfg)
    if role_reasons:
        return CandidateEvaluation(
            address=state.address,
            role=role,
            master=state.master,
            sub_accounts=list(state.subAccounts),
            label=state.label,
            coverage_start=state.fillsHistoryStart or now_ms,
            abstraction=state.abstraction,
            metrics=None,
            reject_reasons=role_reasons,
        )

    raw_metrics = state.lastScore.get("metrics")
    if not isinstance(raw_metrics, dict):
        return CandidateEvaluation(
            address=state.address,
            role=role,
            master=state.master,
            sub_accounts=list(state.subAccounts),
            label=state.label,
            coverage_start=state.fillsHistoryStart or now_ms,
            abstraction=state.abstraction,
            metrics=None,
            reject_reasons=["sample"],
        )

    cached_metrics = WhitelistMetrics.model_validate(raw_metrics)
    closed_models = [ClosedTrade.from_tuple(t) for t in state.closedTrades]
    window_days = cfg.scoring.fillsWindowDays.value
    turnover_month = (
        compute_turnover_month(
            cached_metrics.accountValue,
            leaderboard_vlm_month=leaderboard_vlm_month,
        )
        if leaderboard_vlm_month is not None
        else (
            compute_turnover_month(
                cached_metrics.accountValue,
                fill_notional_days=state.fillStats.notionalDays,
                now_ms=now_ms,
                window_days=window_days,
            )
            if state.fillStats.notionalDays
            else cached_metrics.turnoverMonth
        )
    )
    metrics = cached_metrics.model_copy(
        update={
            "nTrades": compute_n_trades(closed_models),
            "winRate": compute_win_rate(closed_models),
            "profitFactor": compute_profit_factor(closed_models),
            "profitFactorRobust": compute_profit_factor_robust(closed_models),
            "topWinShare": compute_top_win_share(closed_models),
            "medianHoldMinutes": compute_median_hold_minutes(closed_models),
            "historyDays": compute_history_days(state.fillsHistoryStart, now_ms),
            "fillsPerDay": compute_fills_per_day(state.fillStats.days, now_ms, window_days),
            "makerRatio": compute_maker_ratio(state.fillStats.days, now_ms, window_days),
            "fundingShare": compute_funding_share(state.cumFundingNet, cached_metrics.pnlAllTime),
            "turnoverMonth": turnover_month,
        }
    )
    reasons = evaluate_gates(
        metrics,
        cfg,
        role=role,
        truncated=state.truncated,
        fills_history_start=state.fillsHistoryStart,
        last_fill_time=state.lastFillTime,
        closed_trades=closed_models,
        now_ms=now_ms,
        include_funding_gate=True,
    )
    raw_ntl = state.lastScore.get("openPositionsNotional", {})
    open_ntl = (
        {str(k): float(v) for k, v in raw_ntl.items() if float(v) > 0.0}
        if isinstance(raw_ntl, dict)
        else {}
    )
    return CandidateEvaluation(
        address=state.address,
        role=role,
        master=state.master,
        sub_accounts=list(state.subAccounts),
        label=state.label,
        coverage_start=state.fillsHistoryStart or now_ms,
        abstraction=state.abstraction,
        metrics=metrics,
        reject_reasons=reasons,
        open_positions_notional=open_ntl,
    )


@dataclass
class ScoreRunSummary:
    """Summary returned by ``hyperpod score``."""

    whitelist_update: WhitelistUpdateResult
    evaluated_count: int
    expanded_subaccounts: int
    errors: int
    prune_stats: dict[str, int]
    written_files: list[Path]
    warnings: list[str]
    rate_limit_stats: dict[str, object]


async def run_score(
    cfg: PipelineConfig,
    store: StateStore,
    *,
    seed_file: Path,
    blocklist_file: Path,
    schemas_dir: Path | None = None,
    data_dir: Path | None = None,
    limit: int | None = None,
    offline: bool = False,
    refresh_discover: bool = False,
    local_budget: bool = False,
    max_fill_pages: int | None = None,
    max_funding_pages: int | None = None,
    leaderboard_url: str = DEFAULT_LEADERBOARD_URL,
    info_url: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    rate_limiter: WeightRateLimiter | None = None,
    now_ms: int | None = None,
) -> ScoreRunSummary:
    """Evaluate all Stage 2 candidates (online or offline), update whitelist, and write outputs."""
    eval_ts = now_ms if now_ms is not None else now_utc_ms()
    seeds = load_address_list(seed_file)
    blocklist = load_address_list(blocklist_file)
    blocklist_set = set(blocklist)
    prior_wl = store.load_whitelist_state()
    active_wl_set = (
        {m.address for m in prior_wl.addresses.values() if m.is_active}
        if prior_wl is not None
        else set()
    )

    budget_per_min = (
        cfg.ratelimit.localBudgetPerMin.value
        if local_budget
        else cfg.ratelimit.ciBudgetPerMin.value
    )
    limiter = rate_limiter or WeightRateLimiter(
        budget_per_min=budget_per_min,
        max_retries=cfg.ratelimit.maxRetries.value,
        halve_cooldown_seconds=float(cfg.ratelimit.halveCooldownSeconds.value),
    )

    evaluations: list[CandidateEvaluation] = []
    warnings: list[str] = []
    expanded_count = 0
    errors = 0
    active_stage1_addrs: set[str] = set()
    skipped_addresses: set[str] = set()
    skipped_open_notionals: dict[str, dict[str, float]] = {}

    if offline:
        cursor = store.load_backfill_cursor()
        discovered = store.load_discovered_candidates()
        seen_offline: set[str] = set()
        offline_addrs: list[str] = []
        lb_vlm_by_addr: dict[str, float] = {}

        if cursor is not None or discovered is not None:
            if discovered is not None and (
                cursor is None or discovered.generatedAt >= cursor.updatedAt
            ):
                base_records = list(discovered.candidates)
            else:
                assert cursor is not None
                base_records = list(cursor.queue)

            for rec in base_records:
                a = rec.address.lower()
                if a in blocklist_set:
                    continue
                if rec.leaderboardVlmMonth is not None:
                    lb_vlm_by_addr[a] = rec.leaderboardVlmMonth
                if a not in seen_offline:
                    seen_offline.add(a)
                    offline_addrs.append(a)
                parent_state = store.get_address_state(a)
                if parent_state is not None:
                    for sub_rec in parent_state.eligibleSubAccounts:
                        sub_a = sub_rec.address.lower()
                        if sub_a in blocklist_set or sub_a in seen_offline:
                            continue
                        seen_offline.add(sub_a)
                        offline_addrs.append(sub_a)
            for wl_addr in sorted(active_wl_set):
                if wl_addr not in blocklist_set and wl_addr not in seen_offline:
                    seen_offline.add(wl_addr)
                    offline_addrs.append(wl_addr)
        else:
            for shard_state in store.iter_all_address_states():
                a = shard_state.address.lower()
                if a not in blocklist_set and a not in seen_offline:
                    seen_offline.add(a)
                    offline_addrs.append(a)

        active_stage1_addrs = set(offline_addrs)
        if limit is not None:
            omitted = set(offline_addrs[limit:])
            for om_addr in omitted:
                if om_addr in active_wl_set:
                    skipped_addresses.add(om_addr)
                    om_state = store.get_address_state(om_addr)
                    if om_state is not None and isinstance(
                        om_state.lastScore.get("openPositionsNotional"), dict
                    ):
                        skipped_open_notionals[om_addr] = {
                            str(k): float(v)
                            for k, v in om_state.lastScore["openPositionsNotional"].items()
                            if float(v) > 0.0
                        }
            offline_addrs = offline_addrs[:limit]

        for addr in offline_addrs:
            addr_state = store.get_address_state(addr)
            if addr_state is not None:
                evaluations.append(
                    _evaluate_candidate_offline(
                        addr_state,
                        cfg,
                        now_ms=eval_ts,
                        leaderboard_vlm_month=lb_vlm_by_addr.get(addr),
                    )
                )
            elif addr in active_wl_set:
                skipped_addresses.add(addr)
    else:
        discovered = None if refresh_discover else store.load_discovered_candidates()
        if discovered is None:
            discovered = await run_discover(
                cfg,
                store,
                seed_file=seed_file,
                blocklist_file=blocklist_file,
                leaderboard_url=leaderboard_url,
                now_ms=eval_ts,
                http_client=http_client,
                rate_limiter=limiter,
            )
        warnings.extend(discovered.warnings)

        queue = [c for c in discovered.candidates if c.address.lower() not in blocklist_set]
        all_queue_addrs = {c.address.lower() for c in queue}
        if limit is not None:
            for om_cand in queue[limit:]:
                om_addr = om_cand.address.lower()
                if om_addr in active_wl_set:
                    skipped_addresses.add(om_addr)
                    om_state = store.get_address_state(om_addr)
                    if om_state is not None and isinstance(
                        om_state.lastScore.get("openPositionsNotional"), dict
                    ):
                        skipped_open_notionals[om_addr] = {
                            str(k): float(v)
                            for k, v in om_state.lastScore["openPositionsNotional"].items()
                            if float(v) > 0.0
                        }
            queue = queue[:limit]
        known_queue_addresses = {c.address.lower() for c in queue}
        active_stage1_addrs = set(all_queue_addrs)

        client_kwargs: dict[str, Any] = {
            "rate_limiter": limiter,
            "max_concurrency": cfg.ratelimit.maxConcurrency.value,
            "http_client": http_client,
        }
        if info_url is not None:
            client_kwargs["info_url"] = info_url

        async with InfoApiClient(**client_kwargs) as client:
            meta = await client.fetch_meta()
            main_perp_universe = {asset.name for asset in meta.universe}
            sz_decimals_by_coin = {asset.name: asset.szDecimals for asset in meta.universe}

            idx = 0
            while idx < len(queue):
                cand = queue[idx]
                idx += 1
                try:
                    ev, expanded, item_warnings = await evaluate_candidate_live(
                        cand,
                        client=client,
                        store=store,
                        cfg=cfg,
                        main_perp_universe=main_perp_universe,
                        sz_decimals_by_coin=sz_decimals_by_coin,
                        active_whitelist_set=active_wl_set,
                        blocklist_set=blocklist_set,
                        known_queue_addresses=known_queue_addresses,
                        now_ms=eval_ts,
                        max_fill_pages=max_fill_pages,
                        max_funding_pages=max_funding_pages,
                    )
                    evaluations.append(ev)
                    warnings.extend(item_warnings)
                    for sub_rec in expanded:
                        if sub_rec.address not in known_queue_addresses:
                            known_queue_addresses.add(sub_rec.address)
                            active_stage1_addrs.add(sub_rec.address)
                            queue.append(sub_rec)
                            expanded_count += 1
                except (InfoApiError, ValueError) as exc:
                    errors += 1
                    err_addr = cand.address.lower()
                    warnings.append(f"Score error on {cand.address}: {exc}")
                    skipped_addresses.add(err_addr)
                    if err_addr in active_wl_set:
                        err_state = store.get_address_state(err_addr)
                        if err_state is not None and isinstance(
                            err_state.lastScore.get("openPositionsNotional"), dict
                        ):
                            skipped_open_notionals[err_addr] = {
                                str(k): float(v)
                                for k, v in err_state.lastScore["openPositionsNotional"].items()
                                if float(v) > 0.0
                            }

        store.flush_dirty_shards()

    wl_result = update_whitelist_membership(
        prior_wl,
        evaluations,
        cfg,
        eval_ts_ms=eval_ts,
        blocklist=blocklist,
        skipped_addresses=skipped_addresses,
        skipped_open_notionals=skipped_open_notionals,
    )
    warnings.extend(wl_result.warnings)

    effective_schemas_dir = schemas_dir if schemas_dir is not None else _default_schemas_dir()
    validate_output_against_schema(
        wl_result.whitelist.model_dump(mode="json", by_alias=True),
        effective_schemas_dir / "whitelist.schema.json",
    )
    validate_output_against_schema(
        wl_result.criteria.model_dump(mode="json", by_alias=True),
        effective_schemas_dir / "criteria.schema.json",
    )
    validate_output_against_schema(
        wl_result.candidates.model_dump(mode="json", by_alias=True),
        effective_schemas_dir / "candidates.schema.json",
    )

    store.save_whitelist_state(wl_result.state)
    written_files = store.save_daily_outputs(
        whitelist=wl_result.whitelist,
        criteria=wl_result.criteria,
        candidates=wl_result.candidates,
        output_dir=data_dir,
    )

    # Sync failStreak back to AddressDailyState shards before retention flush (§4)
    eval_reasons_by_addr = {ev.address.lower(): ev.reject_reasons for ev in evaluations}
    for addr in set(eval_reasons_by_addr) | set(wl_result.state.addresses):
        if addr in skipped_addresses:
            continue
        addr_state = store.get_address_state(addr)
        if addr_state is None:
            continue
        if addr in wl_result.state.addresses:
            new_fail = wl_result.state.addresses[addr].failStreak
        else:
            reasons = eval_reasons_by_addr.get(addr, [])
            new_fail = 0 if len(reasons) == 0 else addr_state.failStreak + 1
        if new_fail != addr_state.failStreak:
            addr_state.failStreak = new_fail
            store.put_address_state(addr_state)

    prune_stats = store.prune_daily_state(
        now_ms=eval_ts,
        cfg=cfg,
        active_stage1_addresses=active_stage1_addrs,
        seed_addresses=seeds,
        ever_whitelisted_addresses=wl_result.state.addresses.keys(),
        skipped_addresses=skipped_addresses,
    )

    rl_stats = limiter.stats_dict()
    store.append_run_log(
        "daily",
        {
            "job": "score",
            "ts": eval_ts,
            "offline": offline,
            "evaluatedCount": len(evaluations),
            "passedCount": wl_result.criteria.current.stats.passed,
            "activeWhitelistCount": wl_result.active_count,
            "expandedSubaccounts": expanded_count,
            "errors": errors,
            "newlyEntered": len(wl_result.newly_entered),
            "newlyExited": len(wl_result.newly_exited),
            "pruneStats": prune_stats,
            "warnings": warnings,
            "rateLimitStats": rl_stats,
        },
        max_entries=cfg.stateRetention.runsLogMaxEntries.value,
    )

    return ScoreRunSummary(
        whitelist_update=wl_result,
        evaluated_count=len(evaluations),
        expanded_subaccounts=expanded_count,
        errors=errors,
        prune_stats=prune_stats,
        written_files=written_files,
        warnings=warnings,
        rate_limit_stats=rl_stats,
    )

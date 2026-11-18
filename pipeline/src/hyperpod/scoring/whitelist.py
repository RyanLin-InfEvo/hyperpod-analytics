# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conditional whitelist state machine, composite scores, criteria history & coin tracking."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import Field, StrictStr

from hyperpod.config import PipelineConfig
from hyperpod.models.api import AccountRole
from hyperpod.models.candidates import CandidateRow, CandidatesFile
from hyperpod.models.common import (
    ContractModel,
    DateStr,
    EvmAddress,
    NonEmptyStr,
    NonNegInt,
    RejectReason,
    UnitInterval,
)
from hyperpod.models.criteria import (
    CriteriaChange,
    CriteriaCurrent,
    CriteriaFile,
    CriteriaHistoryItem,
    CriteriaMembership,
    CriteriaScope,
    CriteriaStats,
    RuleValue,
)
from hyperpod.models.manifest import CoinTrack
from hyperpod.models.whitelist import (
    MembershipInterval,
    Whitelist,
    WhitelistAddress,
    WhitelistMetrics,
)
from hyperpod.util.time import ms_to_utc_date, next_utc_day_start_ms

_REJECT_REASON_ORDER: tuple[RejectReason, ...] = (
    "role",
    "hft",
    "mm",
    "funding",
    "hedge",
    "passive",
    "sample",
    "mdd",
    "pf",
    "lucky",
)


class WhitelistMemberRecord(ContractModel):
    """Persistent whitelist member state stored inside ``state/daily/whitelist_state.json``."""

    id: NonNegInt
    address: EvmAddress
    label: StrictStr | None = None
    master: EvmAddress | None = None
    subAccounts: list[EvmAddress] = Field(default_factory=list)
    coverageStart: NonNegInt
    score: UnitInterval
    metrics: WhitelistMetrics
    membership: list[MembershipInterval] = Field(min_length=1)
    failStreak: NonNegInt = 0
    abstraction: StrictStr | None = None

    @property
    def is_active(self) -> bool:
        """Return True when the latest membership interval is open (``exitTs is None``)."""
        return self.membership[-1][1] is None

    def to_public(self) -> WhitelistAddress:
        """Convert to public ``whitelist.json`` contract model."""
        return WhitelistAddress(
            id=self.id,
            address=self.address,
            label=self.label,
            master=self.master,
            subAccounts=list(self.subAccounts),
            coverageStart=self.coverageStart,
            score=self.score,
            metrics=self.metrics,
            membership=list(self.membership),
        )


class WhitelistStateRecord(ContractModel):
    """Full daily whitelist, criteria, and tracked-coin state (``whitelist_state.json``)."""

    nextAddrId: NonNegInt = 0
    addresses: dict[EvmAddress, WhitelistMemberRecord] = Field(default_factory=dict)
    criteria: CriteriaFile | None = None
    coins: list[CoinTrack] = Field(default_factory=list)
    dynamicCoinSticky: dict[NonEmptyStr, NonNegInt] = Field(default_factory=dict)
    coinBackfillQueue: list[NonEmptyStr] = Field(default_factory=list)
    lastCoinUpdateDate: DateStr | None = None
    launchedAt: NonNegInt | None = None


@dataclass
class CandidateEvaluation:
    """Evaluated Stage 2 candidate passed into whitelist membership and output export."""

    address: str
    role: AccountRole | None
    master: str | None = None
    sub_accounts: list[str] = field(default_factory=list)
    label: str | None = None
    coverage_start: int | None = None
    abstraction: str | None = None
    metrics: WhitelistMetrics | None = None
    reject_reasons: list[RejectReason] = field(default_factory=list)
    open_positions_notional: dict[str, float] = field(default_factory=dict)


@dataclass
class WhitelistUpdateResult:
    """Outputs produced by a daily whitelist, criteria, candidates, and coin-tracking update."""

    state: WhitelistStateRecord
    whitelist: Whitelist
    criteria: CriteriaFile
    candidates: CandidatesFile
    warnings: list[str] = field(default_factory=list)
    newly_entered: list[str] = field(default_factory=list)
    newly_exited: list[str] = field(default_factory=list)
    active_count: int = 0


def _percentile_ranks(
    values_by_key: Mapping[str, float | None],
    *,
    none_is_top: bool = False,
) -> dict[str, float]:
    n = len(values_by_key)
    if n == 0:
        return {}
    denom = max(n - 1, 1)
    present = sorted((val, key) for key, val in values_by_key.items() if val is not None)
    out: dict[str, float] = {}
    i = 0
    while i < len(present):
        j = i + 1
        while j < len(present) and present[j][0] == present[i][0]:
            j += 1
        avg_rank = (i + j - 1) / 2.0
        pct = 1.0 if n == 1 else avg_rank / denom
        for k in range(i, j):
            out[present[k][1]] = pct
        i = j
    if none_is_top:
        for key, val in values_by_key.items():
            if val is None:
                out[key] = 1.0
    return out


def compute_composite_scores(
    metrics_by_addr: Mapping[str, WhitelistMetrics],
    cfg: PipelineConfig,
) -> dict[str, float]:
    """Compute display-only composite percentile scores over active passing members (§5.6)."""
    if not metrics_by_addr:
        return {}

    pf_pct = _percentile_ranks(
        {addr: m.profitFactorRobust for addr, m in metrics_by_addr.items()},
        none_is_top=True,
    )
    roi_pct = _percentile_ranks({addr: m.roiAllTime for addr, m in metrics_by_addr.items()})
    mdd_pct = _percentile_ranks({addr: 1.0 - m.maxDrawdown for addr, m in metrics_by_addr.items()})
    mret_pct = _percentile_ranks(
        {addr: m.pnlMonth / max(m.accountValue, 1.0) for addr, m in metrics_by_addr.items()}
    )
    wr_pct = _percentile_ranks({addr: m.winRate for addr, m in metrics_by_addr.items()})
    av_pct = _percentile_ranks(
        {addr: math.log(max(m.accountValue, 1.0)) for addr, m in metrics_by_addr.items()}
    )

    weights = cfg.scoring.weights
    scores: dict[str, float] = {}
    for addr in metrics_by_addr:
        raw = (
            weights.profitFactorRobust.value * pf_pct[addr]
            + weights.roiAllTime.value * roi_pct[addr]
            + weights.invMaxDrawdown.value * mdd_pct[addr]
            + weights.monthlyReturn.value * mret_pct[addr]
            + weights.winRate.value * wr_pct[addr]
            + weights.logAccountValue.value * av_pct[addr]
        )
        scores[addr] = min(1.0, max(0.0, round(raw, 6)))
    return scores


def _criteria_payload_for_hash(cfg: PipelineConfig) -> dict[str, object]:
    return {
        "stage1": cfg.export_stage1_rules(),
        "gates": cfg.export_gate_rules(),
        "membership": {
            "exitAfterFailedDays": cfg.membership.exitAfterFailedDays.value,
            "subaccounts": cfg.membership.subaccounts.value,
            "excludeVaults": cfg.membership.excludeVaults.value,
        },
        "scope": {
            "perpDex": cfg.scope.perpDex.value,
            "spot": cfg.scope.spot.value,
            "hip3": cfg.scope.hip3.value,
        },
    }


def compute_criteria_hash(cfg: PipelineConfig) -> str:
    """Compute canonical ``sha256:...`` hash of exported filtering rules (§3.6)."""
    canonical = json.dumps(
        _criteria_payload_for_hash(cfg),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _flatten_criteria_values(
    stage1_rules: Sequence[Mapping[str, object]],
    gate_rules: Sequence[Mapping[str, object]],
    membership: CriteriaMembership,
    scope: CriteriaScope,
) -> dict[str, RuleValue]:
    out: dict[str, RuleValue] = {}
    for rule in stage1_rules:
        out[f"stage1.{rule['key']}"] = rule["value"]  # type: ignore[assignment]
    for rule in gate_rules:
        out[str(rule["key"])] = rule["value"]  # type: ignore[assignment]
    out["membership.exitAfterFailedDays"] = membership.exitAfterFailedDays
    out["membership.subaccounts"] = membership.subaccounts
    out["membership.excludeVaults"] = membership.excludeVaults
    out["scope.perpDex"] = scope.perpDex
    out["scope.spot"] = scope.spot
    out["scope.hip3"] = scope.hip3
    return out


def update_criteria_state(
    cfg: PipelineConfig,
    prior: CriteriaFile | None,
    *,
    eval_ts_ms: int,
    candidates: Sequence[CandidateRow],
) -> CriteriaFile:
    """Export ``criteria.json`` from ``cfg``, bumping ``version`` when rule hash changes (§3.6)."""
    stage1_raw = cfg.export_stage1_rules()
    gate_raw = cfg.export_gate_rules()
    membership_model = CriteriaMembership(
        exitAfterFailedDays=cfg.membership.exitAfterFailedDays.value,
        subaccounts=cfg.membership.subaccounts.value,
        excludeVaults=cfg.membership.excludeVaults.value,
    )
    scope_model = CriteriaScope(
        perpDex=cfg.scope.perpDex.value,
        spot=cfg.scope.spot.value,
        hip3=cfg.scope.hip3.value,
    )
    new_hash = compute_criteria_hash(cfg)

    passed_count = sum(1 for row in candidates if row.passed)
    rej_counts: Counter[RejectReason] = Counter()
    for row in candidates:
        for reason in row.rejectReasons:
            rej_counts[reason] += 1
    rejected_by = {
        reason: rej_counts[reason] for reason in _REJECT_REASON_ORDER if rej_counts[reason] > 0
    }
    stats = CriteriaStats(
        candidates=len(candidates),
        passed=passed_count,
        rejectedBy=rejected_by,
    )

    if prior is None:
        version = 1
        effective_from = eval_ts_ms
        history: list[CriteriaHistoryItem] = []
    elif prior.current.hash == new_hash:
        version = prior.current.version
        effective_from = prior.current.effectiveFrom
        history = list(prior.history)
    else:
        prior_stage1_raw = [r.model_dump() for r in prior.current.stage1]
        prior_gates_raw = [r.model_dump() for r in prior.current.gates]
        old_flat = _flatten_criteria_values(
            prior_stage1_raw,
            prior_gates_raw,
            prior.current.membership,
            prior.current.scope,
        )
        new_flat = _flatten_criteria_values(
            stage1_raw,
            gate_raw,
            membership_model,
            scope_model,
        )
        changes: list[CriteriaChange] = []
        for key in sorted(set(old_flat) | set(new_flat)):
            old_val = old_flat.get(key)
            new_val = new_flat.get(key)
            if old_val != new_val or type(old_val) is not type(new_val):
                changes.append(CriteriaChange(key=key, from_=old_val, to=new_val))
        effective_to = max(prior.current.effectiveFrom, eval_ts_ms)
        history = [
            *prior.history,
            CriteriaHistoryItem(
                version=prior.current.version,
                effectiveFrom=prior.current.effectiveFrom,
                effectiveTo=effective_to,
                hash=prior.current.hash,
                changes=changes,
            ),
        ]
        version = prior.current.version + 1
        effective_from = eval_ts_ms

    current_payload = {
        "version": version,
        "effectiveFrom": effective_from,
        "hash": new_hash,
        "stage1": stage1_raw,
        "gates": gate_raw,
        "membership": membership_model.model_dump(),
        "scope": scope_model.model_dump(),
        "stats": stats.model_dump(),
    }
    current = CriteriaCurrent.model_validate(current_payload)
    return CriteriaFile(schemaVersion=1, current=current, history=history)


def update_tracked_coins(
    prior_coins: Sequence[CoinTrack],
    prior_sticky: Mapping[str, int],
    prior_backfill_queue: Sequence[str],
    *,
    active_coin_notionals: Mapping[str, float],
    cfg: PipelineConfig,
    eval_ts_ms: int,
    last_update_date: DateStr | None = None,
) -> tuple[list[CoinTrack], dict[str, int], list[str], DateStr]:
    """Update pinned and dynamic top-N tracked coins with 30-day stickiness (§3.1.1)."""
    eval_date = ms_to_utc_date(eval_ts_ms)
    same_day_rerun = last_update_date == eval_date
    effective_ts = eval_ts_ms if not prior_coins else next_utc_day_start_ms(eval_ts_ms)

    coins = [track.model_copy() for track in prior_coins]
    sticky: dict[str, int] = dict(prior_sticky)
    queue: list[str] = list(dict.fromkeys(prior_backfill_queue))

    pinned_list = list(dict.fromkeys(cfg.coins.pinned.value))
    pinned_set = set(pinned_list)

    active_by_coin: dict[str, CoinTrack] = {}
    for track in coins:
        if track.trackedTo is None:
            active_by_coin[track.coin] = track

    # Ensure every pinned coin has an open track.
    for p_coin in pinned_list:
        existing_track = active_by_coin.get(p_coin)
        if existing_track is None:
            new_track = CoinTrack(
                coin=p_coin,
                pinned=True,
                trackedFrom=effective_ts,
                trackedTo=None,
            )
            coins.append(new_track)
            active_by_coin[p_coin] = new_track
            if prior_coins and p_coin not in queue:
                queue.append(p_coin)
        else:
            existing_track.pinned = True
        sticky.pop(p_coin, None)

    # Close pinned tracks if a coin is no longer in pinned_set (let dynamic logic govern it).
    for coin, track in list(active_by_coin.items()):
        if track.pinned and coin not in pinned_set:
            track.pinned = False
            sticky.setdefault(coin, 0)

    # Top-N non-pinned main-perp coins ranked by active whitelist notional OI.
    dynamic_ranked = sorted(
        (
            (notional, coin)
            for coin, notional in active_coin_notionals.items()
            if coin not in pinned_set and notional > 0.0
        ),
        key=lambda item: (-item[0], item[1]),
    )
    top_n_coins = [coin for _, coin in dynamic_ranked[: cfg.coins.dynamicTopN.value]]
    top_n_set = set(top_n_coins)

    # Update sticky counters for currently active dynamic coins.
    for coin, track in list(active_by_coin.items()):
        if coin in pinned_set:
            continue
        if coin in top_n_set:
            sticky[coin] = 0
        else:
            if not same_day_rerun:
                sticky[coin] = sticky.get(coin, 0) + 1
            else:
                sticky.setdefault(coin, 0)
            if sticky[coin] > cfg.coins.stickyDays.value:
                if track.trackedFrom >= effective_ts:
                    if track in coins:
                        coins.remove(track)
                    if coin in queue:
                        queue.remove(coin)
                else:
                    track.trackedTo = effective_ts
                active_by_coin.pop(coin, None)
                sticky.pop(coin, None)

    # Open tracks for newly entering top-N dynamic coins and enqueue for backfill.
    for coin in top_n_coins:
        if coin not in active_by_coin:
            new_track = CoinTrack(
                coin=coin,
                pinned=False,
                trackedFrom=effective_ts,
                trackedTo=None,
            )
            coins.append(new_track)
            active_by_coin[coin] = new_track
            sticky[coin] = 0
            if coin not in queue:
                queue.append(coin)

    # Enforce hard maxTracked cap by evicting lowest-notional dynamic coins.
    active_pinned_count = sum(1 for c in active_by_coin if c in pinned_set)
    max_dynamic = max(0, cfg.coins.maxTracked.value - active_pinned_count)
    active_dynamic = [c for c in active_by_coin if c not in pinned_set]
    if len(active_dynamic) > max_dynamic:
        evict_order = sorted(
            active_dynamic,
            key=lambda c: (
                active_coin_notionals.get(c, 0.0),
                1 if c in top_n_set else 0,
                -sticky.get(c, 0),
                c,
            ),
        )
        n_evict = len(active_dynamic) - max_dynamic
        for coin in evict_order[:n_evict]:
            track = active_by_coin.pop(coin)
            if track.trackedFrom >= effective_ts:
                if track in coins:
                    coins.remove(track)
            else:
                track.trackedTo = effective_ts
            sticky.pop(coin, None)
            if coin in queue:
                queue.remove(coin)

    return coins, sticky, queue, eval_date


def _split_or_keep_open_interval(
    membership: list[MembershipInterval],
    *,
    ts_ms: int,
    criteria_version: int,
) -> list[MembershipInterval]:
    out = list(membership)
    enter_ts, exit_ts, old_ver = out[-1]
    if exit_ts is not None:
        out.append((ts_ms, None, criteria_version))
        return out
    if old_ver != criteria_version:
        split_ts = max(enter_ts, ts_ms)
        out[-1] = (enter_ts, split_ts, old_ver)
        out.append((split_ts, None, criteria_version))
    return out


def update_whitelist_membership(
    prior_state: WhitelistStateRecord | None,
    evaluations: Sequence[CandidateEvaluation],
    cfg: PipelineConfig,
    *,
    eval_ts_ms: int,
    blocklist: Iterable[str] = (),
    skipped_addresses: Iterable[str] = (),
    skipped_open_notionals: Mapping[str, Mapping[str, float]] | None = None,
) -> WhitelistUpdateResult:
    """Apply §5.6 whitelist entry/exit hysteresis, §3.6 criteria versioning, and §3.1.1 coins."""
    state = prior_state.model_copy(deep=True) if prior_state is not None else WhitelistStateRecord()
    block_set = {addr.lower() for addr in blocklist}
    skipped_set = {addr.lower() for addr in skipped_addresses}
    skipped_ntl_map = {k.lower(): v for k, v in (skipped_open_notionals or {}).items()}
    exit_after = cfg.membership.exitAfterFailedDays.value

    candidate_rows: list[CandidateRow] = []
    eval_by_addr: dict[str, CandidateEvaluation] = {}
    cand_idx = 0
    for ev in evaluations:
        addr = ev.address.lower()
        if addr in block_set:
            continue
        eval_by_addr[addr] = ev
        passed = len(ev.reject_reasons) == 0 and ev.metrics is not None
        m = ev.metrics
        candidate_rows.append(
            CandidateRow(
                idx=cand_idx,
                passed=passed,
                rejectReasons=list(ev.reject_reasons),
                fillsPerDay=m.fillsPerDay if m is not None else None,
                makerRatio=m.makerRatio if m is not None else None,
                medianHoldMinutes=m.medianHoldMinutes if m is not None else None,
                maxDrawdown=m.maxDrawdown if m is not None else None,
                profitFactorRobust=m.profitFactorRobust if m is not None else None,
                turnoverMonth=m.turnoverMonth if m is not None else None,
                nTrades=m.nTrades if m is not None else None,
                historyDays=m.historyDays if m is not None else None,
                fundingShare=m.fundingShare if m is not None else None,
                hedgeRatio=m.hedgeRatio if m is not None else None,
                topWinShare=m.topWinShare if m is not None else None,
            )
        )
        cand_idx += 1

    criteria_file = update_criteria_state(
        cfg,
        state.criteria,
        eval_ts_ms=eval_ts_ms,
        candidates=candidate_rows,
    )
    crit_ver = criteria_file.current.version

    newly_entered: list[str] = []
    newly_exited: list[str] = []
    passed_active_metrics: dict[str, WhitelistMetrics] = {}

    all_known_addrs = sorted(set(state.addresses) | set(eval_by_addr))
    for addr in all_known_addrs:
        existing = state.addresses.get(addr)
        cand_ev = eval_by_addr.get(addr)
        is_blocked = addr in block_set
        passed_today = (
            cand_ev is not None
            and not is_blocked
            and len(cand_ev.reject_reasons) == 0
            and cand_ev.metrics is not None
        )

        if passed_today:
            assert cand_ev is not None and cand_ev.metrics is not None
            cov_start = cand_ev.coverage_start if cand_ev.coverage_start is not None else eval_ts_ms
            if existing is None:
                assigned_id = state.nextAddrId
                state.nextAddrId += 1
                state.addresses[addr] = WhitelistMemberRecord(
                    id=assigned_id,
                    address=addr,
                    label=cand_ev.label,
                    master=cand_ev.master.lower() if cand_ev.master is not None else None,
                    subAccounts=sorted({s.lower() for s in cand_ev.sub_accounts}),
                    coverageStart=cov_start,
                    score=0.0,
                    metrics=cand_ev.metrics,
                    membership=[(eval_ts_ms, None, crit_ver)],
                    failStreak=0,
                    abstraction=cand_ev.abstraction,
                )
                newly_entered.append(addr)
            else:
                was_active = existing.is_active
                existing.membership = _split_or_keep_open_interval(
                    existing.membership,
                    ts_ms=eval_ts_ms,
                    criteria_version=crit_ver,
                )
                existing.failStreak = 0
                if cand_ev.label is not None:
                    existing.label = cand_ev.label
                if cand_ev.master is not None:
                    existing.master = cand_ev.master.lower()
                if cand_ev.sub_accounts:
                    existing.subAccounts = sorted({s.lower() for s in cand_ev.sub_accounts})
                if cand_ev.coverage_start is not None:
                    existing.coverageStart = min(existing.coverageStart, cand_ev.coverage_start)
                if cand_ev.abstraction is not None:
                    existing.abstraction = cand_ev.abstraction
                existing.metrics = cand_ev.metrics
                if not was_active:
                    newly_entered.append(addr)
            passed_active_metrics[addr] = cand_ev.metrics
            continue

        if existing is None:
            continue

        # Existing member did not pass today (or was omitted/blocklisted).
        if cand_ev is not None:
            if cand_ev.label is not None:
                existing.label = cand_ev.label
            if cand_ev.master is not None:
                existing.master = cand_ev.master.lower()
            if cand_ev.sub_accounts:
                existing.subAccounts = sorted({s.lower() for s in cand_ev.sub_accounts})
            if cand_ev.coverage_start is not None:
                existing.coverageStart = min(existing.coverageStart, cand_ev.coverage_start)
            if cand_ev.abstraction is not None:
                existing.abstraction = cand_ev.abstraction
            if cand_ev.metrics is not None:
                existing.metrics = cand_ev.metrics

        if not existing.is_active:
            continue

        if cand_ev is None and not is_blocked and addr in skipped_set:
            existing.membership = _split_or_keep_open_interval(
                existing.membership,
                ts_ms=eval_ts_ms,
                criteria_version=crit_ver,
            )
            continue

        existing.failStreak += 1
        should_exit = is_blocked or exit_after == 0 or existing.failStreak >= exit_after
        if should_exit:
            enter_ts, _, old_ver = existing.membership[-1]
            existing.membership[-1] = (enter_ts, max(enter_ts, eval_ts_ms), old_ver)
            newly_exited.append(addr)
        else:
            existing.membership = _split_or_keep_open_interval(
                existing.membership,
                ts_ms=eval_ts_ms,
                criteria_version=crit_ver,
            )

    scores = compute_composite_scores(passed_active_metrics, cfg)
    for addr, score_val in scores.items():
        state.addresses[addr].score = score_val

    active_members = [m for m in state.addresses.values() if m.is_active]
    warnings: list[str] = []
    if len(active_members) > cfg.membership.safetyCapWarning.value:
        warnings.append(
            f"Active whitelist size ({len(active_members)}) exceeds "
            f"safetyCapWarning ({cfg.membership.safetyCapWarning.value}); "
            "consider tightening criteria in config/pipeline.yaml."
        )

    active_coin_notionals: dict[str, float] = {}
    for member in active_members:
        member_ev = eval_by_addr.get(member.address)
        pos_map: Mapping[str, float]
        if member_ev is not None:
            pos_map = member_ev.open_positions_notional
        elif member.address in skipped_set and member.address in skipped_ntl_map:
            pos_map = skipped_ntl_map[member.address]
        else:
            continue
        for coin, notional in pos_map.items():
            if notional > 0.0:
                active_coin_notionals[coin] = active_coin_notionals.get(coin, 0.0) + notional

    coins, sticky, queue, coin_date = update_tracked_coins(
        state.coins,
        state.dynamicCoinSticky,
        state.coinBackfillQueue,
        active_coin_notionals=active_coin_notionals,
        cfg=cfg,
        eval_ts_ms=eval_ts_ms,
        last_update_date=state.lastCoinUpdateDate,
    )
    state.criteria = criteria_file
    state.coins = coins
    state.dynamicCoinSticky = sticky
    state.coinBackfillQueue = queue
    state.lastCoinUpdateDate = coin_date

    public_addresses = [
        member.to_public() for member in sorted(state.addresses.values(), key=lambda rec: rec.id)
    ]
    whitelist = Whitelist(
        schemaVersion=1,
        version=ms_to_utc_date(eval_ts_ms),
        addresses=public_addresses,
    )
    candidates_file = CandidatesFile(
        schemaVersion=1,
        generatedAt=eval_ts_ms,
        criteriaVersion=crit_ver,
        candidates=candidate_rows,
    )

    return WhitelistUpdateResult(
        state=state,
        whitelist=whitelist,
        criteria=criteria_file,
        candidates=candidates_file,
        warnings=warnings,
        newly_entered=sorted(newly_entered),
        newly_exited=sorted(newly_exited),
        active_count=len(active_members),
    )

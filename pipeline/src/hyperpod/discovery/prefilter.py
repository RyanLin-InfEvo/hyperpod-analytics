# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stage 1 candidate discovery, seed/blocklist parsing, and subaccount expansion (§5.1)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from hyperpod.client.info_api import compute_account_equity
from hyperpod.config import PipelineConfig
from hyperpod.models.api import LeaderboardResponse, LeaderboardRow, SubAccountItem

_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
CandidateSource = Literal["leaderboard", "seed", "subAccount", "active_whitelist"]


def normalize_address(address: str) -> str:
    """Validate and normalize an EVM address to lowercase ``0x`` + 40 hex chars."""
    stripped = address.strip()
    if not _ADDRESS_RE.match(stripped):
        raise ValueError(f"Invalid EVM address: {address!r}")
    return stripped.lower()


def load_address_list(path: Path) -> list[str]:
    """Load deduplicated lowercase EVM addresses from a config text file.

    Ignores blank lines and ``#`` comments. Returns an empty list if ``path`` does not exist.
    """
    if not path.exists():
        return []
    seen: set[str] = set()
    result: list[str] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        cleaned = raw_line.split("#", 1)[0].strip()
        if not cleaned:
            continue
        try:
            addr = normalize_address(cleaned)
        except ValueError as exc:
            raise ValueError(f"{path}:{line_no}: {exc}") from exc
        if addr not in seen:
            seen.add(addr)
            result.append(addr)
    return result


@dataclass(frozen=True)
class Stage1Candidate:
    """Candidate selected for Stage 2 evaluation."""

    address: str
    source: CandidateSource
    display_name: str | None = None
    master: str | None = None
    leaderboard_account_value: float | None = None
    leaderboard_pnl_all_time: float | None = None
    leaderboard_roi_all_time: float | None = None
    leaderboard_pnl_month: float | None = None
    leaderboard_vlm_month: float | None = None


@dataclass(frozen=True)
class Stage1PrefilterResult:
    """Output of Stage 1 leaderboard prefiltering plus seed and active-whitelist additions."""

    candidates: list[Stage1Candidate]
    total_leaderboard_rows: int
    passed_leaderboard_count: int
    seed_added_count: int
    active_whitelist_added_count: int
    blocklisted_count: int
    rejected_counts: dict[str, int]


def _candidate_from_leaderboard_row(
    address: str,
    source: CandidateSource,
    row: LeaderboardRow | None,
) -> Stage1Candidate:
    if row is None:
        return Stage1Candidate(address=address, source=source)
    return Stage1Candidate(
        address=address,
        source=source,
        display_name=row.displayName,
        master=None,
        leaderboard_account_value=row.account_value_float,
        leaderboard_pnl_all_time=row.all_time_pnl,
        leaderboard_roi_all_time=row.all_time_roi,
        leaderboard_pnl_month=row.month_pnl,
        leaderboard_vlm_month=row.month_vlm,
    )


def prefilter_leaderboard(
    leaderboard: LeaderboardResponse,
    cfg: PipelineConfig,
    *,
    seed_addresses: Sequence[str] = (),
    blocklist: Iterable[str] = (),
    active_whitelist_addresses: Iterable[str] = (),
) -> Stage1PrefilterResult:
    """Filter leaderboard rows through Stage 1 thresholds and merge seed/active addresses (§5.1).

    Stage 1 thresholds (`cfg.stage1`):
    - ``accountValue >= minAccountValue`` (and ``> 0``)
    - ``allTime.pnl > minPnlAllTime``
    - ``month.pnl > minPnlMonthRatio * accountValue``
    - ``month.vlm / accountValue <= maxTurnoverMonth``
    - not in ``blocklist``
    """
    block_set = {normalize_address(addr) for addr in blocklist}
    rejected_counts: dict[str, int] = {
        "minAccountValue": 0,
        "minPnlAllTime": 0,
        "minPnlMonthRatio": 0,
        "maxTurnoverMonth": 0,
        "blocklist": 0,
    }

    leaderboard_index: dict[str, LeaderboardRow] = {}
    candidates: list[Stage1Candidate] = []
    seen_candidates: set[str] = set()
    blocklisted_count = 0

    min_av = cfg.stage1.minAccountValue.value
    min_all_time_pnl = cfg.stage1.minPnlAllTime.value
    min_month_ratio = cfg.stage1.minPnlMonthRatio.value
    max_turnover = cfg.stage1.maxTurnoverMonth.value

    for row in leaderboard.leaderboardRows:
        addr = row.ethAddress.lower()
        if addr not in leaderboard_index:
            leaderboard_index[addr] = row
        else:
            # Skip duplicate leaderboard rows after first occurrence.
            continue

        if addr in block_set:
            rejected_counts["blocklist"] += 1
            blocklisted_count += 1
            continue

        av = row.account_value_float
        all_time_pnl = row.all_time_pnl
        month_pnl = row.month_pnl
        month_vlm = row.month_vlm

        failed = False
        if av < min_av or av <= 0.0:
            rejected_counts["minAccountValue"] += 1
            failed = True
        if not (all_time_pnl > min_all_time_pnl):
            rejected_counts["minPnlAllTime"] += 1
            failed = True
        if not (month_pnl > min_month_ratio * av):
            rejected_counts["minPnlMonthRatio"] += 1
            failed = True
        turnover = (month_vlm / av) if av > 0.0 else (float("inf") if month_vlm > 0.0 else 0.0)
        if not (turnover <= max_turnover):
            rejected_counts["maxTurnoverMonth"] += 1
            failed = True

        if not failed:
            seen_candidates.add(addr)
            candidates.append(_candidate_from_leaderboard_row(addr, "leaderboard", row))

    passed_leaderboard_count = len(candidates)

    seed_added_count = 0
    for raw_seed in seed_addresses:
        addr = normalize_address(raw_seed)
        if addr in block_set:
            continue
        if addr not in seen_candidates:
            seen_candidates.add(addr)
            candidates.append(
                _candidate_from_leaderboard_row(addr, "seed", leaderboard_index.get(addr))
            )
            seed_added_count += 1

    active_whitelist_added_count = 0
    for raw_active in active_whitelist_addresses:
        addr = normalize_address(raw_active)
        if addr in block_set:
            continue
        if addr not in seen_candidates:
            seen_candidates.add(addr)
            candidates.append(
                _candidate_from_leaderboard_row(
                    addr, "active_whitelist", leaderboard_index.get(addr)
                )
            )
            active_whitelist_added_count += 1

    return Stage1PrefilterResult(
        candidates=candidates,
        total_leaderboard_rows=len(leaderboard.leaderboardRows),
        passed_leaderboard_count=passed_leaderboard_count,
        seed_added_count=seed_added_count,
        active_whitelist_added_count=active_whitelist_added_count,
        blocklisted_count=blocklisted_count,
        rejected_counts=rejected_counts,
    )


def estimate_subaccount_equity(item: SubAccountItem, abstraction: str | None = None) -> float:
    """Estimate subaccount equity from abstraction mode or ``spotState`` cues (ADR-0001/0003)."""
    if abstraction is not None:
        return compute_account_equity(item.clearinghouseState, abstraction, item.spotState)[0]
    if (
        item.spotState.portfolioMarginEnabled is True
        or item.spotState.tokenToAvailableAfterMaintenance is not None
    ):
        return compute_account_equity(item.clearinghouseState, "portfolioMargin", item.spotState)[0]
    return float(item.clearinghouseState.marginSummary.accountValue)


def expand_subaccounts(
    sub_accounts: Sequence[SubAccountItem],
    cfg: PipelineConfig,
    *,
    blocklist: Iterable[str] = (),
    existing_addresses: Iterable[str] = (),
    abstraction_by_address: Mapping[str, str] | None = None,
) -> list[Stage1Candidate]:
    """Filter a master user's subaccounts meeting ``stage1.minAccountValue`` (§5.1, ADR-0003)."""
    block_set = {normalize_address(addr) for addr in blocklist}
    existing_set = {normalize_address(addr) for addr in existing_addresses}
    abs_map = {k.lower(): v for k, v in (abstraction_by_address or {}).items()}
    min_av = cfg.stage1.minAccountValue.value

    expanded: list[Stage1Candidate] = []
    seen_subs: set[str] = set()
    for item in sub_accounts:
        sub_addr = item.subAccountUser.lower()
        if sub_addr in seen_subs or sub_addr in block_set or sub_addr in existing_set:
            continue
        seen_subs.add(sub_addr)
        equity = estimate_subaccount_equity(item, abs_map.get(sub_addr))
        if equity < min_av or equity <= 0.0:
            continue
        expanded.append(
            Stage1Candidate(
                address=sub_addr,
                source="subAccount",
                display_name=item.name or None,
                master=item.master.lower(),
            )
        )
    return expanded

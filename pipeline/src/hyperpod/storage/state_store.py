# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Partition-isolated internal state store for the ``state`` branch (§4)."""

from __future__ import annotations

import gzip
import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, StrictBool, StrictStr

from hyperpod.config import PipelineConfig
from hyperpod.models.api import AccountRole
from hyperpod.models.candidates import CandidatesFile
from hyperpod.models.common import (
    ContractModel,
    DateStr,
    EvmAddress,
    JsonNumber,
    NonEmptyStr,
    NonNegInt,
    NonNegNumber,
)
from hyperpod.models.criteria import CriteriaFile
from hyperpod.models.whitelist import Whitelist
from hyperpod.scoring.trades import ClosedTradeTuple, FillDedupKey, OpenTradeState
from hyperpod.scoring.whitelist import WhitelistStateRecord
from hyperpod.util.time import recent_utc_dates

StateOwner = Literal["daily", "fast", "bootstrap"]
StatePartition = Literal["daily", "fast", "bootstrap"]
_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_SHARD_RE = re.compile(r"^[0-9a-f]{2}$")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, data: Any, *, compress: bool = False) -> None:
    if compress:
        raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload = gzip.compress(raw, compresslevel=6, mtime=0)
    else:
        raw_text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        payload = raw_text.encode("utf-8")
    _atomic_write_bytes(path, payload)


def _read_json(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(path.read_text(encoding="utf-8"))


class FillStatsState(ContractModel):
    """Per-UTC-day main-perp fill counts and notionals (§4)."""

    days: dict[DateStr, tuple[NonNegInt, NonNegInt]] = Field(default_factory=dict)
    notionalDays: dict[DateStr, NonNegNumber] = Field(default_factory=dict)


class DiscoveredCandidateRecord(ContractModel):
    """Serializable Stage 1 / expanded candidate queue entry."""

    address: EvmAddress
    source: Literal["leaderboard", "seed", "subAccount", "active_whitelist"]
    displayName: StrictStr | None = None
    master: EvmAddress | None = None
    leaderboardAccountValue: JsonNumber | None = None
    leaderboardPnlAllTime: JsonNumber | None = None
    leaderboardRoiAllTime: JsonNumber | None = None
    leaderboardPnlMonth: JsonNumber | None = None
    leaderboardVlmMonth: NonNegNumber | None = None


class AddressDailyState(ContractModel):
    """Per-address scoring state stored inside ``state/daily/addresses/{shard}.json.gz`` (§4).

    Never stores raw candidate fills (Hard Invariant #4); only incremental cursors,
    bounded overlap dedup keys, reconstructed round-trip trades, daily stats, and
    last computed scoring summary.
    """

    address: EvmAddress
    role: AccountRole | None = None
    master: EvmAddress | None = None
    subAccounts: list[EvmAddress] = Field(default_factory=list)
    eligibleSubAccounts: list[DiscoveredCandidateRecord] = Field(default_factory=list)
    subAccountsUpdatedAt: NonNegInt | None = None
    abstraction: StrictStr | None = None
    label: StrictStr | None = None
    fillsCursorScoring: NonNegInt = 0
    lastFillFetchAt: NonNegInt | None = None
    fillsHistoryStart: NonNegInt | None = None
    lastFillTime: NonNegInt | None = None
    truncated: StrictBool = False
    recentScoringFillKeys: list[FillDedupKey] = Field(default_factory=list)
    lastFundingTime: NonNegInt | None = None
    lastFundingCoins: list[StrictStr] = Field(default_factory=list)
    cumFundingAbs: NonNegNumber = 0.0
    cumFundingNet: JsonNumber = 0.0
    openTrades: dict[StrictStr, OpenTradeState] = Field(default_factory=dict)
    closedTrades: list[ClosedTradeTuple] = Field(default_factory=list)
    fillStats: FillStatsState = Field(default_factory=FillStatsState)
    lastScore: dict[str, Any] = Field(default_factory=dict)
    failStreak: NonNegInt = 0
    absentStage1Streak: NonNegInt = 0


class DiscoveredCandidatesFile(ContractModel):
    """Persisted output of ``hyperpod discover`` in ``state/daily/discovered_candidates.json``."""

    generatedAt: NonNegInt
    totalLeaderboardRows: NonNegInt
    passedLeaderboardCount: NonNegInt
    seedAddedCount: NonNegInt
    activeWhitelistAddedCount: NonNegInt
    blocklistedCount: NonNegInt
    usedLeaderboardFallback: StrictBool
    rejectedCounts: dict[NonEmptyStr, NonNegInt]
    warnings: list[StrictStr] = Field(default_factory=list)
    candidates: list[DiscoveredCandidateRecord]


class BackfillCursorFile(ContractModel):
    """Resumable checkpoint state in ``state/daily/backfill_cursor.json``."""

    startedAt: NonNegInt
    updatedAt: NonNegInt
    nextIndex: NonNegInt
    totalCandidates: NonNegInt
    processedCount: NonNegInt
    completed: StrictBool
    queue: list[DiscoveredCandidateRecord]
    expandedSubaccounts: NonNegInt = 0


def address_shard(address: str) -> str:
    """Return the 2-hex-char shard identifier (``00``–``ff``) for an EVM address (§4)."""
    norm = address.strip().lower()
    if not _ADDRESS_RE.match(norm):
        raise ValueError(f"Invalid EVM address for sharding: {address!r}")
    return norm[2:4]


class StateStore:
    """Partition-isolated reader/writer for ``state/fast/``, ``daily/``, and ``bootstrap/``."""

    def __init__(self, state_dir: Path, *, owner: StateOwner = "daily") -> None:
        self.state_dir = state_dir
        self.owner: StateOwner = owner
        self._shard_cache: dict[str, dict[str, AddressDailyState]] = {}
        self._dirty_shards: set[str] = set()

    @property
    def daily_dir(self) -> Path:
        return self.state_dir / "daily"

    @property
    def fast_dir(self) -> Path:
        return self.state_dir / "fast"

    @property
    def bootstrap_dir(self) -> Path:
        return self.state_dir / "bootstrap"

    @property
    def addresses_dir(self) -> Path:
        return self.daily_dir / "addresses"

    @property
    def leaderboard_cache_path(self) -> Path:
        return self.daily_dir / "leaderboard_last.json.gz"

    @property
    def whitelist_state_path(self) -> Path:
        return self.daily_dir / "whitelist_state.json"

    @property
    def discovered_candidates_path(self) -> Path:
        return self.daily_dir / "discovered_candidates.json"

    @property
    def backfill_cursor_path(self) -> Path:
        return self.daily_dir / "backfill_cursor.json"

    @property
    def latest_outputs_dir(self) -> Path:
        return self.daily_dir / "latest_outputs"

    def _require_write_owner(self, partition: StatePartition) -> None:
        if self.owner == "bootstrap":
            return
        if self.owner != partition:
            raise PermissionError(
                f"StateStore owner {self.owner!r} is forbidden from writing to "
                f"partition {partition!r} (§4 state ownership isolation)"
            )

    def shard_path(self, shard: str) -> Path:
        if not _SHARD_RE.match(shard):
            raise ValueError(
                f"Invalid shard identifier (expected 2 lowercase hex chars): {shard!r}"
            )
        return self.addresses_dir / f"{shard}.json.gz"

    def load_shard(self, shard: str) -> dict[str, AddressDailyState]:
        """Load and validate a single hex shard ``00``–``ff``, caching in memory."""
        cached = self._shard_cache.get(shard)
        if cached is not None:
            return cached
        path = self.shard_path(shard)
        if not path.is_file():
            self._shard_cache[shard] = {}
            return self._shard_cache[shard]
        raw = _read_json(path)
        if not isinstance(raw, dict):
            raise ValueError(f"Expected dict in address shard {path}, got {type(raw).__name__}")
        parsed: dict[str, AddressDailyState] = {}
        for addr_key, payload in raw.items():
            rec = AddressDailyState.model_validate(payload)
            parsed[addr_key.lower()] = rec
        self._shard_cache[shard] = parsed
        return parsed

    def get_address_state(self, address: str) -> AddressDailyState | None:
        """Return deep copy of persisted ``AddressDailyState``, or ``None`` if absent."""
        norm = address.lower()
        shard = address_shard(norm)
        rec = self.load_shard(shard).get(norm)
        return rec.model_copy(deep=True) if rec is not None else None

    def put_address_state(self, state: AddressDailyState) -> None:
        """Stage an updated ``AddressDailyState`` in its shard (``daily``/``bootstrap``)."""
        self._require_write_owner("daily")
        norm = state.address.lower()
        shard = address_shard(norm)
        shard_map = self.load_shard(shard)
        shard_map[norm] = state.model_copy(deep=True)
        self._dirty_shards.add(shard)

    def flush_dirty_shards(self) -> list[str]:
        """Atomically persist all modified address shards to gzipped JSON."""
        self._require_write_owner("daily")
        flushed: list[str] = []
        for shard in sorted(self._dirty_shards):
            shard_map = self._shard_cache.get(shard, {})
            serialized = {addr: shard_map[addr].model_dump() for addr in sorted(shard_map)}
            _atomic_write_json(self.shard_path(shard), serialized, compress=True)
            flushed.append(shard)
        self._dirty_shards.clear()
        return flushed

    def iter_all_address_states(self) -> Iterator[AddressDailyState]:
        """Yield all ``AddressDailyState`` records across cached and on-disk shards."""
        shards_seen: set[str] = set(self._shard_cache.keys())
        if self.addresses_dir.is_dir():
            for path in sorted(self.addresses_dir.glob("*.json.gz")):
                shards_seen.add(path.name.removesuffix(".json.gz"))
        for shard in sorted(shards_seen):
            shard_map = self.load_shard(shard)
            for addr in sorted(shard_map):
                yield shard_map[addr]

    def load_whitelist_state(self) -> WhitelistStateRecord | None:
        """Read ``state/daily/whitelist_state.json`` (allowed for both ``fast`` and ``daily``)."""
        if not self.whitelist_state_path.is_file():
            return None
        return WhitelistStateRecord.model_validate(_read_json(self.whitelist_state_path))

    def save_whitelist_state(self, state: WhitelistStateRecord) -> None:
        """Atomically persist ``state/daily/whitelist_state.json``."""
        self._require_write_owner("daily")
        _atomic_write_json(self.whitelist_state_path, state.model_dump(), compress=False)

    def load_discovered_candidates(self) -> DiscoveredCandidatesFile | None:
        """Read ``state/daily/discovered_candidates.json`` if present."""
        if not self.discovered_candidates_path.is_file():
            return None
        return DiscoveredCandidatesFile.model_validate(_read_json(self.discovered_candidates_path))

    def save_discovered_candidates(self, payload: DiscoveredCandidatesFile) -> None:
        """Atomically persist ``state/daily/discovered_candidates.json``."""
        self._require_write_owner("daily")
        _atomic_write_json(self.discovered_candidates_path, payload.model_dump(), compress=False)

    def load_backfill_cursor(self) -> BackfillCursorFile | None:
        """Read ``state/daily/backfill_cursor.json`` if present."""
        if not self.backfill_cursor_path.is_file():
            return None
        return BackfillCursorFile.model_validate(_read_json(self.backfill_cursor_path))

    def save_backfill_cursor(self, cursor: BackfillCursorFile) -> None:
        """Atomically persist ``state/daily/backfill_cursor.json``."""
        self._require_write_owner("daily")
        _atomic_write_json(self.backfill_cursor_path, cursor.model_dump(), compress=False)

    def save_daily_outputs(
        self,
        *,
        whitelist: Whitelist,
        criteria: CriteriaFile,
        candidates: CandidatesFile,
        output_dir: Path | None = None,
    ) -> list[Path]:
        """Write ``whitelist.json``, ``criteria.json``, and ``candidates.json``."""
        self._require_write_owner("daily")
        written: list[Path] = []
        targets = [self.latest_outputs_dir]
        if output_dir is not None and output_dir.resolve() != self.latest_outputs_dir.resolve():
            targets.append(output_dir)

        for target_dir in targets:
            for name, model in (
                ("whitelist.json", whitelist),
                ("criteria.json", criteria),
                ("candidates.json", candidates),
            ):
                out_path = target_dir / name
                _atomic_write_json(out_path, model.model_dump(by_alias=True), compress=False)
                written.append(out_path)
        return written

    def prune_daily_state(
        self,
        *,
        now_ms: int,
        cfg: PipelineConfig,
        active_stage1_addresses: Iterable[str],
        seed_addresses: Iterable[str],
        ever_whitelisted_addresses: Iterable[str],
        skipped_addresses: Iterable[str] = (),
    ) -> dict[str, int]:
        """Enforce §4 retention rules on ``state/daily/addresses/`` shards.

        - Retains only the most recent ``fillStatsRetentionDays`` (90) UTC days in ``fillStats``.
        - Increments ``absentStage1Streak`` for addresses absent from Stage 1 today (excluding
          transiently skipped addresses) and prunes non-whitelisted, non-seed candidates with
          ``absentStage1Streak > 90``.
        """
        self._require_write_owner("daily")
        keep_dates = recent_utc_dates(now_ms, cfg.stateRetention.fillStatsRetentionDays.value)
        max_absent = cfg.stateRetention.pruneAbsentNonWhitelistDays.value
        stage1_set = {a.lower() for a in active_stage1_addresses}
        seed_set = {a.lower() for a in seed_addresses}
        ever_wl_set = {a.lower() for a in ever_whitelisted_addresses}
        skipped_set = {a.lower() for a in skipped_addresses}

        shards_to_check: set[str] = set(self._shard_cache.keys())
        if self.addresses_dir.is_dir():
            for path in self.addresses_dir.glob("*.json.gz"):
                shards_to_check.add(path.name.removesuffix(".json.gz"))

        fill_stats_pruned = 0
        candidates_pruned = 0

        for shard in sorted(shards_to_check):
            shard_map = self.load_shard(shard)
            shard_modified = False
            for addr in list(shard_map.keys()):
                rec = shard_map[addr]
                old_days_len = len(rec.fillStats.days)
                old_ntl_len = len(rec.fillStats.notionalDays)
                filtered_days = {d: val for d, val in rec.fillStats.days.items() if d in keep_dates}
                filtered_ntl = {
                    d: val for d, val in rec.fillStats.notionalDays.items() if d in keep_dates
                }
                if len(filtered_days) != old_days_len or len(filtered_ntl) != old_ntl_len:
                    rec.fillStats = FillStatsState(days=filtered_days, notionalDays=filtered_ntl)
                    fill_stats_pruned += (old_days_len - len(filtered_days)) + (
                        old_ntl_len - len(filtered_ntl)
                    )
                    shard_modified = True

                if addr in skipped_set:
                    new_streak = rec.absentStage1Streak
                else:
                    new_streak = 0 if addr in stage1_set else rec.absentStage1Streak + 1
                if new_streak != rec.absentStage1Streak:
                    rec.absentStage1Streak = new_streak
                    shard_modified = True

                if (
                    addr not in ever_wl_set
                    and addr not in seed_set
                    and rec.absentStage1Streak > max_absent
                ):
                    del shard_map[addr]
                    candidates_pruned += 1
                    shard_modified = True

            if shard_modified:
                self._dirty_shards.add(shard)

        flushed = self.flush_dirty_shards()
        return {
            "fillStatsEntriesPruned": fill_stats_pruned,
            "candidatesPruned": candidates_pruned,
            "shardsFlushed": len(flushed),
        }

    def append_run_log(
        self,
        partition: Literal["daily", "fast"],
        entry: dict[str, Any],
        *,
        max_entries: int = 500,
    ) -> Path:
        """Append one JSONL execution record and cap ``runs.jsonl`` at ``max_entries`` (§4)."""
        self._require_write_owner(partition)
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        runs_path = (self.daily_dir if partition == "daily" else self.fast_dir) / "runs.jsonl"
        lines: list[str] = []
        if runs_path.is_file():
            lines = [ln for ln in runs_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        lines.append(json.dumps(entry, separators=(",", ":"), sort_keys=True))
        if len(lines) > max_entries:
            lines = lines[-max_entries:]
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        _atomic_write_bytes(runs_path, payload)
        return runs_path

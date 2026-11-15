# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract data models corresponding 1:1 with schemas/*.schema.json."""

from hyperpod.models.candidates import CandidateRow, CandidatesFile, RejectReason
from hyperpod.models.candles import (
    BackfillCandles,
    CandleInterval,
    CandlesFile,
    SingleIntervalCandles,
    validate_candles,
)
from hyperpod.models.criteria import (
    CriteriaChange,
    CriteriaCurrent,
    CriteriaFile,
    CriteriaHistoryItem,
    CriteriaMembership,
    CriteriaRule,
    CriteriaScope,
    CriteriaStats,
)
from hyperpod.models.fills import DirCode, FillRow, FillsFile, LiqFlag
from hyperpod.models.manifest import (
    CandleFiles,
    CoinTrack,
    FillFiles,
    Manifest,
    ManifestFiles,
    PipelineStatus,
    StorageInfo,
)
from hyperpod.models.snapshot import (
    MarkResolution,
    Snapshot,
    SnapshotAvSeries,
    SnapshotKeyframe,
    SnapshotSeries,
)
from hyperpod.models.whitelist import (
    EquitySource,
    Whitelist,
    WhitelistAddress,
    WhitelistMetrics,
)

__all__ = [
    "BackfillCandles",
    "CandidateRow",
    "CandidatesFile",
    "CandleFiles",
    "CandleInterval",
    "CandlesFile",
    "CoinTrack",
    "CriteriaChange",
    "CriteriaCurrent",
    "CriteriaFile",
    "CriteriaHistoryItem",
    "CriteriaMembership",
    "CriteriaRule",
    "CriteriaScope",
    "CriteriaStats",
    "DirCode",
    "EquitySource",
    "FillFiles",
    "FillRow",
    "FillsFile",
    "LiqFlag",
    "Manifest",
    "ManifestFiles",
    "MarkResolution",
    "PipelineStatus",
    "RejectReason",
    "SingleIntervalCandles",
    "Snapshot",
    "SnapshotAvSeries",
    "SnapshotKeyframe",
    "SnapshotSeries",
    "StorageInfo",
    "Whitelist",
    "WhitelistAddress",
    "WhitelistMetrics",
    "validate_candles",
]

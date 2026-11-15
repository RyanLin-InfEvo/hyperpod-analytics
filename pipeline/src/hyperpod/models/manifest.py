# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic contract models for schemas/manifest.schema.json."""

from __future__ import annotations

from pydantic import StrictBool, StrictStr, model_validator

from hyperpod.models.common import (
    ContractModel,
    DateStr,
    MonthStr,
    NonEmptyStr,
    NonNegInt,
    PosInt,
    SchemaVersion,
)


class CoinTrack(ContractModel):
    coin: NonEmptyStr
    pinned: StrictBool
    trackedFrom: NonNegInt
    trackedTo: NonNegInt | None

    @model_validator(mode="after")
    def _check_tracked_order(self) -> CoinTrack:
        if self.trackedTo is not None and self.trackedTo < self.trackedFrom:
            raise ValueError("trackedTo must be >= trackedFrom")
        return self


class FillFiles(ContractModel):
    days: list[DateStr]
    months: list[MonthStr]


class CandleFiles(ContractModel):
    months5m: list[MonthStr]
    hasBackfill: StrictBool
    hasOverview4h: StrictBool


class ManifestFiles(ContractModel):
    snapshotDays: list[DateStr]
    snapshotMonths: list[MonthStr]
    fills: dict[str, FillFiles]
    candles: dict[str, CandleFiles]


class StorageInfo(ContractModel):
    totalBytesOnDisk: NonNegInt
    softLimitBytes: PosInt


class PipelineStatus(ContractModel):
    fastLastOk: NonNegInt | None
    dailyLastOk: NonNegInt | None
    warnings: list[StrictStr]


class Manifest(ContractModel):
    schemaVersion: SchemaVersion
    generatedAt: NonNegInt
    latestSnapshotAt: NonNegInt
    launchedAt: NonNegInt
    whitelistVersion: DateStr
    criteriaVersion: PosInt
    coins: list[CoinTrack]
    files: ManifestFiles
    storage: StorageInfo
    pipeline: PipelineStatus

# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic contract models for schemas/snapshot.schema.json."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StrictBool, model_validator

from hyperpod.models.common import (
    ContractModel,
    JsonNumber,
    NonEmptyStr,
    NonNegInt,
    NonZeroNumber,
    PosNumber,
    SchemaVersion,
)

MarkResolution = Literal["5m", "10m", "1h", "4h", "mixed"]
TrackedInterval = tuple[NonNegInt, NonNegInt, NonNegInt | None]
KeyframePos = tuple[NonNegInt, NonNegInt, NonZeroNumber, PosNumber | None]
PosChange = tuple[NonNegInt, NonNegInt, NonNegInt, JsonNumber, PosNumber | None]
StaleEntry = tuple[NonNegInt, list[NonNegInt]]


class SnapshotKeyframe(ContractModel):
    t: NonNegInt
    pos: list[KeyframePos]


class SnapshotAvSeries(ContractModel):
    addrIds: list[NonNegInt]
    values: list[list[JsonNumber | None]]

    @model_validator(mode="after")
    def _check_av_rows(self) -> SnapshotAvSeries:
        if len(self.values) != len(self.addrIds):
            raise ValueError("series.av.values row count must equal len(series.av.addrIds)")
        return self


class SnapshotSeries(ContractModel):
    t: list[NonNegInt]
    mark: list[list[PosNumber | None]]
    av: SnapshotAvSeries

    @model_validator(mode="after")
    def _check_series_lengths(self) -> SnapshotSeries:
        n_pts = len(self.t)
        for row in self.mark:
            if len(row) != n_pts:
                raise ValueError("each series.mark[coinIdx] length must equal len(series.t)")
        for row in self.av.values:
            if len(row) != n_pts:
                raise ValueError("each series.av.values[addrIdx] length must equal len(series.t)")
        return self


class Snapshot(ContractModel):
    schemaVersion: SchemaVersion
    coins: Annotated[list[NonEmptyStr], Field(min_length=1)]
    reconstructed: StrictBool | None = None
    reconstructedBefore: NonNegInt | None = None
    markResolution: MarkResolution | None = None
    tracked: list[TrackedInterval]
    keyframe: SnapshotKeyframe
    posChanges: list[PosChange]
    series: SnapshotSeries
    stale: list[StaleEntry] | None = None

    @model_validator(mode="after")
    def _check_coin_dim(self) -> Snapshot:
        if len(self.series.mark) != len(self.coins):
            raise ValueError("len(series.mark) must equal len(coins)")
        for _, from_ts, to_ts in self.tracked:
            if to_ts is not None and to_ts < from_ts:
                raise ValueError("tracked toTs must be >= fromTs")
        n_coins = len(self.coins)
        for _, coin_idx, _, _ in self.keyframe.pos:
            if coin_idx >= n_coins:
                raise ValueError(f"keyframe coinIdx {coin_idx} out of bounds for {n_coins} coins")
        for _, _, coin_idx, szi, entry_px in self.posChanges:
            if coin_idx >= n_coins:
                raise ValueError(f"posChanges coinIdx {coin_idx} out of bounds for {n_coins} coins")
            if szi == 0.0 and entry_px is not None:
                raise ValueError("posChanges entryPx must be null when szi == 0")
        return self

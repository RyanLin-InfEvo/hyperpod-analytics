# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic contract models for schemas/whitelist.schema.json."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StrictStr, model_validator

from hyperpod.models.common import (
    ContractModel,
    DateStr,
    EvmAddress,
    JsonNumber,
    NonNegInt,
    NonNegNumber,
    SchemaVersion,
    UnitInterval,
)

EquitySource = Literal["perp", "perp+spot"]
MembershipInterval = tuple[NonNegInt, NonNegInt | None, NonNegInt]


class WhitelistMetrics(ContractModel):
    accountValue: NonNegNumber
    equitySource: EquitySource
    pnlAllTime: JsonNumber
    roiAllTime: JsonNumber
    pnlMonth: JsonNumber
    maxDrawdown: NonNegNumber
    profitFactor: NonNegNumber | None
    profitFactorRobust: NonNegNumber | None
    winRate: UnitInterval
    nTrades: NonNegInt
    historyDays: NonNegNumber
    fillsPerDay: NonNegNumber
    medianHoldMinutes: NonNegNumber
    makerRatio: UnitInterval
    turnoverMonth: NonNegNumber
    fundingShare: NonNegNumber
    topWinShare: UnitInterval | None = None
    hedgeRatio: UnitInterval | None = None


class WhitelistAddress(ContractModel):
    id: NonNegInt
    address: EvmAddress
    label: StrictStr | None
    master: EvmAddress | None
    subAccounts: list[EvmAddress]
    coverageStart: NonNegInt
    score: UnitInterval
    metrics: WhitelistMetrics
    membership: Annotated[list[MembershipInterval], Field(min_length=1)]

    @model_validator(mode="after")
    def _check_membership_order(self) -> WhitelistAddress:
        for enter_ts, exit_ts, _ in self.membership:
            if exit_ts is not None and exit_ts < enter_ts:
                raise ValueError("membership exitTs must be >= enterTs")
        return self


class Whitelist(ContractModel):
    schemaVersion: SchemaVersion
    version: DateStr
    addresses: list[WhitelistAddress]

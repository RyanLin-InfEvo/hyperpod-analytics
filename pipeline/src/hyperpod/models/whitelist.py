# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic contract models for schemas/whitelist.schema.json."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from hyperpod.models.common import (
    ContractModel,
    DateStr,
    EvmAddress,
    JsonNumber,
    NonNegInt,
    NonNegNumber,
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
    label: str | None
    master: EvmAddress | None
    subAccounts: list[EvmAddress]
    coverageStart: NonNegInt
    score: UnitInterval
    metrics: WhitelistMetrics
    membership: Annotated[list[MembershipInterval], Field(min_length=1)]


class Whitelist(ContractModel):
    schemaVersion: Literal[1]
    version: DateStr
    addresses: list[WhitelistAddress]

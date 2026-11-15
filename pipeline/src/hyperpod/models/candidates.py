# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic contract models for schemas/candidates.schema.json."""

from __future__ import annotations

from typing import Literal

from pydantic import StrictBool

from hyperpod.models.common import (
    ContractModel,
    NonNegInt,
    NonNegNumber,
    PosInt,
    UnitInterval,
)

RejectReason = Literal[
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
]


class CandidateRow(ContractModel):
    idx: NonNegInt
    passed: StrictBool
    rejectReasons: list[RejectReason]
    fillsPerDay: NonNegNumber | None
    makerRatio: UnitInterval | None
    medianHoldMinutes: NonNegNumber | None
    maxDrawdown: NonNegNumber | None
    profitFactorRobust: NonNegNumber | None
    turnoverMonth: NonNegNumber | None
    nTrades: NonNegInt | None = None
    historyDays: NonNegNumber | None = None
    fundingShare: NonNegNumber | None = None
    hedgeRatio: UnitInterval | None = None
    topWinShare: UnitInterval | None = None


class CandidatesFile(ContractModel):
    schemaVersion: Literal[1]
    generatedAt: NonNegInt
    criteriaVersion: PosInt
    candidates: list[CandidateRow]

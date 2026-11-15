# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic contract models for schemas/candidates.schema.json."""

from __future__ import annotations

from pydantic import StrictBool, model_validator

from hyperpod.models.common import (
    ContractModel,
    NonNegInt,
    NonNegNumber,
    PosInt,
    SchemaVersion,
    UnitInterval,
)
from hyperpod.models.common import (
    RejectReason as RejectReason,
)


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

    @model_validator(mode="after")
    def _check_passed_consistency(self) -> CandidateRow:
        if self.passed != (len(self.rejectReasons) == 0):
            raise ValueError("passed must be True iff rejectReasons is empty")
        return self


class CandidatesFile(ContractModel):
    schemaVersion: SchemaVersion
    generatedAt: NonNegInt
    criteriaVersion: PosInt
    candidates: list[CandidateRow]

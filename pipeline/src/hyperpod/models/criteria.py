# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic contract models for schemas/criteria.schema.json."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr

from hyperpod.models.common import (
    ContractModel,
    JsonNumber,
    NonEmptyStr,
    NonNegInt,
    PosInt,
    Sha256Ref,
)

RuleOp = Literal["<", "<=", ">", ">=", "==", "!=", "not_in"]
SubaccountMode = Literal["independent", "merge_after_filter", "merge_before_filter"]
RuleValue = StrictBool | StrictInt | JsonNumber | StrictStr


class CriteriaRule(ContractModel):
    key: NonEmptyStr
    label: NonEmptyStr
    op: RuleOp
    value: RuleValue
    unit: StrictStr
    description: NonEmptyStr


class CriteriaMembership(ContractModel):
    exitAfterFailedDays: NonNegInt
    subaccounts: SubaccountMode
    excludeVaults: StrictBool


class CriteriaScope(ContractModel):
    perpDex: Literal["main"]
    spot: StrictBool
    hip3: StrictBool


class CriteriaStats(ContractModel):
    candidates: NonNegInt
    passed: NonNegInt
    rejectedBy: dict[str, NonNegInt]


class CriteriaCurrent(ContractModel):
    version: PosInt
    effectiveFrom: NonNegInt
    hash: Sha256Ref
    stage1: list[CriteriaRule]
    gates: list[CriteriaRule]
    membership: CriteriaMembership
    scope: CriteriaScope
    stats: CriteriaStats


class CriteriaChange(ContractModel):
    key: NonEmptyStr
    from_: RuleValue | None = Field(alias="from")
    to: RuleValue | None


class CriteriaHistoryItem(ContractModel):
    version: PosInt
    effectiveFrom: NonNegInt
    effectiveTo: NonNegInt
    hash: Sha256Ref
    changes: list[CriteriaChange]


class CriteriaFile(ContractModel):
    schemaVersion: Literal[1]
    current: CriteriaCurrent
    history: list[CriteriaHistoryItem]

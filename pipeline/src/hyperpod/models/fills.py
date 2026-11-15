# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic contract models for schemas/fills.schema.json."""

from __future__ import annotations

from typing import Literal

from hyperpod.models.common import (
    ContractModel,
    NonEmptyStr,
    NonNegInt,
    NonNegNumber,
    PosNumber,
)

DirCode = Literal["OL", "CL", "OS", "CS"]
LiqFlag = Literal[0, 1]
FillRow = tuple[NonNegInt, NonNegInt, DirCode, PosNumber, PosNumber, NonNegNumber, LiqFlag]


class FillsFile(ContractModel):
    schemaVersion: Literal[1]
    coin: NonEmptyStr
    fills: list[FillRow]

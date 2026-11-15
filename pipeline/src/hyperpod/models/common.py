# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared strict Pydantic field types and validators matching draft 2020-12 JSON schemas."""

from __future__ import annotations

import math
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _validate_json_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected JSON number, got non-numeric value")
    fval = float(value)
    if not math.isfinite(fval):
        raise ValueError("JSON number must be finite")
    return fval


def _validate_non_zero_number(value: object) -> float:
    fval = _validate_json_number(value)
    if fval == 0.0:
        raise ValueError("value must be non-zero")
    return fval


class ContractModel(BaseModel):
    """Base contract model forbidding unknown fields and validating strictly."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


NonNegInt = Annotated[int, Field(strict=True, ge=0)]
PosInt = Annotated[int, Field(strict=True, ge=1)]

JsonNumber = Annotated[float, BeforeValidator(_validate_json_number)]
NonNegNumber = Annotated[float, BeforeValidator(_validate_json_number), Field(ge=0.0)]
PosNumber = Annotated[float, BeforeValidator(_validate_json_number), Field(gt=0.0)]
NonZeroNumber = Annotated[float, BeforeValidator(_validate_non_zero_number)]
UnitInterval = Annotated[float, BeforeValidator(_validate_json_number), Field(ge=0.0, le=1.0)]

NonEmptyStr = Annotated[str, Field(strict=True, min_length=1)]
DateStr = Annotated[str, Field(strict=True, pattern=r"^\d{4}-\d{2}-\d{2}$")]
MonthStr = Annotated[str, Field(strict=True, pattern=r"^\d{4}-\d{2}$")]
EvmAddress = Annotated[str, Field(strict=True, pattern=r"^0x[a-fA-F0-9]{40}$")]
Sha256Ref = Annotated[str, Field(strict=True, pattern=r"^sha256:[a-f0-9]+$")]

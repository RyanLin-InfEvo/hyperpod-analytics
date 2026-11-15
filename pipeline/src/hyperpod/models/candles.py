# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic contract models for schemas/candles.schema.json."""

from __future__ import annotations

from typing import Literal

from hyperpod.models.common import (
    ContractModel,
    NonEmptyStr,
    NonNegInt,
    NonNegNumber,
    PosNumber,
    SchemaVersion,
)

CandleInterval = Literal["5m", "15m", "1h", "4h"]
CandleRow = tuple[NonNegInt, PosNumber, PosNumber, PosNumber, PosNumber, NonNegNumber]


class SingleIntervalCandles(ContractModel):
    schemaVersion: SchemaVersion
    coin: NonEmptyStr
    interval: CandleInterval
    c: list[CandleRow]


class BackfillCandles(ContractModel):
    schemaVersion: SchemaVersion
    coin: NonEmptyStr
    interval: Literal["15m_1h"]
    c15m: list[CandleRow]
    c1h: list[CandleRow]


CandlesFile = SingleIntervalCandles | BackfillCandles


def validate_candles(payload: object) -> CandlesFile:
    """Validate either a single-interval or combined 15m_1h backfill candle payload."""
    if isinstance(payload, dict) and payload.get("interval") == "15m_1h":
        return BackfillCandles.model_validate(payload)
    return SingleIntervalCandles.model_validate(payload)

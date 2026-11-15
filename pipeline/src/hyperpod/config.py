# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strict Pydantic loader and metadata validator for config/pipeline.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

RuleOp = Literal["<", "<=", ">", ">=", "==", "!=", "not_in"]
SubaccountMode = Literal["independent", "merge_after_filter", "merge_before_filter"]


def default_config_path() -> Path:
    """Resolve config/pipeline.yaml relative to repository root."""
    return Path(__file__).resolve().parents[3] / "config" / "pipeline.yaml"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParamSpec[T](_StrictModel):
    """Single configurable parameter carrying mandatory human-readable metadata."""

    value: T
    label: str
    description: str

    @field_validator("label", "description")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class GateParamSpec[T](ParamSpec[T]):
    """Stage 1 or hard-gate parameter exported to criteria.json."""

    op: RuleOp
    unit: str

    @field_validator("unit")
    @classmethod
    def _non_blank_unit(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("unit must be a non-empty string")
        return v


class ScopeConfig(_StrictModel):
    perpDex: ParamSpec[Literal["main"]]
    spot: ParamSpec[Literal[False]]
    hip3: ParamSpec[Literal[False]]


class CoinsConfig(_StrictModel):
    pinned: ParamSpec[list[Annotated[str, Field(min_length=1)]]]
    dynamicTopN: ParamSpec[Annotated[int, Field(strict=True, ge=0)]]
    stickyDays: ParamSpec[Annotated[int, Field(strict=True, ge=0)]]
    maxTracked: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]

    @model_validator(mode="after")
    def _check_max_tracked(self) -> CoinsConfig:
        if self.maxTracked.value < len(self.pinned.value):
            raise ValueError("coins.maxTracked must be >= number of pinned coins")
        return self


class Stage1Config(_StrictModel):
    minAccountValue: GateParamSpec[Annotated[float, Field(ge=0.0)]]
    minPnlAllTime: GateParamSpec[float]
    minPnlMonthRatio: GateParamSpec[float]
    maxTurnoverMonth: GateParamSpec[Annotated[float, Field(gt=0.0)]]


class HftGateConfig(_StrictModel):
    fillsPerDay: GateParamSpec[Annotated[float, Field(gt=0.0)]]
    medianHoldMinutes: GateParamSpec[Annotated[float, Field(ge=0.0)]]
    minTruncatedSpanDays: GateParamSpec[Annotated[float, Field(ge=0.0)]]


class MmGateConfig(_StrictModel):
    makerRatio: GateParamSpec[Annotated[float, Field(ge=0.0, le=1.0)]]
    turnoverMonth: GateParamSpec[Annotated[float, Field(ge=0.0)]]


class FundingGateConfig(_StrictModel):
    maxShare: GateParamSpec[Annotated[float, Field(ge=0.0)]]


class HedgeGateConfig(_StrictModel):
    maxRatio: GateParamSpec[Annotated[float, Field(ge=0.0, le=1.0)]]


class PassiveGateConfig(_StrictModel):
    minRecentTrades90d: GateParamSpec[Annotated[int, Field(strict=True, ge=0)]]


class SampleGateConfig(_StrictModel):
    minTrades: GateParamSpec[Annotated[int, Field(strict=True, ge=0)]]
    minHistoryDays: GateParamSpec[Annotated[float, Field(ge=0.0)]]


class MddGateConfig(_StrictModel):
    maxDrawdown: GateParamSpec[Annotated[float, Field(ge=0.0)]]


class PfGateConfig(_StrictModel):
    minRobust: GateParamSpec[Annotated[float, Field(ge=0.0)]]


class LuckyGateConfig(_StrictModel):
    maxTopWinShare: GateParamSpec[Annotated[float, Field(ge=0.0, le=1.0)]]


class GatesConfig(_StrictModel):
    excludeRoles: GateParamSpec[list[Annotated[str, Field(min_length=1)]]]
    hft: HftGateConfig
    mm: MmGateConfig
    funding: FundingGateConfig
    hedge: HedgeGateConfig
    passive: PassiveGateConfig
    sample: SampleGateConfig
    mdd: MddGateConfig
    pf: PfGateConfig
    lucky: LuckyGateConfig


class ScoringWeightsConfig(_StrictModel):
    profitFactorRobust: ParamSpec[Annotated[float, Field(ge=0.0, le=1.0)]]
    roiAllTime: ParamSpec[Annotated[float, Field(ge=0.0, le=1.0)]]
    invMaxDrawdown: ParamSpec[Annotated[float, Field(ge=0.0, le=1.0)]]
    monthlyReturn: ParamSpec[Annotated[float, Field(ge=0.0, le=1.0)]]
    winRate: ParamSpec[Annotated[float, Field(ge=0.0, le=1.0)]]
    logAccountValue: ParamSpec[Annotated[float, Field(ge=0.0, le=1.0)]]

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> ScoringWeightsConfig:
        total = (
            self.profitFactorRobust.value
            + self.roiAllTime.value
            + self.invMaxDrawdown.value
            + self.monthlyReturn.value
            + self.winRate.value
            + self.logAccountValue.value
        )
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"scoring.weights must sum to 1.0, got {total}")
        return self


class ScoringConfig(_StrictModel):
    fillsWindowDays: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]
    passiveWindowDays: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]
    subAccountRefreshDays: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]
    weights: ScoringWeightsConfig


class MembershipConfig(_StrictModel):
    exitAfterFailedDays: ParamSpec[Annotated[int, Field(strict=True, ge=0)]]
    subaccounts: ParamSpec[SubaccountMode]
    excludeVaults: ParamSpec[StrictBool]
    safetyCapWarning: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]


class RateLimitConfig(_StrictModel):
    ciBudgetPerMin: ParamSpec[Annotated[int, Field(strict=True, ge=1, le=1200)]]
    localBudgetPerMin: ParamSpec[Annotated[int, Field(strict=True, ge=1, le=1200)]]
    maxConcurrency: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]
    maxRetries: ParamSpec[Annotated[int, Field(strict=True, ge=0)]]
    halveCooldownSeconds: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]
    fillOverlapMs: ParamSpec[Annotated[int, Field(strict=True, ge=0)]]


class SnapshotConfig(_StrictModel):
    intervalSeconds: ParamSpec[Annotated[int, Field(strict=True, ge=60)]]
    maxFailedAddressRatio: ParamSpec[Annotated[float, Field(gt=0.0, le=1.0)]]
    staleWarnStreak: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]
    reconstructDays: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]
    reconstruct1hDays: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]
    hotRetentionDays: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]
    softLimitBytes: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]


class StateRetentionConfig(_StrictModel):
    fillStatsRetentionDays: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]
    pruneAbsentNonWhitelistDays: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]
    runsLogMaxEntries: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]


class FrontendDefaultsConfig(_StrictModel):
    dustRatio: ParamSpec[Annotated[float, Field(ge=0.0, le=0.5)]]
    minNotionalUsd: ParamSpec[Annotated[float, Field(ge=0.0)]]
    frvpDefaultBins: ParamSpec[Annotated[int, Field(strict=True, ge=20, le=200)]]
    exposureLookbackToleranceMinutes: ParamSpec[Annotated[int, Field(strict=True, ge=1)]]


class PipelineConfig(_StrictModel):
    """Top-level configuration parsed from config/pipeline.yaml."""

    schemaVersion: Literal[1]
    scope: ScopeConfig
    coins: CoinsConfig
    stage1: Stage1Config
    gates: GatesConfig
    scoring: ScoringConfig
    membership: MembershipConfig
    ratelimit: RateLimitConfig
    snapshot: SnapshotConfig
    stateRetention: StateRetentionConfig
    frontendDefaults: FrontendDefaultsConfig


def load_config(path: Path | None = None) -> PipelineConfig:
    """Load and strictly validate config/pipeline.yaml."""
    cfg_path = path or default_config_path()
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected mapping at root of {cfg_path}")
    return PipelineConfig.model_validate(raw)

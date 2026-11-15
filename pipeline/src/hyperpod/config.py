# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strict Pydantic loader and metadata validator for config/pipeline.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Protocol

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

from hyperpod.models.common import (
    JsonNumber,
    NonEmptyStr,
    NonNegInt,
    NonNegNumber,
    PosInt,
    PosNumber,
    SchemaVersion,
    StrictFalse,
    UnitInterval,
    _validate_json_number,
)

RuleOp = Literal["<", "<=", ">", ">=", "==", "!=", "not_in"]
SubaccountMode = Literal["independent", "merge_after_filter", "merge_before_filter"]
PosUnitFraction = Annotated[float, BeforeValidator(_validate_json_number), Field(gt=0.0, le=1.0)]
DustFraction = Annotated[float, BeforeValidator(_validate_json_number), Field(ge=0.0, le=0.5)]


def default_config_path() -> Path:
    """Resolve config/pipeline.yaml relative to repository root."""
    return Path(__file__).resolve().parents[3] / "config" / "pipeline.yaml"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParamSpec[T](_StrictModel):
    """Single configurable parameter carrying mandatory human-readable metadata."""

    value: T
    label: StrictStr
    description: StrictStr

    @field_validator("label", "description")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class GateParamSpec[T](ParamSpec[T]):
    """Stage 1 or hard-gate parameter exported to criteria.json."""

    op: RuleOp
    unit: StrictStr

    @field_validator("unit")
    @classmethod
    def _non_blank_unit(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("unit must be a non-empty string")
        return v

    def to_rule(self, key: str) -> dict[str, object]:
        """Serialize this gate parameter as a criteria.json ruleItem dictionary."""
        return {
            "key": key,
            "label": self.label,
            "op": self.op,
            "value": self.value,
            "unit": self.unit,
            "description": self.description,
        }


class ScopeConfig(_StrictModel):
    perpDex: ParamSpec[Literal["main"]]
    spot: ParamSpec[StrictFalse]
    hip3: ParamSpec[StrictFalse]


class CoinsConfig(_StrictModel):
    pinned: ParamSpec[list[NonEmptyStr]]
    dynamicTopN: ParamSpec[NonNegInt]
    stickyDays: ParamSpec[NonNegInt]
    maxTracked: ParamSpec[PosInt]

    @model_validator(mode="after")
    def _check_max_tracked(self) -> CoinsConfig:
        if self.maxTracked.value < len(self.pinned.value):
            raise ValueError("coins.maxTracked must be >= number of pinned coins")
        return self


class Stage1Config(_StrictModel):
    minAccountValue: GateParamSpec[NonNegNumber]
    minPnlAllTime: GateParamSpec[JsonNumber]
    minPnlMonthRatio: GateParamSpec[JsonNumber]
    maxTurnoverMonth: GateParamSpec[PosNumber]


class HftGateConfig(_StrictModel):
    fillsPerDay: GateParamSpec[PosNumber]
    medianHoldMinutes: GateParamSpec[NonNegNumber]
    minTruncatedSpanDays: GateParamSpec[NonNegNumber]


class MmGateConfig(_StrictModel):
    makerRatio: GateParamSpec[UnitInterval]
    turnoverMonth: GateParamSpec[NonNegNumber]


class FundingGateConfig(_StrictModel):
    maxShare: GateParamSpec[NonNegNumber]


class HedgeGateConfig(_StrictModel):
    maxRatio: GateParamSpec[UnitInterval]


class PassiveGateConfig(_StrictModel):
    minRecentTrades90d: GateParamSpec[NonNegInt]


class SampleGateConfig(_StrictModel):
    minTrades: GateParamSpec[NonNegInt]
    minHistoryDays: GateParamSpec[NonNegNumber]


class MddGateConfig(_StrictModel):
    maxDrawdown: GateParamSpec[NonNegNumber]


class PfGateConfig(_StrictModel):
    minRobust: GateParamSpec[NonNegNumber]


class LuckyGateConfig(_StrictModel):
    maxTopWinShare: GateParamSpec[UnitInterval]


class GatesConfig(_StrictModel):
    excludeRoles: GateParamSpec[Annotated[list[NonEmptyStr], Field(min_length=1)]]
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
    profitFactorRobust: ParamSpec[UnitInterval]
    roiAllTime: ParamSpec[UnitInterval]
    invMaxDrawdown: ParamSpec[UnitInterval]
    monthlyReturn: ParamSpec[UnitInterval]
    winRate: ParamSpec[UnitInterval]
    logAccountValue: ParamSpec[UnitInterval]

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
    fillsWindowDays: ParamSpec[PosInt]
    passiveWindowDays: ParamSpec[PosInt]
    subAccountRefreshDays: ParamSpec[PosInt]
    weights: ScoringWeightsConfig


class MembershipConfig(_StrictModel):
    exitAfterFailedDays: ParamSpec[NonNegInt]
    subaccounts: ParamSpec[SubaccountMode]
    excludeVaults: ParamSpec[StrictBool]
    safetyCapWarning: ParamSpec[PosInt]


class RateLimitConfig(_StrictModel):
    ciBudgetPerMin: ParamSpec[Annotated[int, Field(strict=True, ge=1, le=1200)]]
    localBudgetPerMin: ParamSpec[Annotated[int, Field(strict=True, ge=1, le=1200)]]
    maxConcurrency: ParamSpec[PosInt]
    maxRetries: ParamSpec[NonNegInt]
    halveCooldownSeconds: ParamSpec[PosInt]
    fillOverlapMs: ParamSpec[NonNegInt]


class SnapshotConfig(_StrictModel):
    intervalSeconds: ParamSpec[Annotated[int, Field(strict=True, ge=60)]]
    maxFailedAddressRatio: ParamSpec[PosUnitFraction]
    staleWarnStreak: ParamSpec[PosInt]
    reconstructDays: ParamSpec[PosInt]
    reconstruct1hDays: ParamSpec[PosInt]
    hotRetentionDays: ParamSpec[PosInt]
    softLimitBytes: ParamSpec[PosInt]

    @model_validator(mode="after")
    def _check_reconstruction_windows(self) -> SnapshotConfig:
        if self.reconstruct1hDays.value > self.reconstructDays.value:
            raise ValueError("snapshot.reconstruct1hDays must be <= snapshot.reconstructDays")
        return self


class StateRetentionConfig(_StrictModel):
    fillStatsRetentionDays: ParamSpec[PosInt]
    pruneAbsentNonWhitelistDays: ParamSpec[PosInt]
    runsLogMaxEntries: ParamSpec[PosInt]


class FrontendDefaultsConfig(_StrictModel):
    dustRatio: ParamSpec[DustFraction]
    minNotionalUsd: ParamSpec[NonNegNumber]
    frvpDefaultBins: ParamSpec[Annotated[int, Field(strict=True, ge=20, le=200)]]
    exposureLookbackToleranceMinutes: ParamSpec[PosInt]


class _RuleExportable(Protocol):
    def to_rule(self, key: str) -> dict[str, object]: ...


class PipelineConfig(_StrictModel):
    """Top-level configuration parsed from config/pipeline.yaml."""

    schemaVersion: SchemaVersion
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

    def export_stage1_rules(self) -> list[dict[str, object]]:
        """Export all Stage 1 filtering rules in criteria.json ruleItem format."""
        specs: list[tuple[str, _RuleExportable]] = [
            ("minAccountValue", self.stage1.minAccountValue),
            ("minPnlAllTime", self.stage1.minPnlAllTime),
            ("minPnlMonthRatio", self.stage1.minPnlMonthRatio),
            ("maxTurnoverMonth", self.stage1.maxTurnoverMonth),
        ]
        return [spec.to_rule(key) for key, spec in specs]

    def export_gate_rules(self) -> list[dict[str, object]]:
        """Export all Stage 2 hard-gate rules in criteria.json ruleItem format."""
        specs: list[tuple[str, _RuleExportable]] = [
            ("excludeRoles", self.gates.excludeRoles),
            ("hft.fillsPerDay", self.gates.hft.fillsPerDay),
            ("hft.medianHoldMinutes", self.gates.hft.medianHoldMinutes),
            ("hft.minTruncatedSpanDays", self.gates.hft.minTruncatedSpanDays),
            ("mm.makerRatio", self.gates.mm.makerRatio),
            ("mm.turnoverMonth", self.gates.mm.turnoverMonth),
            ("funding.maxShare", self.gates.funding.maxShare),
            ("hedge.maxRatio", self.gates.hedge.maxRatio),
            ("passive.minRecentTrades90d", self.gates.passive.minRecentTrades90d),
            ("sample.minTrades", self.gates.sample.minTrades),
            ("sample.minHistoryDays", self.gates.sample.minHistoryDays),
            ("mdd.maxDrawdown", self.gates.mdd.maxDrawdown),
            ("pf.minRobust", self.gates.pf.minRobust),
            ("lucky.maxTopWinShare", self.gates.lucky.maxTopWinShare),
        ]
        return [spec.to_rule(key) for key, spec in specs]


def load_config(path: Path | None = None) -> PipelineConfig:
    """Load and strictly validate config/pipeline.yaml."""
    cfg_path = path or default_config_path()
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected mapping at root of {cfg_path}")
    return PipelineConfig.model_validate(raw)

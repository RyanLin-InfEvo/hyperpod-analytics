# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strict Pydantic models for Hyperliquid Info API and Leaderboard responses."""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import (
    BeforeValidator,
    Field,
    StrictBool,
    StrictStr,
    model_validator,
)

from hyperpod.models.candles import CandleRow
from hyperpod.models.common import (
    ContractModel,
    EvmAddress,
    JsonNumber,
    NonEmptyStr,
    NonNegInt,
    PosInt,
)


def _validate_decimal_str(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("expected non-empty decimal string without surrounding whitespace")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"invalid decimal string: {value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite decimal string: {value!r}")
    return value


def _validate_non_neg_decimal_str(value: object) -> str:
    text = _validate_decimal_str(value)
    if float(text) < 0.0:
        raise ValueError(f"expected non-negative decimal string, got {text!r}")
    return text


def _validate_pos_decimal_str(value: object) -> str:
    text = _validate_decimal_str(value)
    if float(text) <= 0.0:
        raise ValueError(f"expected positive decimal string, got {text!r}")
    return text


DecimalStr = Annotated[str, BeforeValidator(_validate_decimal_str)]
NonNegDecimalStr = Annotated[str, BeforeValidator(_validate_non_neg_decimal_str)]
PosDecimalStr = Annotated[str, BeforeValidator(_validate_pos_decimal_str)]

AccountRole = Literal["user", "vault", "subAccount", "agent", "missing"]
PortfolioWindowName = Literal[
    "day",
    "week",
    "month",
    "allTime",
    "perpDay",
    "perpWeek",
    "perpMonth",
    "perpAllTime",
]
REQUIRED_PORTFOLIO_WINDOWS: frozenset[str] = frozenset(
    {
        "day",
        "week",
        "month",
        "allTime",
        "perpDay",
        "perpWeek",
        "perpMonth",
        "perpAllTime",
    }
)
LeaderboardWindowName = Literal["day", "week", "month", "allTime"]
REQUIRED_LEADERBOARD_WINDOWS: frozenset[str] = frozenset({"day", "week", "month", "allTime"})


class UserRoleData(ContractModel):
    master: EvmAddress | None = None
    user: EvmAddress | None = None


class UserRoleResponse(ContractModel):
    role: AccountRole
    data: UserRoleData | None = None

    @model_validator(mode="after")
    def _check_role_data(self) -> UserRoleResponse:
        if self.role == "subAccount" and (self.data is None or self.data.master is None):
            raise ValueError("subAccount userRole response must include data.master")
        return self


class MarginSummary(ContractModel):
    accountValue: DecimalStr
    totalMarginUsed: DecimalStr
    totalNtlPos: DecimalStr
    totalRawUsd: DecimalStr


class CumFunding(ContractModel):
    allTime: DecimalStr
    sinceOpen: DecimalStr
    sinceChange: DecimalStr


class PositionLeverage(ContractModel):
    type: Literal["cross", "isolated"]
    value: PosInt
    rawUsd: DecimalStr | None = None


class AssetPositionDetail(ContractModel):
    coin: NonEmptyStr
    cumFunding: CumFunding
    entryPx: DecimalStr | None = None
    leverage: PositionLeverage
    liquidationPx: DecimalStr | None = None
    marginUsed: DecimalStr
    maxLeverage: PosInt
    positionValue: DecimalStr
    returnOnEquity: DecimalStr
    szi: DecimalStr
    unrealizedPnl: DecimalStr


class AssetPositionItem(ContractModel):
    type: Literal["oneWay"]
    position: AssetPositionDetail


class ClearinghouseStateResponse(ContractModel):
    assetPositions: list[AssetPositionItem]
    crossMaintenanceMarginUsed: DecimalStr
    crossMarginSummary: MarginSummary
    marginSummary: MarginSummary
    time: NonNegInt
    withdrawable: DecimalStr


class SpotBalance(ContractModel):
    coin: NonEmptyStr
    token: NonNegInt
    total: DecimalStr
    hold: DecimalStr
    entryNtl: DecimalStr
    ltv: DecimalStr | None = None
    supplied: DecimalStr | None = None
    borrowed: DecimalStr | None = None


class SpotClearinghouseStateResponse(ContractModel):
    balances: list[SpotBalance]
    portfolioMarginEnabled: StrictBool | None = None
    portfolioMarginRatio: DecimalStr | None = None
    tokenToAvailableAfterMaintenance: list[tuple[NonNegInt, DecimalStr]] | None = None
    tokenToPortfolioBorrowRatio: list[tuple[NonNegInt, DecimalStr]] | None = None
    tokenToPortfolioSupplyRatio: list[tuple[NonNegInt, DecimalStr]] | None = None
    evmEscrows: list[dict[str, object]] | None = None


class SubAccountItem(ContractModel):
    name: StrictStr
    subAccountUser: EvmAddress
    master: EvmAddress
    clearinghouseState: ClearinghouseStateResponse
    spotState: SpotClearinghouseStateResponse


HistoryPoint = tuple[NonNegInt, DecimalStr]


class PortfolioWindowMetrics(ContractModel):
    accountValueHistory: list[HistoryPoint]
    pnlHistory: list[HistoryPoint]
    vlm: NonNegDecimalStr

    @model_validator(mode="after")
    def _check_alignment(self) -> PortfolioWindowMetrics:
        if len(self.accountValueHistory) != len(self.pnlHistory):
            raise ValueError(
                "accountValueHistory and pnlHistory must have identical length "
                f"({len(self.accountValueHistory)} != {len(self.pnlHistory)})"
            )
        for idx, ((av_ts, _), (pnl_ts, _)) in enumerate(
            zip(self.accountValueHistory, self.pnlHistory, strict=True)
        ):
            if av_ts != pnl_ts:
                raise ValueError(
                    f"portfolio timestamp mismatch at index {idx}: {av_ts} != {pnl_ts}"
                )
        return self


class PortfolioResponse(ContractModel):
    windows: dict[StrictStr, PortfolioWindowMetrics]

    @model_validator(mode="before")
    @classmethod
    def _coerce_wire_pairs(cls, value: object) -> object:
        if isinstance(value, dict) and "windows" in value:
            return value
        if not isinstance(value, list):
            raise ValueError("expected list of [windowName, metrics] pairs for portfolio response")
        windows: dict[str, object] = {}
        for item in value:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError("portfolio entry must be a 2-element [windowName, metrics] pair")
            key, metrics = item
            if not isinstance(key, str) or not key:
                raise ValueError("portfolio window name must be a non-empty string")
            if key in windows:
                raise ValueError(f"duplicate portfolio window: {key!r}")
            windows[key] = metrics
        return {"windows": windows}

    @model_validator(mode="after")
    def _check_required_windows(self) -> PortfolioResponse:
        missing = REQUIRED_PORTFOLIO_WINDOWS - set(self.windows)
        if missing:
            raise ValueError(f"missing required portfolio windows: {sorted(missing)}")
        return self

    def window(self, name: PortfolioWindowName) -> PortfolioWindowMetrics:
        """Return metrics for a specific portfolio window."""
        return self.windows[name]


class FillLiquidation(ContractModel):
    liquidatedUser: EvmAddress
    markPx: DecimalStr
    method: StrictStr


class RawFill(ContractModel):
    coin: NonEmptyStr
    px: NonNegDecimalStr
    sz: PosDecimalStr
    side: Literal["A", "B"]
    time: NonNegInt
    startPosition: DecimalStr
    dir: NonEmptyStr
    closedPnl: DecimalStr
    hash: StrictStr
    oid: NonNegInt
    crossed: StrictBool
    fee: DecimalStr
    tid: NonNegInt
    feeToken: NonEmptyStr | None = "USDC"
    twapId: NonNegInt | None = None
    builderFee: DecimalStr | None = None
    cloid: StrictStr | None = None
    liquidation: FillLiquidation | None = None


class UserFundingDelta(ContractModel):
    type: Literal["funding"]
    coin: NonEmptyStr
    usdc: DecimalStr
    szi: DecimalStr
    fundingRate: DecimalStr
    nSamples: NonNegInt | None = None


class UserFundingEvent(ContractModel):
    time: NonNegInt
    hash: StrictStr
    delta: UserFundingDelta


class MarginTier(ContractModel):
    lowerBound: NonNegDecimalStr
    maxLeverage: PosInt


class MarginTableSpec(ContractModel):
    description: StrictStr
    marginTiers: list[MarginTier]


class PerpAssetMeta(ContractModel):
    name: NonEmptyStr
    szDecimals: NonNegInt
    maxLeverage: PosInt
    marginTableId: NonNegInt | None = None
    onlyIsolated: StrictBool | None = None
    isDelisted: StrictBool | None = None
    marginMode: StrictStr | None = None


class PerpMeta(ContractModel):
    universe: list[PerpAssetMeta]
    marginTables: list[tuple[NonNegInt, MarginTableSpec]] = Field(default_factory=list)
    collateralToken: NonNegInt | None = None


class PerpAssetCtx(ContractModel):
    funding: DecimalStr
    openInterest: DecimalStr
    prevDayPx: DecimalStr
    dayNtlVlm: DecimalStr
    premium: DecimalStr | None = None
    oraclePx: DecimalStr
    markPx: DecimalStr
    midPx: DecimalStr | None = None
    impactPxs: tuple[DecimalStr, DecimalStr] | None = None
    dayBaseVlm: DecimalStr | None = None


class MetaAndAssetCtxsResponse(ContractModel):
    meta: PerpMeta
    asset_ctxs: list[PerpAssetCtx]

    @model_validator(mode="before")
    @classmethod
    def _coerce_wire_pair(cls, value: object) -> object:
        if isinstance(value, dict) and "meta" in value and "asset_ctxs" in value:
            return value
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("metaAndAssetCtxs must be a 2-element [meta, assetCtxs] payload")
        return {"meta": value[0], "asset_ctxs": value[1]}

    @model_validator(mode="after")
    def _check_lengths(self) -> MetaAndAssetCtxsResponse:
        if len(self.meta.universe) != len(self.asset_ctxs):
            raise ValueError(
                "meta.universe length must match assetCtxs length "
                f"({len(self.meta.universe)} != {len(self.asset_ctxs)})"
            )
        return self


class RawCandle(ContractModel):
    t: NonNegInt
    T: NonNegInt
    s: NonEmptyStr
    i: NonEmptyStr
    o: PosDecimalStr
    c: PosDecimalStr
    h: PosDecimalStr
    l: PosDecimalStr  # noqa: E741
    v: NonNegDecimalStr
    n: NonNegInt

    @model_validator(mode="after")
    def _check_time_order(self) -> RawCandle:
        if self.t > self.T:
            raise ValueError(f"candle close timestamp T ({self.T}) < open timestamp t ({self.t})")
        return self

    def to_candle_row(self) -> CandleRow:
        """Convert wire candle object to `[t, o, h, l, c, v]` contract tuple."""
        return (
            self.t,
            float(self.o),
            float(self.h),
            float(self.l),
            float(self.c),
            float(self.v),
        )


class LeaderboardWindowPerf(ContractModel):
    pnl: DecimalStr
    roi: DecimalStr
    vlm: NonNegDecimalStr


class LeaderboardRow(ContractModel):
    ethAddress: EvmAddress
    accountValue: DecimalStr
    windowPerformances: list[tuple[LeaderboardWindowName, LeaderboardWindowPerf]]
    prize: JsonNumber
    displayName: StrictStr | None = None

    @model_validator(mode="after")
    def _check_windows(self) -> LeaderboardRow:
        names = [w for w, _ in self.windowPerformances]
        if len(names) != len(set(names)) or set(names) != REQUIRED_LEADERBOARD_WINDOWS:
            raise ValueError(
                "leaderboard row windowPerformances must contain exact unique windows "
                f"{sorted(REQUIRED_LEADERBOARD_WINDOWS)}, got {names}"
            )
        return self

    def window(self, name: LeaderboardWindowName) -> LeaderboardWindowPerf:
        """Return metrics for the requested leaderboard window."""
        for win_name, perf in self.windowPerformances:
            if win_name == name:
                return perf
        raise KeyError(name)  # pragma: no cover

    @property
    def account_value_float(self) -> float:
        return float(self.accountValue)

    @property
    def month_pnl(self) -> float:
        return float(self.window("month").pnl)

    @property
    def month_vlm(self) -> float:
        return float(self.window("month").vlm)

    @property
    def all_time_pnl(self) -> float:
        return float(self.window("allTime").pnl)

    @property
    def all_time_roi(self) -> float:
        return float(self.window("allTime").roi)


class LeaderboardResponse(ContractModel):
    leaderboardRows: Annotated[list[LeaderboardRow], Field(min_length=1)]

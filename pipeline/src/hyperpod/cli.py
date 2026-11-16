# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""HyperPod CLI entrypoint."""

from __future__ import annotations

import asyncio
import gzip
import json
import re
from collections import Counter
from pathlib import Path
from typing import Annotated, Any

import typer

from hyperpod.client.info_api import (
    FillsPageResult,
    FundingPageResult,
    InfoApiClient,
    InfoApiError,
    compute_account_equity,
    is_main_perp_coin,
    normalize_fills,
)
from hyperpod.client.ratelimit import WeightRateLimiter
from hyperpod.config import PipelineConfig, default_config_path, load_config
from hyperpod.models import (
    CandidatesFile,
    ClearinghouseStateResponse,
    CriteriaFile,
    FillsFile,
    Manifest,
    PortfolioResponse,
    RawFill,
    Snapshot,
    SpotClearinghouseStateResponse,
    SubAccountItem,
    UserFundingEvent,
    UserRoleResponse,
    Whitelist,
    validate_candles,
)

_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

app = typer.Typer(help="HyperPod Hyperliquid main-perp analytics pipeline CLI.")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_json(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_unique_matching_names(label: str, expected: list[str], actual: set[str]) -> None:
    if len(expected) != len(set(expected)):
        raise ValueError(f"Duplicate entries in manifest for {label}: {expected}")
    if set(expected) != actual:
        raise ValueError(
            f"Manifest mismatch for {label}: "
            f"expected {sorted(expected)}, found {sorted(actual)} on disk"
        )


def _verify_manifest_matches_disk(data_dir: Path, manifest: Manifest) -> None:
    snap_days = {p.stem for p in (data_dir / "snapshots" / "days").glob("*.json")}
    snap_months = {
        p.name.removesuffix(".json.gz")
        for p in (data_dir / "snapshots" / "months").glob("*.json.gz")
    }
    _assert_unique_matching_names("files.snapshotDays", manifest.files.snapshotDays, snap_days)
    _assert_unique_matching_names(
        "files.snapshotMonths", manifest.files.snapshotMonths, snap_months
    )

    fills_root = data_dir / "fills"
    disk_fill_coins = (
        {p.name for p in fills_root.iterdir() if p.is_dir()} if fills_root.exists() else set()
    )
    if set(manifest.files.fills.keys()) != disk_fill_coins:
        raise ValueError(
            f"Manifest fills coins mismatch: manifest={sorted(manifest.files.fills)}, "
            f"disk={sorted(disk_fill_coins)}"
        )
    for coin, fill_files in manifest.files.fills.items():
        coin_dir = fills_root / coin
        actual_days = {p.stem for p in (coin_dir / "days").glob("*.json")}
        actual_months = {
            p.name.removesuffix(".json.gz") for p in (coin_dir / "months").glob("*.json.gz")
        }
        _assert_unique_matching_names(f"files.fills.{coin}.days", fill_files.days, actual_days)
        _assert_unique_matching_names(
            f"files.fills.{coin}.months", fill_files.months, actual_months
        )

    candles_root = data_dir / "candles"
    disk_candle_coins = (
        {p.name for p in candles_root.iterdir() if p.is_dir()} if candles_root.exists() else set()
    )
    if set(manifest.files.candles.keys()) != disk_candle_coins:
        raise ValueError(
            f"Manifest candles coins mismatch: manifest={sorted(manifest.files.candles)}, "
            f"disk={sorted(disk_candle_coins)}"
        )
    for coin, candle_files in manifest.files.candles.items():
        coin_dir = candles_root / coin
        actual_5m = {p.name.removesuffix(".json.gz") for p in (coin_dir / "5m").glob("*.json.gz")}
        _assert_unique_matching_names(
            f"files.candles.{coin}.months5m", candle_files.months5m, actual_5m
        )
        has_backfill = (coin_dir / "backfill_15m_1h.json.gz").is_file()
        if candle_files.hasBackfill != has_backfill:
            raise ValueError(
                f"Manifest candles.{coin}.hasBackfill={candle_files.hasBackfill} "
                f"does not match disk ({has_backfill})"
            )
        has_overview = (coin_dir / "overview_4h.json.gz").is_file()
        if candle_files.hasOverview4h != has_overview:
            raise ValueError(
                f"Manifest candles.{coin}.hasOverview4h={candle_files.hasOverview4h} "
                f"does not match disk ({has_overview})"
            )

    actual_bytes = sum(p.stat().st_size for p in data_dir.rglob("*") if p.is_file())
    if manifest.storage.totalBytesOnDisk != actual_bytes:
        raise ValueError(
            f"Manifest storage.totalBytesOnDisk ({manifest.storage.totalBytesOnDisk}) "
            f"!= actual disk bytes ({actual_bytes})"
        )


@app.command("validate-contracts")
def validate_contracts(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to pipeline.yaml")
    ] = None,
    site_data_dir: Annotated[
        Path | None, typer.Option("--site-data", help="Path to site-data directory")
    ] = None,
) -> None:
    """Validate config/pipeline.yaml metadata and all site-data files against Pydantic contracts."""
    cfg = load_config(config_path or default_config_path())
    data_dir = site_data_dir or (_repo_root() / "fixtures" / "site-data")

    manifest = Manifest.model_validate(_load_json(data_dir / "manifest.json"))
    Whitelist.model_validate(_load_json(data_dir / "whitelist.json"))
    CriteriaFile.model_validate(_load_json(data_dir / "criteria.json"))
    CandidatesFile.model_validate(_load_json(data_dir / "candidates.json"))

    Snapshot.model_validate(_load_json(data_dir / "snapshots" / "latest.json"))
    for path in sorted((data_dir / "snapshots" / "days").glob("*.json")):
        Snapshot.model_validate(_load_json(path))
    for path in sorted((data_dir / "snapshots" / "months").glob("*.json.gz")):
        Snapshot.model_validate(_load_json(path))

    for path in sorted((data_dir / "fills").glob("*/days/*.json")):
        FillsFile.model_validate(_load_json(path))
    for path in sorted((data_dir / "fills").glob("*/months/*.json.gz")):
        FillsFile.model_validate(_load_json(path))

    for path in sorted((data_dir / "candles").glob("*/*/*.json.gz")):
        validate_candles(_load_json(path))
    for path in sorted((data_dir / "candles").glob("*/*.json.gz")):
        validate_candles(_load_json(path))

    _verify_manifest_matches_disk(data_dir, manifest)

    typer.echo(
        f"Validated config v{cfg.schemaVersion} and site-data manifest "
        f"({manifest.whitelistVersion}, {manifest.storage.totalBytesOnDisk} B)."
    )


def _not_implemented(cmd: str, phase: str) -> None:
    typer.echo(f"Command '{cmd}' is scheduled for implementation in {phase}.", err=True)
    raise typer.Exit(code=2)


def _summarize_debug_address(
    *,
    address: str,
    role_resp: UserRoleResponse,
    abstraction: str,
    sub_accounts: list[SubAccountItem],
    clearinghouse: ClearinghouseStateResponse,
    spot: SpotClearinghouseStateResponse | None,
    portfolio: PortfolioResponse,
    fills_res: FillsPageResult,
    funding_res: FundingPageResult,
    main_perp_universe: set[str] | None,
    rate_limit_stats: dict[str, object],
    extra_warnings: list[str],
) -> dict[str, Any]:
    equity, equity_source, equity_warnings = compute_account_equity(
        clearinghouse, abstraction, spot
    )
    normalized_fills, norm_warnings = normalize_fills(
        fills_res.fills,
        address,
        main_perp_universe=main_perp_universe,
    )
    dir_counts = Counter(f.dir_code for f in normalized_fills)
    self_liquidations = sum(1 for f in normalized_fills if f.liq == 1)
    counterparty_liquidations = sum(
        1
        for _, raw in fills_res.fills
        if is_main_perp_coin(raw.coin, main_perp_universe)
        and raw.liquidation is not None
        and raw.liquidation.liquidatedUser.lower() != address.lower()
    )

    open_positions = [
        {
            "coin": ap.position.coin,
            "szi": float(ap.position.szi),
            "entryPx": float(ap.position.entryPx) if ap.position.entryPx is not None else None,
            "positionValue": float(ap.position.positionValue),
            "unrealizedPnl": float(ap.position.unrealizedPnl),
        }
        for ap in clearinghouse.assetPositions
        if is_main_perp_coin(ap.position.coin, main_perp_universe)
        and abs(float(ap.position.szi)) > 0.0
    ]
    open_positions.sort(key=lambda item: str(item["coin"]))

    all_time = portfolio.window("allTime")
    perp_all_time = portfolio.window("perpAllTime")
    perp_month = portfolio.window("perpMonth")

    warnings = [
        *extra_warnings,
        *equity_warnings,
        *fills_res.warnings,
        *norm_warnings,
    ]
    return {
        "address": address,
        "role": role_resp.role,
        "master": role_resp.data.master if role_resp.data else None,
        "abstraction": abstraction,
        "equity": equity,
        "equitySource": equity_source,
        "subAccountCount": len(sub_accounts),
        "openPositionsCount": len(open_positions),
        "openPositions": open_positions,
        "portfolioSummary": {
            "allTimeAccountValue": (
                float(all_time.accountValueHistory[-1][1]) if all_time.accountValueHistory else None
            ),
            "allTimePnl": (float(all_time.pnlHistory[-1][1]) if all_time.pnlHistory else None),
            "perpAllTimeAccountValue": (
                float(perp_all_time.accountValueHistory[-1][1])
                if perp_all_time.accountValueHistory
                else None
            ),
            "perpAllTimePnl": (
                float(perp_all_time.pnlHistory[-1][1]) if perp_all_time.pnlHistory else None
            ),
            "perpMonthVlm": float(perp_month.vlm),
            "perpAllTimePoints": len(perp_all_time.accountValueHistory),
        },
        "fillsSummary": {
            "pagesFetched": fills_res.pages_fetched,
            "rawFetchedCount": fills_res.raw_fetched_count,
            "dedupedRawCount": len(fills_res.fills),
            "normalizedMainPerpCount": len(normalized_fills),
            "dirCounts": dict(sorted(dir_counts.items())),
            "selfLiquidations": self_liquidations,
            "counterpartyLiquidations": counterparty_liquidations,
            "hasMore": fills_res.has_more,
            "truncated10k": fills_res.truncated_10k,
        },
        "fundingSummary": {
            "pagesFetched": funding_res.pages_fetched,
            "rawEventCount": len(funding_res.events),
            "mainPerpEventCount": len(funding_res.main_perp_events),
            "cumFundingNet": funding_res.cum_funding_net,
            "cumFundingAbs": funding_res.cum_funding_abs,
            "lastFundingTime": funding_res.last_funding_time,
            "lastFundingCoins": funding_res.last_funding_coins,
        },
        "rateLimitStats": rate_limit_stats,
        "warnings": warnings,
    }


async def _debug_address_live(
    address: str,
    cfg: PipelineConfig,
    *,
    max_fill_pages: int,
    max_funding_pages: int,
) -> dict[str, Any]:
    limiter = WeightRateLimiter(
        budget_per_min=cfg.ratelimit.ciBudgetPerMin.value,
        max_retries=cfg.ratelimit.maxRetries.value,
        halve_cooldown_seconds=float(cfg.ratelimit.halveCooldownSeconds.value),
    )
    async with InfoApiClient(
        rate_limiter=limiter,
        max_concurrency=cfg.ratelimit.maxConcurrency.value,
    ) as client:
        meta = await client.fetch_meta()
        main_perp_universe = {asset.name for asset in meta.universe}

        role_resp = await client.fetch_user_role(address)
        abstraction = await client.fetch_user_abstraction(address)
        sub_accounts = await client.fetch_sub_accounts(address) if role_resp.role == "user" else []
        clearinghouse = await client.fetch_clearinghouse_state(address)
        has_short = any(float(ap.position.szi) < 0.0 for ap in clearinghouse.assetPositions)
        spot: SpotClearinghouseStateResponse | None = None
        if abstraction not in {"disabled", "default"} or has_short:
            spot = await client.fetch_spot_clearinghouse_state(address)

        portfolio = await client.fetch_portfolio(address)
        fills_res = await client.fetch_user_fills_by_time(
            address,
            overlap_ms=cfg.ratelimit.fillOverlapMs.value,
            max_pages=max_fill_pages,
        )
        funding_res = await client.fetch_user_funding(
            address,
            max_pages=max_funding_pages,
            main_perp_universe=main_perp_universe,
        )
        return _summarize_debug_address(
            address=address,
            role_resp=role_resp,
            abstraction=abstraction,
            sub_accounts=sub_accounts,
            clearinghouse=clearinghouse,
            spot=spot,
            portfolio=portfolio,
            fills_res=fills_res,
            funding_res=funding_res,
            main_perp_universe=main_perp_universe,
            rate_limit_stats=limiter.stats_dict(),
            extra_warnings=[],
        )


def _debug_address_from_fixtures(address: str, fixtures_dir: Path) -> dict[str, Any]:
    short = address[:10].lower()
    file_map: dict[str, str] = {}
    index_path = fixtures_dir / "index.json"
    if index_path.is_file():
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
        if isinstance(index_data, list):
            for entry in index_data:
                if (
                    isinstance(entry, dict)
                    and str(entry.get("address", "")).lower() == address.lower()
                ):
                    raw_files = entry.get("files")
                    if isinstance(raw_files, dict):
                        file_map = {str(k): str(v) for k, v in raw_files.items()}
                    break

    def load_endpoint(endpoint: str) -> Any:
        filename = file_map.get(endpoint, f"{endpoint}_{short}.json")
        path = fixtures_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"Missing fixture file for {endpoint}: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    role_resp = UserRoleResponse.model_validate(load_endpoint("userRole"))
    raw_abs = load_endpoint("userAbstraction")
    if not isinstance(raw_abs, str) or not raw_abs.strip():
        raise ValueError(f"Invalid userAbstraction fixture payload: {raw_abs!r}")
    abstraction = raw_abs

    raw_subs = load_endpoint("subAccounts")
    if raw_subs is None:
        sub_accounts = []
    elif isinstance(raw_subs, list):
        sub_accounts = [SubAccountItem.model_validate(item) for item in raw_subs]
    else:
        raise ValueError(f"Expected list or null for subAccounts, got {type(raw_subs).__name__}")
    clearinghouse = ClearinghouseStateResponse.model_validate(load_endpoint("clearinghouseState"))
    has_short = any(float(ap.position.szi) < 0.0 for ap in clearinghouse.assetPositions)
    spot: SpotClearinghouseStateResponse | None = None
    if abstraction not in {"disabled", "default"} or has_short:
        spot = SpotClearinghouseStateResponse.model_validate(
            load_endpoint("spotClearinghouseState")
        )

    portfolio = PortfolioResponse.model_validate(load_endpoint("portfolio"))
    raw_fills_list = [RawFill.model_validate(item) for item in load_endpoint("userFillsByTime")]
    fills_res = FillsPageResult(
        fills=list(enumerate(raw_fills_list)),
        pages_fetched=1,
        raw_fetched_count=len(raw_fills_list),
        last_cursor_ms=raw_fills_list[-1].time if raw_fills_list else None,
        has_more=False,
        truncated_10k=False,
        warnings=[],
    )
    raw_funding = [UserFundingEvent.model_validate(item) for item in load_endpoint("userFunding")]
    main_perp_funding = [ev for ev in raw_funding if is_main_perp_coin(ev.delta.coin)]
    max_t = max((ev.time for ev in raw_funding), default=None)
    last_coins = (
        sorted({ev.delta.coin for ev in raw_funding if ev.time == max_t})
        if max_t is not None
        else []
    )
    funding_res = FundingPageResult(
        events=raw_funding,
        main_perp_events=main_perp_funding,
        cum_funding_net=sum(float(ev.delta.usdc) for ev in main_perp_funding),
        cum_funding_abs=sum(abs(float(ev.delta.usdc)) for ev in main_perp_funding),
        last_funding_time=max_t,
        last_funding_coins=last_coins,
        pages_fetched=1,
    )
    return _summarize_debug_address(
        address=address,
        role_resp=role_resp,
        abstraction=abstraction,
        sub_accounts=sub_accounts,
        clearinghouse=clearinghouse,
        spot=spot,
        portfolio=portfolio,
        fills_res=fills_res,
        funding_res=funding_res,
        main_perp_universe=None,
        rate_limit_stats={"mode": "fixtures"},
        extra_warnings=[],
    )


@app.command("debug-address")
def debug_address(
    address: str,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to pipeline.yaml")
    ] = None,
    fixtures_dir: Annotated[
        Path | None,
        typer.Option("--fixtures-dir", help="Optional directory containing recorded API fixtures"),
    ] = None,
    max_fill_pages: Annotated[
        int, typer.Option("--max-fill-pages", min=1, help="Maximum fill pages to fetch")
    ] = 5,
    max_funding_pages: Annotated[
        int, typer.Option("--max-funding-pages", min=1, help="Maximum funding pages to fetch")
    ] = 5,
) -> None:
    """Inspect normalized Hyperliquid state for a single address."""
    if not _ADDRESS_RE.match(address):
        typer.echo(f"Invalid EVM address: {address!r}", err=True)
        raise typer.Exit(code=1)

    cfg = load_config(config_path or default_config_path())
    try:
        if fixtures_dir is not None:
            summary = _debug_address_from_fixtures(address, fixtures_dir)
        else:
            summary = asyncio.run(
                _debug_address_live(
                    address,
                    cfg,
                    max_fill_pages=max_fill_pages,
                    max_funding_pages=max_funding_pages,
                )
            )
    except (FileNotFoundError, InfoApiError, ValueError) as exc:
        typer.echo(f"Failed to inspect address {address}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(summary, indent=2))


@app.command("discover")
def discover() -> None:
    """Discover Stage 1 candidates from Hyperliquid leaderboard (Phase 3)."""
    _not_implemented("discover", "Phase 3")


@app.command("score")
def score() -> None:
    """Score Stage 2 candidates and update whitelist state (Phase 3)."""
    _not_implemented("score", "Phase 3")


@app.command("backfill")
def backfill(
    budget_minutes: Annotated[int | None, typer.Option("--budget-minutes")] = None,
) -> None:
    """Backfill candidate fills and scoring history with checkpointing (Phase 3)."""
    _ = budget_minutes
    _not_implemented("backfill", "Phase 3")


@app.command("snapshot")
def snapshot() -> None:
    """Capture 10-minute position snapshot and incremental whitelist fills (Phase 4)."""
    _not_implemented("snapshot", "Phase 4")


@app.command("candles")
def candles() -> None:
    """Archive 5m and 4h candles for tracked coins (Phase 4)."""
    _not_implemented("candles", "Phase 4")


@app.command("reconstruct")
def reconstruct() -> None:
    """Reconstruct pre-launch historical snapshots from fills and portfolio curves (Phase 4)."""
    _not_implemented("reconstruct", "Phase 4")


@app.command("bootstrap")
def bootstrap(
    budget_minutes: Annotated[int | None, typer.Option("--budget-minutes")] = None,
) -> None:
    """Run one-time full history backfill, candle archive, and reconstruction (Phase 4)."""
    _ = budget_minutes
    _not_implemented("bootstrap", "Phase 4")


@app.command("publish")
def publish() -> None:
    """Compact >7d daily archives to monthly gzip and publish gh-pages/state (Phase 4)."""
    _not_implemented("publish", "Phase 4")


@app.command("health")
def health(
    hours: Annotated[int | None, typer.Option("--hours")] = None,
    days: Annotated[int | None, typer.Option("--days")] = None,
) -> None:
    """Evaluate pipeline health from state runs.jsonl and snapshot intervals (Phase 4)."""
    _ = (hours, days)
    _not_implemented("health", "Phase 4")


@app.command("verify-live")
def verify_live(sample: Annotated[int, typer.Option("--sample")] = 3) -> None:
    """Verify published latest snapshot against live Hyperliquid API (Phase 4)."""
    _ = sample
    _not_implemented("verify-live", "Phase 4")


if __name__ == "__main__":
    app()

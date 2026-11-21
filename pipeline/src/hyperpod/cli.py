# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""HyperPod CLI entrypoint."""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from hyperpod.client.info_api import (
    DEFAULT_INFO_URL,
    FillsPageResult,
    FundingPageResult,
    InfoApiClient,
    InfoApiError,
    compute_account_equity,
    is_main_perp_coin,
    normalize_fills,
)
from hyperpod.client.leaderboard import DEFAULT_LEADERBOARD_URL, LeaderboardFetchError
from hyperpod.client.ratelimit import WeightRateLimiter
from hyperpod.config import PipelineConfig, default_config_path, load_config
from hyperpod.models import (
    ClearinghouseStateResponse,
    PortfolioResponse,
    RawFill,
    SpotClearinghouseStateResponse,
    SubAccountItem,
    UserFundingEvent,
    UserRoleResponse,
)
from hyperpod.ops.health import evaluate_health, verify_live_positions
from hyperpod.scoring.daily_runner import run_backfill, run_discover, run_score
from hyperpod.snapshot.candles import run_candles
from hyperpod.snapshot.collect import SnapshotAbortError, run_snapshot
from hyperpod.snapshot.reconstruct import run_reconstruct
from hyperpod.storage.git_publish import (
    BranchName,
    GitPublishError,
    PublishOwner,
    publish_orphan_branch,
)
from hyperpod.storage.site_writer import run_daily_publish, validate_site_data_directory
from hyperpod.storage.state_store import BootstrapDoneFile, StateStore
from hyperpod.util.time import now_utc_ms

_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

app = typer.Typer(help="HyperPod Hyperliquid main-perp analytics pipeline CLI.")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


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
    try:
        manifest = validate_site_data_directory(
            data_dir,
            schemas_dir=_repo_root() / "schemas",
            validate_jsonschema=False,
        )
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Contract validation failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"Validated config v{cfg.schemaVersion} and site-data manifest "
        f"({manifest.whitelistVersion}, {manifest.storage.totalBytesOnDisk} B)."
    )


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


def _default_state_dir() -> Path:
    return _repo_root() / "state"


def _default_seed_file() -> Path:
    return _repo_root() / "config" / "seed_addresses.txt"


def _default_blocklist_file() -> Path:
    return _repo_root() / "config" / "blocklist.txt"


@app.command("discover")
def discover(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to pipeline.yaml")
    ] = None,
    state_dir: Annotated[
        Path | None, typer.Option("--state-dir", help="Path to state directory")
    ] = None,
    seed_file: Annotated[
        Path | None, typer.Option("--seed-file", help="Path to seed_addresses.txt")
    ] = None,
    blocklist_file: Annotated[
        Path | None, typer.Option("--blocklist-file", help="Path to blocklist.txt")
    ] = None,
    leaderboard_url: Annotated[
        str, typer.Option("--leaderboard-url", help="Hyperliquid leaderboard URL")
    ] = DEFAULT_LEADERBOARD_URL,
) -> None:
    """Discover Stage 1 candidates from Hyperliquid leaderboard (Phase 3)."""
    cfg = load_config(config_path or default_config_path())
    store = StateStore(state_dir or _default_state_dir(), owner="daily")
    try:
        discovered = asyncio.run(
            run_discover(
                cfg,
                store,
                seed_file=seed_file or _default_seed_file(),
                blocklist_file=blocklist_file or _default_blocklist_file(),
                leaderboard_url=leaderboard_url,
            )
        )
    except (FileNotFoundError, LeaderboardFetchError, ValueError) as exc:
        typer.echo(f"discover failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    summary = {
        "generatedAt": discovered.generatedAt,
        "totalLeaderboardRows": discovered.totalLeaderboardRows,
        "passedLeaderboardCount": discovered.passedLeaderboardCount,
        "seedAddedCount": discovered.seedAddedCount,
        "activeWhitelistAddedCount": discovered.activeWhitelistAddedCount,
        "blocklistedCount": discovered.blocklistedCount,
        "totalCandidates": len(discovered.candidates),
        "usedLeaderboardFallback": discovered.usedLeaderboardFallback,
        "rejectedCounts": discovered.rejectedCounts,
        "warnings": discovered.warnings,
    }
    typer.echo(json.dumps(summary, indent=2))


@app.command("score")
def score(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to pipeline.yaml")
    ] = None,
    state_dir: Annotated[
        Path | None, typer.Option("--state-dir", help="Path to state directory")
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Optional public site data directory for outputs"),
    ] = None,
    seed_file: Annotated[
        Path | None, typer.Option("--seed-file", help="Path to seed_addresses.txt")
    ] = None,
    blocklist_file: Annotated[
        Path | None, typer.Option("--blocklist-file", help="Path to blocklist.txt")
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Optional cap on evaluated candidates")
    ] = None,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Score using persisted state/daily shards without network"),
    ] = False,
    refresh_discover: Annotated[
        bool,
        typer.Option("--refresh-discover", help="Re-run leaderboard discover before scoring"),
    ] = False,
    max_fill_pages: Annotated[
        int | None, typer.Option("--max-fill-pages", min=1, help="Optional fill page cap")
    ] = None,
    max_funding_pages: Annotated[
        int | None, typer.Option("--max-funding-pages", min=1, help="Optional funding page cap")
    ] = None,
    local_budget: Annotated[
        bool,
        typer.Option("--local-budget/--ci-budget", help="Select local vs CI rate-limit budget"),
    ] = False,
    leaderboard_url: Annotated[
        str, typer.Option("--leaderboard-url", help="Hyperliquid leaderboard URL")
    ] = DEFAULT_LEADERBOARD_URL,
    info_url: Annotated[
        str, typer.Option("--info-url", help="Hyperliquid Info API URL")
    ] = DEFAULT_INFO_URL,
) -> None:
    """Score Stage 2 candidates and update whitelist state (Phase 3)."""
    cfg = load_config(config_path or default_config_path())
    store = StateStore(state_dir or _default_state_dir(), owner="daily")
    try:
        res = asyncio.run(
            run_score(
                cfg,
                store,
                seed_file=seed_file or _default_seed_file(),
                blocklist_file=blocklist_file or _default_blocklist_file(),
                schemas_dir=_repo_root() / "schemas",
                data_dir=data_dir,
                limit=limit,
                offline=offline,
                refresh_discover=refresh_discover,
                local_budget=local_budget,
                max_fill_pages=max_fill_pages,
                max_funding_pages=max_funding_pages,
                leaderboard_url=leaderboard_url,
                info_url=info_url,
            )
        )
    except (FileNotFoundError, InfoApiError, LeaderboardFetchError, ValueError) as exc:
        typer.echo(f"score failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    wl_upd = res.whitelist_update
    summary = {
        "whitelistVersion": wl_upd.whitelist.version,
        "criteriaVersion": wl_upd.criteria.current.version,
        "evaluatedCount": res.evaluated_count,
        "passedCount": wl_upd.criteria.current.stats.passed,
        "activeWhitelistCount": wl_upd.active_count,
        "totalWhitelistRecords": len(wl_upd.whitelist.addresses),
        "newlyEntered": wl_upd.newly_entered,
        "newlyExited": wl_upd.newly_exited,
        "expandedSubaccounts": res.expanded_subaccounts,
        "rejectedBy": wl_upd.criteria.current.stats.rejectedBy,
        "trackedCoins": [c.coin for c in wl_upd.state.coins if c.trackedTo is None],
        "coinBackfillQueue": wl_upd.state.coinBackfillQueue,
        "errors": res.errors,
        "pruneStats": res.prune_stats,
        "writtenFiles": [str(p) for p in res.written_files],
        "warnings": res.warnings,
        "rateLimitStats": res.rate_limit_stats,
    }
    typer.echo(json.dumps(summary, indent=2))


@app.command("backfill")
def backfill(
    budget_minutes: Annotated[
        float | None,
        typer.Option("--budget-minutes", min=0.0, help="Stop cleanly after N minutes"),
    ] = None,
    checkpoint_every: Annotated[
        int,
        typer.Option("--checkpoint-every", min=1, help="Flush shards/cursor every N addresses"),
    ] = 20,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Optional cap on candidates this run")
    ] = None,
    restart: Annotated[
        bool, typer.Option("--restart", help="Ignore prior incomplete backfill_cursor.json")
    ] = False,
    refresh_discover: Annotated[
        bool, typer.Option("--refresh-discover", help="Re-run leaderboard discover on fresh start")
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to pipeline.yaml")
    ] = None,
    state_dir: Annotated[
        Path | None, typer.Option("--state-dir", help="Path to state directory")
    ] = None,
    seed_file: Annotated[
        Path | None, typer.Option("--seed-file", help="Path to seed_addresses.txt")
    ] = None,
    blocklist_file: Annotated[
        Path | None, typer.Option("--blocklist-file", help="Path to blocklist.txt")
    ] = None,
    max_fill_pages: Annotated[
        int | None, typer.Option("--max-fill-pages", min=1, help="Optional fill page cap")
    ] = None,
    max_funding_pages: Annotated[
        int | None, typer.Option("--max-funding-pages", min=1, help="Optional funding page cap")
    ] = None,
    local_budget: Annotated[
        bool,
        typer.Option("--local-budget/--ci-budget", help="Select local vs CI rate-limit budget"),
    ] = True,
    leaderboard_url: Annotated[
        str, typer.Option("--leaderboard-url", help="Hyperliquid leaderboard URL")
    ] = DEFAULT_LEADERBOARD_URL,
    info_url: Annotated[
        str, typer.Option("--info-url", help="Hyperliquid Info API URL")
    ] = DEFAULT_INFO_URL,
) -> None:
    """Backfill candidate fills and scoring history with checkpointing (Phase 3)."""
    cfg = load_config(config_path or default_config_path())
    store = StateStore(state_dir or _default_state_dir(), owner="daily")
    try:
        res = asyncio.run(
            run_backfill(
                cfg,
                store,
                seed_file=seed_file or _default_seed_file(),
                blocklist_file=blocklist_file or _default_blocklist_file(),
                budget_minutes=budget_minutes,
                checkpoint_every=checkpoint_every,
                limit=limit,
                restart=restart,
                refresh_discover=refresh_discover,
                local_budget=local_budget,
                max_fill_pages=max_fill_pages,
                max_funding_pages=max_funding_pages,
                leaderboard_url=leaderboard_url,
                info_url=info_url,
            )
        )
    except (FileNotFoundError, InfoApiError, LeaderboardFetchError, ValueError) as exc:
        typer.echo(f"backfill failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    summary = {
        "completed": res.completed,
        "processedThisRun": res.processed_this_run,
        "totalProcessed": res.total_processed,
        "totalCandidates": res.total_candidates,
        "nextIndex": res.next_index,
        "expandedSubaccounts": res.expanded_subaccounts,
        "errors": res.errors,
        "budgetExhausted": res.budget_exhausted,
        "warnings": res.warnings,
        "rateLimitStats": res.rate_limit_stats,
    }
    typer.echo(json.dumps(summary, indent=2))


def _default_data_dir() -> Path:
    return _repo_root() / "data"


@app.command("snapshot")
def snapshot(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to pipeline.yaml")
    ] = None,
    state_dir: Annotated[
        Path | None, typer.Option("--state-dir", help="Path to state directory")
    ] = None,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Path to public site data directory")
    ] = None,
    local_budget: Annotated[
        bool,
        typer.Option("--local-budget/--ci-budget", help="Select local vs CI rate-limit budget"),
    ] = False,
    info_url: Annotated[
        str, typer.Option("--info-url", help="Hyperliquid Info API URL")
    ] = DEFAULT_INFO_URL,
) -> None:
    """Capture 10-minute position snapshot and incremental whitelist fills (Phase 4)."""
    cfg = load_config(config_path or default_config_path())
    store = StateStore(state_dir or _default_state_dir(), owner="fast")
    try:
        res = asyncio.run(
            run_snapshot(
                cfg,
                store,
                data_dir=data_dir or _default_data_dir(),
                schemas_dir=_repo_root() / "schemas",
                local_budget=local_budget,
                info_url=info_url,
            )
        )
    except (
        FileNotFoundError,
        InfoApiError,
        PermissionError,
        SnapshotAbortError,
        ValueError,
    ) as exc:
        typer.echo(f"snapshot failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    summary = {
        "tNow": res.t_now,
        "dateStr": res.date_str,
        "activeCount": res.active_count,
        "failedCount": res.failed_count,
        "staleIds": res.stale_ids,
        "fillsPolledCount": res.fills_polled_count,
        "fillsRowsUpserted": res.fills_rows_upserted,
        "posChangesEmitted": res.pos_changes_emitted,
        "coins": res.coins,
        "warnings": res.warnings,
        "rateLimitStats": res.rate_limit_stats,
    }
    typer.echo(json.dumps(summary, indent=2))


@app.command("candles")
def candles(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to pipeline.yaml")
    ] = None,
    state_dir: Annotated[
        Path | None, typer.Option("--state-dir", help="Path to state directory")
    ] = None,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Path to public site data directory")
    ] = None,
    full_backfill: Annotated[
        bool,
        typer.Option("--full-backfill", help="Fetch full 5m/15m/1h/4h lookback depth"),
    ] = False,
    include_today_5m: Annotated[
        bool,
        typer.Option("--include-today-5m", help="Include today's partial 5m candles"),
    ] = False,
    local_budget: Annotated[
        bool,
        typer.Option("--local-budget/--ci-budget", help="Select local vs CI rate-limit budget"),
    ] = False,
    info_url: Annotated[
        str, typer.Option("--info-url", help="Hyperliquid Info API URL")
    ] = DEFAULT_INFO_URL,
) -> None:
    """Archive 5m, 4h, and queued 15m/1h backfill candles for tracked coins (Phase 4)."""
    cfg = load_config(config_path or default_config_path())
    store = StateStore(state_dir or _default_state_dir(), owner="daily")
    try:
        res = asyncio.run(
            run_candles(
                cfg,
                store,
                data_dir=data_dir or _default_data_dir(),
                schemas_dir=_repo_root() / "schemas",
                full_backfill=full_backfill,
                include_today_5m=include_today_5m,
                local_budget=local_budget,
                info_url=info_url,
            )
        )
    except (FileNotFoundError, InfoApiError, PermissionError, ValueError) as exc:
        typer.echo(f"candles failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    summary = {
        "coinsProcessed": res.coins_processed,
        "coinStats": res.coin_stats,
        "errors": res.errors,
        "warnings": res.warnings,
        "rateLimitStats": res.rate_limit_stats,
    }
    typer.echo(json.dumps(summary, indent=2))


@app.command("reconstruct")
def reconstruct(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to pipeline.yaml")
    ] = None,
    state_dir: Annotated[
        Path | None, typer.Option("--state-dir", help="Path to state directory")
    ] = None,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Path to public site data directory")
    ] = None,
    launched_at: Annotated[
        int | None,
        typer.Option("--launched-at", min=0, help="Launch timestamp in UTC epoch ms"),
    ] = None,
    write_fills: Annotated[
        bool,
        typer.Option("--write-fills/--no-write-fills", help="Write historical whitelist fills"),
    ] = True,
    local_budget: Annotated[
        bool,
        typer.Option("--local-budget/--ci-budget", help="Select local vs CI rate-limit budget"),
    ] = True,
    info_url: Annotated[
        str, typer.Option("--info-url", help="Hyperliquid Info API URL")
    ] = DEFAULT_INFO_URL,
) -> None:
    """Reconstruct pre-launch historical snapshots from fills and portfolio curves (Phase 4)."""
    cfg = load_config(config_path or default_config_path())
    store = StateStore(state_dir or _default_state_dir(), owner="daily")
    try:
        res = asyncio.run(
            run_reconstruct(
                cfg,
                store,
                data_dir=data_dir or _default_data_dir(),
                schemas_dir=_repo_root() / "schemas",
                launched_at_ms=launched_at,
                write_fills=write_fills,
                local_budget=local_budget,
                info_url=info_url,
            )
        )
    except (FileNotFoundError, InfoApiError, KeyError, PermissionError, ValueError) as exc:
        typer.echo(f"reconstruct failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    summary = {
        "launchedAt": res.launched_at,
        "reconstructStart": res.reconstruct_start,
        "cutoff1h": res.cutoff_1h,
        "daysReconstructed": res.days_reconstructed,
        "hotDaysWritten": res.hot_days_written,
        "monthsWritten": res.months_written,
        "activeAddresses": res.active_addresses,
        "sziMismatches": res.szi_mismatches,
        "fillsRowsUpserted": res.fills_rows_upserted,
        "warnings": res.warnings,
        "rateLimitStats": res.rate_limit_stats,
    }
    typer.echo(json.dumps(summary, indent=2))


@app.command("publish")
def publish(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to pipeline.yaml")
    ] = None,
    state_dir: Annotated[
        Path | None, typer.Option("--state-dir", help="Path to state directory")
    ] = None,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Path to public site data directory")
    ] = None,
    artifact_dir: Annotated[
        Path | None, typer.Option("--artifact-dir", help="Optional artifact dir from score job")
    ] = None,
    fetch_fills: Annotated[
        bool,
        typer.Option("--fetch-fills/--no-fetch-fills", help="Fetch missing/new-member fills"),
    ] = True,
    local_budget: Annotated[
        bool,
        typer.Option("--local-budget/--ci-budget", help="Select local vs CI rate-limit budget"),
    ] = False,
    info_url: Annotated[
        str, typer.Option("--info-url", help="Hyperliquid Info API URL")
    ] = DEFAULT_INFO_URL,
    remote_url: Annotated[
        str | None, typer.Option("--remote-url", help="Git remote URL or bare repo path")
    ] = None,
    push_state: Annotated[
        bool, typer.Option("--push-state", help="Push scoped state partition to state branch")
    ] = False,
    push_gh_pages: Annotated[
        bool, typer.Option("--push-gh-pages", help="Push scoped output to gh-pages branch")
    ] = False,
    push_only: Annotated[
        bool,
        typer.Option("--push-only", help="Only run git_publish without daily merge/compaction"),
    ] = False,
    branch: Annotated[
        str | None,
        typer.Option("--branch", help="Target branch when using --push-only (state|gh-pages)"),
    ] = None,
    owner: Annotated[
        str,
        typer.Option("--owner", help="Publish owner (fast|daily|frontend|bootstrap)"),
    ] = "daily",
    source_dir: Annotated[
        Path | None,
        typer.Option("--source-dir", help="Override source dir for --push-only"),
    ] = None,
    commit_message: Annotated[
        str | None,
        typer.Option("--commit-message", help="Commit message for orphan branch push"),
    ] = None,
) -> None:
    """Compact >7d daily archives to monthly gzip and publish gh-pages/state (Phase 4)."""
    if owner not in {"fast", "daily", "frontend", "bootstrap"}:
        typer.echo(f"Invalid --owner: {owner!r}", err=True)
        raise typer.Exit(code=2)
    typed_owner = cast(PublishOwner, owner)
    cfg = load_config(config_path or default_config_path())
    eff_state_dir = state_dir or _default_state_dir()
    eff_data_dir = data_dir or _default_data_dir()
    schemas_dir = _repo_root() / "schemas"

    if push_only:
        if remote_url is None:
            typer.echo("--remote-url is required with --push-only", err=True)
            raise typer.Exit(code=2)
        if branch not in {"state", "gh-pages"}:
            typer.echo("--branch must be 'state' or 'gh-pages' with --push-only", err=True)
            raise typer.Exit(code=2)
        typed_branch = cast(BranchName, branch)
        eff_source = source_dir or (eff_state_dir if typed_branch == "state" else eff_data_dir)
        msg = commit_message or f"chore(publish): update {typed_branch} ({typed_owner})"
        try:
            git_res = publish_orphan_branch(
                eff_source,
                remote_url=remote_url,
                branch=typed_branch,
                owner=typed_owner,
                commit_message=msg,
                cfg=cfg,
                schemas_dir=schemas_dir,
            )
        except (GitPublishError, ValueError, FileNotFoundError) as exc:
            typer.echo(f"publish --push-only failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(
            json.dumps(
                {
                    "branch": git_res.branch,
                    "owner": git_res.owner,
                    "commitSha": git_res.commit_sha,
                    "attempts": git_res.attempts,
                    "hadRemoteBranch": git_res.had_remote_branch,
                },
                indent=2,
            )
        )
        return

    if (push_state or push_gh_pages) and remote_url is None:
        typer.echo("--remote-url is required when --push-state or --push-gh-pages is set", err=True)
        raise typer.Exit(code=2)

    store = StateStore(eff_state_dir, owner="daily")
    try:
        res = asyncio.run(
            run_daily_publish(
                cfg,
                store,
                data_dir=eff_data_dir,
                schemas_dir=schemas_dir,
                artifact_dir=artifact_dir,
                fetch_missing_fills=fetch_fills,
                local_budget=local_budget,
                info_url=info_url,
            )
        )
        pushed: list[dict[str, object]] = []
        if push_state:
            assert remote_url is not None
            st_res = publish_orphan_branch(
                eff_state_dir,
                remote_url=remote_url,
                branch="state",
                owner=typed_owner,
                commit_message=commit_message or f"chore(state): {typed_owner} publish",
                cfg=cfg,
                schemas_dir=schemas_dir,
            )
            pushed.append(
                {
                    "branch": st_res.branch,
                    "commitSha": st_res.commit_sha,
                    "attempts": st_res.attempts,
                }
            )
        if push_gh_pages:
            assert remote_url is not None
            gp_res = publish_orphan_branch(
                eff_data_dir,
                remote_url=remote_url,
                branch="gh-pages",
                owner=typed_owner,
                commit_message=commit_message or f"chore(data): {typed_owner} publish",
                cfg=cfg,
                schemas_dir=schemas_dir,
            )
            pushed.append(
                {
                    "branch": gp_res.branch,
                    "commitSha": gp_res.commit_sha,
                    "attempts": gp_res.attempts,
                }
            )
    except (
        FileNotFoundError,
        GitPublishError,
        InfoApiError,
        PermissionError,
        ValueError,
    ) as exc:
        typer.echo(f"publish failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    summary = {
        "whitelistVersion": res.manifest.whitelistVersion,
        "criteriaVersion": res.manifest.criteriaVersion,
        "snapshotDaysCompacted": res.compaction.snapshot_days_compacted,
        "snapshotMonthsUpdated": res.compaction.snapshot_months_updated,
        "fillDaysCompacted": res.compaction.fill_days_compacted,
        "fillMonthsUpdated": res.compaction.fill_months_updated,
        "fillsRowsUpserted": res.fills_rows_upserted,
        "fillsAddressesPolled": res.fills_addresses_polled,
        "totalBytesOnDisk": res.manifest.storage.totalBytesOnDisk,
        "pushed": pushed,
        "warnings": res.warnings,
        "rateLimitStats": res.rate_limit_stats,
    }
    typer.echo(json.dumps(summary, indent=2))


@app.command("bootstrap")
def bootstrap(
    budget_minutes: Annotated[
        float | None,
        typer.Option("--budget-minutes", min=0.0, help="Stop backfill cleanly after N minutes"),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Optional candidate cap for backfill")
    ] = None,
    checkpoint_every: Annotated[
        int, typer.Option("--checkpoint-every", min=1, help="Checkpoint cadence during backfill")
    ] = 20,
    skip_backfill: Annotated[
        bool,
        typer.Option("--skip-backfill", help="Skip backfill stage and score existing shards"),
    ] = False,
    refresh_discover: Annotated[
        bool, typer.Option("--refresh-discover", help="Force fresh leaderboard discovery")
    ] = False,
    push_remote: Annotated[
        str | None,
        typer.Option("--push-remote", help="Optional git remote URL to push state and gh-pages"),
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to pipeline.yaml")
    ] = None,
    state_dir: Annotated[
        Path | None, typer.Option("--state-dir", help="Path to state directory")
    ] = None,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Path to public site data directory")
    ] = None,
    seed_file: Annotated[
        Path | None, typer.Option("--seed-file", help="Path to seed_addresses.txt")
    ] = None,
    blocklist_file: Annotated[
        Path | None, typer.Option("--blocklist-file", help="Path to blocklist.txt")
    ] = None,
    leaderboard_url: Annotated[
        str, typer.Option("--leaderboard-url", help="Hyperliquid leaderboard URL")
    ] = DEFAULT_LEADERBOARD_URL,
    info_url: Annotated[
        str, typer.Option("--info-url", help="Hyperliquid Info API URL")
    ] = DEFAULT_INFO_URL,
) -> None:
    """Run one-time full history backfill, candle archive, and reconstruction (Phase 4)."""
    cfg = load_config(config_path or default_config_path())
    eff_state_dir = state_dir or _default_state_dir()
    eff_data_dir = data_dir or _default_data_dir()
    eff_seed = seed_file or _default_seed_file()
    eff_blocklist = blocklist_file or _default_blocklist_file()
    schemas_dir = _repo_root() / "schemas"
    store = StateStore(eff_state_dir, owner="bootstrap")

    async def _run_all() -> dict[str, Any]:
        if not skip_backfill:
            bf_res = await run_backfill(
                cfg,
                store,
                seed_file=eff_seed,
                blocklist_file=eff_blocklist,
                budget_minutes=budget_minutes,
                checkpoint_every=checkpoint_every,
                limit=limit,
                refresh_discover=refresh_discover,
                local_budget=True,
                leaderboard_url=leaderboard_url,
                info_url=info_url,
            )
            if not bf_res.completed:
                return {
                    "completed": False,
                    "stage": "backfill",
                    "processedThisRun": bf_res.processed_this_run,
                    "totalProcessed": bf_res.total_processed,
                    "totalCandidates": bf_res.total_candidates,
                    "nextIndex": bf_res.next_index,
                    "budgetExhausted": bf_res.budget_exhausted,
                    "warnings": bf_res.warnings,
                }

        score_res = await run_score(
            cfg,
            store,
            seed_file=eff_seed,
            blocklist_file=eff_blocklist,
            schemas_dir=schemas_dir,
            data_dir=eff_data_dir,
            offline=True,
            local_budget=True,
            leaderboard_url=leaderboard_url,
            info_url=info_url,
        )
        candles_res = await run_candles(
            cfg,
            store,
            data_dir=eff_data_dir,
            schemas_dir=schemas_dir,
            full_backfill=True,
            include_today_5m=True,
            local_budget=True,
            info_url=info_url,
        )
        recon_res = await run_reconstruct(
            cfg,
            store,
            data_dir=eff_data_dir,
            schemas_dir=schemas_dir,
            launched_at_ms=None,
            write_fills=True,
            local_budget=True,
            info_url=info_url,
        )
        launched_at = recon_res.launched_at
        store.save_fast_fill_cursors(recon_res.fill_cursors_by_id)
        store.save_daily_publish_fill_cursors(recon_res.fill_cursors_by_id)

        snap_res = await run_snapshot(
            cfg,
            store,
            data_dir=eff_data_dir,
            schemas_dir=schemas_dir,
            now_ms=max(launched_at, now_utc_ms()),
            local_budget=True,
            info_url=info_url,
        )
        manifest = validate_site_data_directory(eff_data_dir, schemas_dir=schemas_dir)
        done_file = BootstrapDoneFile(
            completedAt=now_utc_ms(),
            launchedAt=launched_at,
            whitelistVersion=manifest.whitelistVersion,
            criteriaVersion=manifest.criteriaVersion,
            activeWhitelistCount=score_res.whitelist_update.active_count,
            trackedCoins=snap_res.coins,
            reconstructDays=cfg.snapshot.reconstructDays.value,
            reconstructStart=recon_res.reconstruct_start,
        )
        store.save_bootstrap_done(done_file)

        pushed: list[dict[str, object]] = []
        if push_remote is not None:
            st_res = publish_orphan_branch(
                eff_state_dir,
                remote_url=push_remote,
                branch="state",
                owner="bootstrap",
                commit_message="chore(state): initial bootstrap state",
                cfg=cfg,
                schemas_dir=schemas_dir,
            )
            gp_res = publish_orphan_branch(
                eff_data_dir,
                remote_url=push_remote,
                branch="gh-pages",
                owner="bootstrap",
                commit_message="chore(data): initial bootstrap site data",
                cfg=cfg,
                schemas_dir=schemas_dir,
            )
            pushed.extend(
                [
                    {"branch": st_res.branch, "commitSha": st_res.commit_sha},
                    {"branch": gp_res.branch, "commitSha": gp_res.commit_sha},
                ]
            )

        return {
            "completed": True,
            "stage": "done",
            "launchedAt": launched_at,
            "activeWhitelistCount": score_res.whitelist_update.active_count,
            "trackedCoins": snap_res.coins,
            "candlesCoins": candles_res.coins_processed,
            "daysReconstructed": recon_res.days_reconstructed,
            "monthsWritten": recon_res.months_written,
            "sziMismatches": recon_res.szi_mismatches,
            "totalBytesOnDisk": manifest.storage.totalBytesOnDisk,
            "pushed": pushed,
            "warnings": [*recon_res.warnings, *snap_res.warnings],
        }

    try:
        summary = asyncio.run(_run_all())
    except (
        FileNotFoundError,
        GitPublishError,
        InfoApiError,
        KeyError,
        LeaderboardFetchError,
        PermissionError,
        SnapshotAbortError,
        ValueError,
    ) as exc:
        typer.echo(f"bootstrap failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(summary, indent=2))


@app.command("health")
def health(
    hours: Annotated[float | None, typer.Option("--hours", min=0.1)] = None,
    days: Annotated[float | None, typer.Option("--days", min=0.1)] = None,
    max_gap_minutes: Annotated[float, typer.Option("--max-gap-minutes", min=1.0)] = 30.0,
    min_success_rate: Annotated[float, typer.Option("--min-success-rate", min=0.0, max=1.0)] = 0.95,
    max_avg_fast_weight: Annotated[float, typer.Option("--max-avg-fast-weight", min=1.0)] = 1500.0,
    state_dir: Annotated[
        Path | None, typer.Option("--state-dir", help="Path to state directory")
    ] = None,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Path to public site data directory")
    ] = None,
) -> None:
    """Evaluate pipeline health from state runs.jsonl and snapshot intervals (Phase 4)."""
    if hours is not None and days is not None:
        typer.echo("Specify either --hours or --days, not both.", err=True)
        raise typer.Exit(code=2)
    eff_hours = (days * 24.0) if days is not None else (hours if hours is not None else 24.0)
    report = evaluate_health(
        state_dir or _default_state_dir(),
        data_dir or _default_data_dir(),
        hours=eff_hours,
        max_gap_minutes=max_gap_minutes,
        min_success_rate=min_success_rate,
        max_avg_fast_weight=max_avg_fast_weight,
    )
    typer.echo(json.dumps(report.to_dict(), indent=2))
    if not report.healthy:
        raise typer.Exit(code=1)


@app.command("verify-live")
def verify_live(
    sample: Annotated[int, typer.Option("--sample", min=1)] = 3,
    seed: Annotated[int | None, typer.Option("--seed")] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to pipeline.yaml")
    ] = None,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Path to public site data directory")
    ] = None,
    info_url: Annotated[
        str, typer.Option("--info-url", help="Hyperliquid Info API URL")
    ] = DEFAULT_INFO_URL,
) -> None:
    """Verify published latest snapshot against live Hyperliquid API (Phase 4)."""
    cfg = load_config(config_path or default_config_path())
    try:
        report = asyncio.run(
            verify_live_positions(
                data_dir or _default_data_dir(),
                cfg,
                sample=sample,
                seed=seed,
                info_url=info_url,
            )
        )
    except (FileNotFoundError, InfoApiError, ValueError) as exc:
        typer.echo(f"verify-live failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(report.to_dict(), indent=2))
    if not report.passed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()

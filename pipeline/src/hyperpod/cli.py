# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""HyperPod CLI entrypoint."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from hyperpod.config import default_config_path, load_config
from hyperpod.models import (
    CandidatesFile,
    CriteriaFile,
    FillsFile,
    Manifest,
    Snapshot,
    Whitelist,
    validate_candles,
)

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


@app.command("debug-address")
def debug_address(address: str) -> None:
    """Inspect normalized Hyperliquid state for a single address (Phase 2)."""
    _not_implemented(f"debug-address {address}", "Phase 2")


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

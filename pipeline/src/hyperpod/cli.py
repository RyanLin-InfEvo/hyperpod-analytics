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

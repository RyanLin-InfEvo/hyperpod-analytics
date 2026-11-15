# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract parity tests between schemas/*.schema.json, Pydantic models, and fixtures/site-data."""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from hyperpod.cli import app
from hyperpod.models import (
    CandidatesFile,
    CriteriaFile,
    FillsFile,
    Manifest,
    Snapshot,
    Whitelist,
    validate_candles,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "schemas"
EXAMPLES_VALID_DIR = SCHEMAS_DIR / "examples" / "valid"
EXAMPLES_INVALID_DIR = SCHEMAS_DIR / "examples" / "invalid"
SITE_DATA_DIR = REPO_ROOT / "fixtures" / "site-data"

ValidatorFn = Callable[[object], Any]

EXAMPLE_CASES: list[tuple[str, str, ValidatorFn]] = [
    ("manifest.json", "manifest.schema.json", Manifest.model_validate),
    ("whitelist.json", "whitelist.schema.json", Whitelist.model_validate),
    ("snapshot.json", "snapshot.schema.json", Snapshot.model_validate),
    ("fills.json", "fills.schema.json", FillsFile.model_validate),
    ("candles.json", "candles.schema.json", validate_candles),
    ("candles_backfill.json", "candles.schema.json", validate_candles),
    ("criteria.json", "criteria.schema.json", CriteriaFile.model_validate),
    ("candidates.json", "candidates.schema.json", CandidatesFile.model_validate),
]


def _load_schema(name: str) -> dict[str, Any]:
    data = json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _load_json(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("example_name", "schema_name", "validator"), EXAMPLE_CASES)
def test_valid_schema_examples_pass_jsonschema_and_pydantic(
    example_name: str, schema_name: str, validator: ValidatorFn
) -> None:
    schema = _load_schema(schema_name)
    payload = _load_json(EXAMPLES_VALID_DIR / example_name)
    jsonschema.validate(instance=payload, schema=schema, cls=jsonschema.Draft202012Validator)
    validator(payload)


@pytest.mark.parametrize(("example_name", "schema_name", "validator"), EXAMPLE_CASES)
def test_invalid_schema_examples_fail_jsonschema_and_pydantic(
    example_name: str, schema_name: str, validator: ValidatorFn
) -> None:
    schema = _load_schema(schema_name)
    payload = _load_json(EXAMPLES_INVALID_DIR / example_name)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=schema, cls=jsonschema.Draft202012Validator)
    with pytest.raises(ValidationError):
        validator(payload)


def test_site_data_fixtures_validate_against_jsonschema_and_pydantic() -> None:
    schemas = {
        "manifest": _load_schema("manifest.schema.json"),
        "whitelist": _load_schema("whitelist.schema.json"),
        "snapshot": _load_schema("snapshot.schema.json"),
        "fills": _load_schema("fills.schema.json"),
        "candles": _load_schema("candles.schema.json"),
        "criteria": _load_schema("criteria.schema.json"),
        "candidates": _load_schema("candidates.schema.json"),
    }

    manifest_raw = _load_json(SITE_DATA_DIR / "manifest.json")
    jsonschema.validate(manifest_raw, schemas["manifest"], cls=jsonschema.Draft202012Validator)
    manifest = Manifest.model_validate(manifest_raw)

    whitelist_raw = _load_json(SITE_DATA_DIR / "whitelist.json")
    jsonschema.validate(whitelist_raw, schemas["whitelist"], cls=jsonschema.Draft202012Validator)
    whitelist = Whitelist.model_validate(whitelist_raw)

    criteria_raw = _load_json(SITE_DATA_DIR / "criteria.json")
    jsonschema.validate(criteria_raw, schemas["criteria"], cls=jsonschema.Draft202012Validator)
    criteria = CriteriaFile.model_validate(criteria_raw)

    candidates_raw = _load_json(SITE_DATA_DIR / "candidates.json")
    jsonschema.validate(candidates_raw, schemas["candidates"], cls=jsonschema.Draft202012Validator)
    CandidatesFile.model_validate(candidates_raw)

    snap_paths = [
        SITE_DATA_DIR / "snapshots" / "latest.json",
        *sorted((SITE_DATA_DIR / "snapshots" / "months").glob("*.json.gz")),
        *sorted((SITE_DATA_DIR / "snapshots" / "days").glob("*.json")),
    ]
    assert len(snap_paths) == 9
    for path in snap_paths:
        raw = _load_json(path)
        jsonschema.validate(raw, schemas["snapshot"], cls=jsonschema.Draft202012Validator)
        Snapshot.model_validate(raw)

    fill_paths = [
        *sorted((SITE_DATA_DIR / "fills").glob("*/months/*.json.gz")),
        *sorted((SITE_DATA_DIR / "fills").glob("*/days/*.json")),
    ]
    assert len(fill_paths) == 16
    for path in fill_paths:
        raw = _load_json(path)
        jsonschema.validate(raw, schemas["fills"], cls=jsonschema.Draft202012Validator)
        FillsFile.model_validate(raw)

    candle_paths = [
        *sorted((SITE_DATA_DIR / "candles").glob("*/*/*.json.gz")),
        *sorted((SITE_DATA_DIR / "candles").glob("*/*.json.gz")),
    ]
    assert len(candle_paths) == 8
    for path in candle_paths:
        raw = _load_json(path)
        jsonschema.validate(raw, schemas["candles"], cls=jsonschema.Draft202012Validator)
        validate_candles(raw)

    # Fixture coverage assertions (Phase 1.3 requirements)
    assert len(whitelist.addresses) == 5
    assert [a.id for a in whitelist.addresses] == [0, 1, 2, 3, 4]
    assert whitelist.addresses[1].master == whitelist.addresses[0].address
    assert whitelist.addresses[3].membership[-1][1] is not None  # exited member
    assert whitelist.addresses[4].metrics.profitFactor is None
    assert whitelist.addresses[4].metrics.profitFactorRobust is None
    assert criteria.current.version == 2
    assert len(criteria.history) == 1
    assert manifest.criteriaVersion == 2


def test_site_data_cross_file_invariants_and_snapshot_replay() -> None:
    manifest = Manifest.model_validate(_load_json(SITE_DATA_DIR / "manifest.json"))

    # 1. totalBytesOnDisk matches exact on-disk sum
    actual_bytes = sum(p.stat().st_size for p in SITE_DATA_DIR.rglob("*") if p.is_file())
    assert manifest.storage.totalBytesOnDisk == actual_bytes

    # 2. Day vs month mutual exclusion across snapshots & fills
    month_snap = Snapshot.model_validate(
        _load_json(SITE_DATA_DIR / "snapshots" / "months" / "2026-10.json.gz")
    )
    month_dates = {
        datetime.fromtimestamp(t / 1000, tz=UTC).strftime("%Y-%m-%d") for t in month_snap.series.t
    }
    day_dates = set(manifest.files.snapshotDays)
    assert month_dates.isdisjoint(day_dates)
    assert len(month_dates | day_dates) == 10

    for coin in ("BTC", "ETH"):
        m_fills = FillsFile.model_validate(
            _load_json(SITE_DATA_DIR / "fills" / coin / "months" / "2026-10.json.gz")
        )
        m_fill_dates = {
            datetime.fromtimestamp(row[0] / 1000, tz=UTC).strftime("%Y-%m-%d")
            for row in m_fills.fills
        }
        d_fill_dates = set(manifest.files.fills[coin].days)
        assert m_fill_dates.isdisjoint(d_fill_dates)

    # 3. Position replay continuity across month -> 7 daily snapshots -> latest.json
    ordered_files = [
        SITE_DATA_DIR / "snapshots" / "months" / "2026-10.json.gz",
        *[SITE_DATA_DIR / "snapshots" / "days" / f"{d}.json" for d in manifest.files.snapshotDays],
    ]

    def tracked_at(snap: Snapshot, addr_id: int, t_ms: int) -> bool:
        return any(
            aid == addr_id and f_ts <= t_ms and (t_ts is None or t_ms <= t_ts)
            for aid, f_ts, t_ts in snap.tracked
        )

    carried: dict[tuple[int, int], tuple[float, float | None]] | None = None
    for path in ordered_files:
        snap = Snapshot.model_validate(_load_json(path))
        kf_map = {(aid, cid): (szi, epx) for aid, cid, szi, epx in snap.keyframe.pos}
        if carried is not None:
            expected_kf = {
                (aid, cid): val
                for (aid, cid), val in carried.items()
                if val[0] != 0.0 and tracked_at(snap, aid, snap.keyframe.t)
            }
            assert kf_map == expected_kf, f"keyframe mismatch at {path.name}"

        state = dict(kf_map)
        for _, aid, cid, szi, epx in snap.posChanges:
            if szi == 0.0:
                state.pop((aid, cid), None)
            else:
                state[(aid, cid)] = (szi, epx)
        carried = state

    latest = Snapshot.model_validate(_load_json(SITE_DATA_DIR / "snapshots" / "latest.json"))
    assert carried is not None
    expected_latest = {
        (aid, cid): val
        for (aid, cid), val in carried.items()
        if val[0] != 0.0 and tracked_at(latest, aid, latest.keyframe.t)
    }
    actual_latest = {(aid, cid): (szi, epx) for aid, cid, szi, epx in latest.keyframe.pos}
    assert actual_latest == expected_latest


def test_cli_validate_contracts_and_phase_stubs() -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["validate-contracts"])
    assert res.exit_code == 0, res.output
    assert "Validated config v1" in res.output

    stub = runner.invoke(app, ["discover"])
    assert stub.exit_code == 2

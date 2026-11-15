# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract parity tests between schemas/*.schema.json, Pydantic models, and fixtures/site-data."""

from __future__ import annotations

import copy
import gzip
import json
import math
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from hyperpod.cli import app
from hyperpod.config import load_config
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


def test_criteria_rejects_unknown_rejected_by_keys_and_accepts_list_rule_values() -> None:
    schema = _load_schema("criteria.schema.json")
    valid_criteria = _load_json(EXAMPLES_VALID_DIR / "criteria.json")

    with_roles = copy.deepcopy(valid_criteria)
    with_roles["current"]["gates"].append(
        {
            "key": "excludeRoles",
            "label": "Excluded account roles",
            "op": "not_in",
            "value": ["vault", "agent", "missing"],
            "unit": "role",
            "description": "Hyperliquid userRole values rejected immediately",
        }
    )
    with_roles["history"][0]["changes"].append(
        {
            "key": "excludeRoles",
            "from": ["vault"],
            "to": ["vault", "agent", "missing"],
        }
    )
    jsonschema.validate(instance=with_roles, schema=schema, cls=jsonschema.Draft202012Validator)
    CriteriaFile.model_validate(with_roles)

    bad_rejected_by = copy.deepcopy(valid_criteria)
    bad_rejected_by["current"]["stats"]["rejectedBy"] = {"unknown_gate": 1}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            instance=bad_rejected_by, schema=schema, cls=jsonschema.Draft202012Validator
        )
    with pytest.raises(ValidationError):
        CriteriaFile.model_validate(bad_rejected_by)


def test_strict_literal_and_string_parity_across_models() -> None:
    models_and_examples: list[tuple[str, ValidatorFn]] = [
        ("manifest.json", Manifest.model_validate),
        ("whitelist.json", Whitelist.model_validate),
        ("snapshot.json", Snapshot.model_validate),
        ("fills.json", FillsFile.model_validate),
        ("criteria.json", CriteriaFile.model_validate),
        ("candidates.json", CandidatesFile.model_validate),
        ("candles.json", validate_candles),
        ("candles_backfill.json", validate_candles),
    ]
    for example_name, validator in models_and_examples:
        payload = _load_json(EXAMPLES_VALID_DIR / example_name)
        payload["schemaVersion"] = True
        with pytest.raises(ValidationError):
            validator(payload)

    fills_schema = _load_schema("fills.schema.json")
    bad_fills = _load_json(EXAMPLES_VALID_DIR / "fills.json")
    bad_fills["fills"][0][6] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            instance=bad_fills, schema=fills_schema, cls=jsonschema.Draft202012Validator
        )
    with pytest.raises(ValidationError):
        FillsFile.model_validate(bad_fills)

    bad_whitelist = _load_json(EXAMPLES_VALID_DIR / "whitelist.json")
    bad_whitelist["addresses"][0]["label"] = 123
    with pytest.raises(ValidationError):
        Whitelist.model_validate(bad_whitelist)

    bad_manifest = _load_json(EXAMPLES_VALID_DIR / "manifest.json")
    bad_manifest["pipeline"]["warnings"] = [123]
    with pytest.raises(ValidationError):
        Manifest.model_validate(bad_manifest)


def test_semantic_and_interval_order_constraints() -> None:
    snap_schema = _load_schema("snapshot.schema.json")
    valid_snap = _load_json(EXAMPLES_VALID_DIR / "snapshot.json")

    bad_close_epx = copy.deepcopy(valid_snap)
    bad_close_epx["posChanges"] = [[1761962400000, 0, 0, 0, 96000.0]]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            instance=bad_close_epx, schema=snap_schema, cls=jsonschema.Draft202012Validator
        )
    with pytest.raises(ValidationError):
        Snapshot.model_validate(bad_close_epx)

    cand_schema = _load_schema("candidates.schema.json")
    valid_cands = _load_json(EXAMPLES_VALID_DIR / "candidates.json")

    passed_with_reason = copy.deepcopy(valid_cands)
    passed_with_reason["candidates"][0]["rejectReasons"] = ["hft"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            instance=passed_with_reason, schema=cand_schema, cls=jsonschema.Draft202012Validator
        )
    with pytest.raises(ValidationError):
        CandidatesFile.model_validate(passed_with_reason)

    failed_without_reason = copy.deepcopy(valid_cands)
    failed_without_reason["candidates"][1]["rejectReasons"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            instance=failed_without_reason, schema=cand_schema, cls=jsonschema.Draft202012Validator
        )
    with pytest.raises(ValidationError):
        CandidatesFile.model_validate(failed_without_reason)

    bad_tracked_order = copy.deepcopy(valid_snap)
    bad_tracked_order["tracked"] = [[0, 1761962400000, 1761955200000]]
    with pytest.raises(ValidationError):
        Snapshot.model_validate(bad_tracked_order)

    bad_membership_order = _load_json(EXAMPLES_VALID_DIR / "whitelist.json")
    bad_membership_order["addresses"][0]["membership"] = [[1761955200000, 1761523200000, 0]]
    with pytest.raises(ValidationError):
        Whitelist.model_validate(bad_membership_order)

    bad_cointrack_order = _load_json(EXAMPLES_VALID_DIR / "manifest.json")
    bad_cointrack_order["coins"][0]["trackedTo"] = (
        bad_cointrack_order["coins"][0]["trackedFrom"] - 1
    )
    with pytest.raises(ValidationError):
        Manifest.model_validate(bad_cointrack_order)

    bad_criteria_hist = _load_json(EXAMPLES_VALID_DIR / "criteria.json")
    bad_criteria_hist["history"][0]["effectiveTo"] = (
        bad_criteria_hist["history"][0]["effectiveFrom"] - 1
    )
    with pytest.raises(ValidationError):
        CriteriaFile.model_validate(bad_criteria_hist)

    # Custom validator branches on Snapshot & common numbers
    bad_av_rows = copy.deepcopy(valid_snap)
    bad_av_rows["series"]["av"]["values"].pop()
    with pytest.raises(ValidationError):
        Snapshot.model_validate(bad_av_rows)

    bad_mark_len = copy.deepcopy(valid_snap)
    bad_mark_len["series"]["mark"][0].pop()
    with pytest.raises(ValidationError):
        Snapshot.model_validate(bad_mark_len)

    bad_av_len = copy.deepcopy(valid_snap)
    bad_av_len["series"]["av"]["values"][0].pop()
    with pytest.raises(ValidationError):
        Snapshot.model_validate(bad_av_len)

    bad_coin_dim = copy.deepcopy(valid_snap)
    bad_coin_dim["series"]["mark"].append([100.0, 100.0])
    with pytest.raises(ValidationError):
        Snapshot.model_validate(bad_coin_dim)

    bad_kf_coin = copy.deepcopy(valid_snap)
    bad_kf_coin["keyframe"]["pos"][0][1] = 99
    with pytest.raises(ValidationError):
        Snapshot.model_validate(bad_kf_coin)

    bad_pc_coin = copy.deepcopy(valid_snap)
    bad_pc_coin["posChanges"][0][2] = 99
    with pytest.raises(ValidationError):
        Snapshot.model_validate(bad_pc_coin)

    bad_nan_mark = copy.deepcopy(valid_snap)
    bad_nan_mark["series"]["mark"][0][0] = float("nan")
    with pytest.raises(ValidationError):
        Snapshot.model_validate(bad_nan_mark)

    bad_inf_av = copy.deepcopy(valid_snap)
    bad_inf_av["series"]["av"]["values"][0][0] = float("inf")
    with pytest.raises(ValidationError):
        Snapshot.model_validate(bad_inf_av)

    bad_bool_av = copy.deepcopy(valid_snap)
    bad_bool_av["series"]["av"]["values"][0][0] = True
    with pytest.raises(ValidationError):
        Snapshot.model_validate(bad_bool_av)


def test_site_data_criteria_candidates_and_whitelist_score_consistency() -> None:
    cfg = load_config()
    criteria = _load_json(SITE_DATA_DIR / "criteria.json")
    candidates_payload = _load_json(SITE_DATA_DIR / "candidates.json")
    whitelist = _load_json(SITE_DATA_DIR / "whitelist.json")

    assert [r["key"] for r in criteria["current"]["stage1"]] == [
        r["key"] for r in cfg.export_stage1_rules()
    ]
    assert criteria["current"]["stage1"] == cfg.export_stage1_rules()
    assert [r["key"] for r in criteria["current"]["gates"]] == [
        r["key"] for r in cfg.export_gate_rules()
    ]
    assert criteria["current"]["gates"] == cfg.export_gate_rules()

    rows = candidates_payload["candidates"]
    expected_passed = sum(1 for row in rows if row["passed"])
    expected_counts: Counter[str] = Counter()
    for row in rows:
        for reason in row["rejectReasons"]:
            expected_counts[reason] += 1

    stats = criteria["current"]["stats"]
    assert stats["candidates"] == len(rows)
    assert stats["passed"] == expected_passed
    assert stats["rejectedBy"] == dict(expected_counts)

    # Active members (last membership entry has exitTs is None)
    active = [a for a in whitelist["addresses"] if a["membership"][-1][1] is None]
    assert [a["id"] for a in active] == [0, 1, 2, 4]

    n_active = len(active)
    denom = max(n_active - 1, 1)

    def pct_map(
        values_by_id: dict[int, float | None], *, none_is_top: bool = False
    ) -> dict[int, float]:
        present = [(val, aid) for aid, val in values_by_id.items() if val is not None]
        present.sort(key=lambda pair: (pair[0], pair[1]))
        out: dict[int, float] = {}
        for rank, (_, aid) in enumerate(present):
            out[aid] = rank / denom
        if none_is_top:
            for aid, val in values_by_id.items():
                if val is None:
                    out[aid] = 1.0
        return out

    pf_pct = pct_map(
        {a["id"]: a["metrics"]["profitFactorRobust"] for a in active}, none_is_top=True
    )
    roi_pct = pct_map({a["id"]: a["metrics"]["roiAllTime"] for a in active})
    mdd_pct = pct_map({a["id"]: 1.0 - a["metrics"]["maxDrawdown"] for a in active})
    mret_pct = pct_map(
        {a["id"]: a["metrics"]["pnlMonth"] / a["metrics"]["accountValue"] for a in active}
    )
    wr_pct = pct_map({a["id"]: a["metrics"]["winRate"] for a in active})
    av_pct = pct_map({a["id"]: math.log(max(a["metrics"]["accountValue"], 1.0)) for a in active})

    w = cfg.scoring.weights
    for addr in active:
        aid = addr["id"]
        expected_score = (
            w.profitFactorRobust.value * pf_pct[aid]
            + w.roiAllTime.value * roi_pct[aid]
            + w.invMaxDrawdown.value * mdd_pct[aid]
            + w.monthlyReturn.value * mret_pct[aid]
            + w.winRate.value * wr_pct[aid]
            + w.logAccountValue.value * av_pct[aid]
        )
        assert addr["score"] == pytest.approx(expected_score, abs=1e-4)


def test_cli_validate_contracts_rejects_manifest_disk_mismatch(tmp_path: Path) -> None:
    import shutil

    copied = tmp_path / "site-data"
    shutil.copytree(SITE_DATA_DIR, copied)
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["storage"]["totalBytesOnDisk"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    runner = CliRunner()
    res = runner.invoke(app, ["validate-contracts", "--site-data", str(copied)])
    assert res.exit_code != 0

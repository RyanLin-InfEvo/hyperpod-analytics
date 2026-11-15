# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit and property tests for config/pipeline.yaml loading and metadata validation."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from hyperpod.config import PipelineConfig, default_config_path, load_config
from hyperpod.models import CriteriaFile


def _raw_config() -> dict[str, Any]:
    raw = yaml.safe_load(default_config_path().read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _collect_param_paths(node: Any, prefix: str = "") -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        if "value" in node:
            found.append((prefix, node))
        else:
            for k, v in node.items():
                child = f"{prefix}.{k}" if prefix else str(k)
                found.extend(_collect_param_paths(v, child))
    return found


def test_load_default_pipeline_config_values() -> None:
    cfg = load_config()
    assert cfg.schemaVersion == 1
    assert cfg.scope.perpDex.value == "main"
    assert cfg.scope.spot.value is False
    assert cfg.scope.hip3.value is False
    assert cfg.coins.pinned.value == ["BTC", "ETH", "SOL", "HYPE"]
    assert cfg.coins.dynamicTopN.value == 20
    assert cfg.coins.stickyDays.value == 30
    assert cfg.coins.maxTracked.value == 32
    assert cfg.stage1.minAccountValue.value == 1_000_000.0
    assert cfg.gates.hft.fillsPerDay.value == 500.0
    assert cfg.gates.mdd.maxDrawdown.value == 0.45
    assert cfg.gates.pf.minRobust.value == 1.5
    assert cfg.membership.exitAfterFailedDays.value == 3
    assert cfg.membership.subaccounts.value == "merge_after_filter"
    assert cfg.ratelimit.ciBudgetPerMin.value == 800
    assert cfg.ratelimit.localBudgetPerMin.value == 1000
    assert cfg.snapshot.hotRetentionDays.value == 7
    assert cfg.snapshot.softLimitBytes.value == 800_000_000


def test_every_parameter_in_pipeline_yaml_has_non_empty_label_and_description() -> None:
    raw = _raw_config()
    params = _collect_param_paths(raw)
    assert len(params) >= 40
    for key_path, param in params:
        label = param.get("label")
        desc = param.get("description")
        assert isinstance(label, str) and label.strip(), f"missing/blank label at {key_path}"
        assert isinstance(desc, str) and desc.strip(), f"missing/blank description at {key_path}"


def test_missing_or_blank_label_or_description_rejected() -> None:
    raw = _raw_config()

    no_label = copy.deepcopy(raw)
    del no_label["gates"]["mdd"]["maxDrawdown"]["label"]
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(no_label)

    no_desc = copy.deepcopy(raw)
    del no_desc["stage1"]["minAccountValue"]["description"]
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(no_desc)

    unknown_key = copy.deepcopy(raw)
    unknown_key["extraSection"] = {"x": {"value": 1, "label": "x", "description": "x"}}
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(unknown_key)


def test_weights_must_sum_to_one_and_max_tracked_bounds() -> None:
    raw = _raw_config()

    bad_weights = copy.deepcopy(raw)
    bad_weights["scoring"]["weights"]["winRate"]["value"] = 0.5
    with pytest.raises(ValidationError, match="sum to 1.0"):
        PipelineConfig.model_validate(bad_weights)

    bad_max_tracked = copy.deepcopy(raw)
    bad_max_tracked["coins"]["maxTracked"]["value"] = 2
    with pytest.raises(ValidationError, match="maxTracked"):
        PipelineConfig.model_validate(bad_max_tracked)


@given(blank=st.text(alphabet=" \t\r\n", min_size=0, max_size=8))
def test_whitespace_metadata_always_rejected(blank: str) -> None:
    raw = _raw_config()
    raw["ratelimit"]["ciBudgetPerMin"]["label"] = blank
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(raw)


def test_load_config_non_mapping_root_raises(tmp_path: Path) -> None:
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("- not a dict\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Expected mapping"):
        load_config(bad_yaml)


def test_config_strict_types_reject_bool_string_and_non_finite_floats() -> None:
    base = _raw_config()

    bad_schema_ver = copy.deepcopy(base)
    bad_schema_ver["schemaVersion"] = True
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(bad_schema_ver)

    bad_spot = copy.deepcopy(base)
    bad_spot["scope"]["spot"]["value"] = 0
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(bad_spot)

    bad_av_bool = copy.deepcopy(base)
    bad_av_bool["stage1"]["minAccountValue"]["value"] = True
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(bad_av_bool)

    bad_av_str = copy.deepcopy(base)
    bad_av_str["stage1"]["minAccountValue"]["value"] = "1000000"
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(bad_av_str)

    bad_pnl_nan = copy.deepcopy(base)
    bad_pnl_nan["stage1"]["minPnlAllTime"]["value"] = float("nan")
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(bad_pnl_nan)

    bad_ratio_inf = copy.deepcopy(base)
    bad_ratio_inf["stage1"]["minPnlMonthRatio"]["value"] = float("inf")
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(bad_ratio_inf)

    bad_label_type = copy.deepcopy(base)
    bad_label_type["ratelimit"]["ciBudgetPerMin"]["label"] = 123
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(bad_label_type)


def test_snapshot_reconstruct_1h_days_exceeding_reconstruct_days_rejected() -> None:
    raw = _raw_config()
    raw["snapshot"]["reconstruct1hDays"]["value"] = 120
    raw["snapshot"]["reconstructDays"]["value"] = 90
    with pytest.raises(ValidationError, match="reconstruct1hDays"):
        PipelineConfig.model_validate(raw)


def test_export_stage1_and_gate_rules_validate_against_criteria_contract() -> None:
    cfg = load_config()
    stage1_rules = cfg.export_stage1_rules()
    gate_rules = cfg.export_gate_rules()

    assert len(stage1_rules) == 4
    assert len(gate_rules) == 14
    exclude_roles = next(rule for rule in gate_rules if rule["key"] == "excludeRoles")
    assert exclude_roles["op"] == "not_in"
    assert exclude_roles["value"] == ["vault", "agent", "missing"]

    payload = {
        "schemaVersion": 1,
        "current": {
            "version": 1,
            "effectiveFrom": 1762128000000,
            "hash": "sha256:" + hashlib.sha256(b"test").hexdigest(),
            "stage1": stage1_rules,
            "gates": gate_rules,
            "membership": {
                "exitAfterFailedDays": cfg.membership.exitAfterFailedDays.value,
                "subaccounts": cfg.membership.subaccounts.value,
                "excludeVaults": cfg.membership.excludeVaults.value,
            },
            "scope": {
                "perpDex": cfg.scope.perpDex.value,
                "spot": cfg.scope.spot.value,
                "hip3": cfg.scope.hip3.value,
            },
            "stats": {
                "candidates": 10,
                "passed": 2,
                "rejectedBy": {"hft": 5, "pf": 3},
            },
        },
        "history": [],
    }
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "criteria.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(instance=payload, schema=schema, cls=jsonschema.Draft202012Validator)
    CriteriaFile.model_validate(payload)

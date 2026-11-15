# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit and property tests for config/pipeline.yaml loading and metadata validation."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from hyperpod.config import PipelineConfig, default_config_path, load_config


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

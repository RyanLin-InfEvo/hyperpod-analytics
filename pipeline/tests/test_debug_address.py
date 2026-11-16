# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI tests for `hyperpod debug-address` (offline respx, fixtures-dir, and live smoke)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from hyperpod.cli import app
from hyperpod.client.info_api import DEFAULT_INFO_URL

REPO_ROOT = Path(__file__).resolve().parents[2]
API_FIXTURES_DIR = REPO_ROOT / "fixtures" / "api"

_UNIVERSE_COINS = [
    "AAVE",
    "ADA",
    "AVAX",
    "BCH",
    "BNB",
    "BTC",
    "DOGE",
    "DOT",
    "ENA",
    "ETH",
    "FARTCOIN",
    "HBAR",
    "HYPE",
    "LINK",
    "LTC",
    "NEAR",
    "ONDO",
    "PUMP",
    "PURR",
    "SOL",
    "SUI",
    "WLD",
    "WLFI",
    "XLM",
    "XPL",
    "XRP",
    "ZEC",
    "ZORA",
]


def _load_fixture(name: str) -> Any:
    return json.loads((API_FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _build_meta_payload() -> dict[str, Any]:
    return {
        "universe": [
            {"name": coin, "szDecimals": 2, "maxLeverage": 20, "marginTableId": 1}
            for coin in _UNIVERSE_COINS
        ],
        "marginTables": [
            [1, {"description": "", "marginTiers": [{"lowerBound": "0.0", "maxLeverage": 20}]}]
        ],
        "collateralToken": 0,
    }


@pytest.mark.parametrize(
    ("address", "short", "expected_source", "expected_equity", "expected_pos_count"),
    [
        (
            "0xfc27136e42af1732ddc9ce2605ea9bff1b959d9d",
            "0xfc27136e",
            "perp",
            23_083_445.821943,
            22,
        ),
        (
            "0xb1ec7febea4f841d525785955b7fe0e812d6e86a",
            "0xb1ec7feb",
            "perp+spot",
            4_736_433.30574199,
            5,
        ),
    ],
)
def test_debug_address_cli_respx_offline(
    address: str,
    short: str,
    expected_source: str,
    expected_equity: float,
    expected_pos_count: int,
) -> None:
    meta_payload = _build_meta_payload()

    with respx.mock(assert_all_called=True) as router:

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            kind = body["type"]
            if kind == "meta":
                return httpx.Response(200, json=meta_payload)
            return httpx.Response(200, json=_load_fixture(f"{kind}_{short}.json"))

        router.post(DEFAULT_INFO_URL).mock(side_effect=handler)

        runner = CliRunner()
        res = runner.invoke(app, ["debug-address", address])

    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["address"] == address
    assert data["role"] == "user"
    assert data["equitySource"] == expected_source
    assert data["equity"] == pytest.approx(expected_equity, abs=1e-6)
    assert data["openPositionsCount"] == expected_pos_count
    assert data["rateLimitStats"]["totalRequests"] >= 7
    assert data["rateLimitStats"]["totalWeight"] > 0


def test_debug_address_fixtures_dir_and_validation_errors(tmp_path: Path) -> None:
    runner = CliRunner()

    # Subaccount fixture
    sub_addr = "0x6ecbb68c07bd4be7adb7f0365e9263e382aeca2d"
    res_sub = runner.invoke(
        app, ["debug-address", sub_addr, "--fixtures-dir", str(API_FIXTURES_DIR)]
    )
    assert res_sub.exit_code == 0, res_sub.output
    sub_data = json.loads(res_sub.output)
    assert sub_data["role"] == "subAccount"
    assert sub_data["master"] == "0xae6446c95cc4e455e9cf56f28757ab728d88e81e"
    assert sub_data["subAccountCount"] == 0
    assert sub_data["fillsSummary"]["rawFetchedCount"] == 200
    assert sub_data["fillsSummary"]["normalizedMainPerpCount"] == 57

    # Vault fixture
    vault_addr = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"
    res_vault = runner.invoke(
        app, ["debug-address", vault_addr, "--fixtures-dir", str(API_FIXTURES_DIR)]
    )
    assert res_vault.exit_code == 0, res_vault.output
    vault_data = json.loads(res_vault.output)
    assert vault_data["role"] == "vault"
    assert vault_data["openPositionsCount"] == 0

    # Invalid EVM address
    bad_addr = runner.invoke(app, ["debug-address", "not-an-address"])
    assert bad_addr.exit_code == 1
    assert "Invalid EVM address" in bad_addr.output

    # Missing fixture directory
    missing_res = runner.invoke(app, ["debug-address", sub_addr, "--fixtures-dir", str(tmp_path)])
    assert missing_res.exit_code == 1
    assert "Missing fixture file" in missing_res.output


@pytest.mark.live
def test_debug_address_live_hyperliquid_api() -> None:
    runner = CliRunner()
    address = "0xfc27136e42af1732ddc9ce2605ea9bff1b959d9d"
    res = runner.invoke(
        app,
        [
            "debug-address",
            address,
            "--max-fill-pages",
            "1",
            "--max-funding-pages",
            "1",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["address"] == address
    assert data["role"] == "user"
    assert data["equitySource"] in {"perp", "perp+spot"}
    assert data["equity"] >= 0.0
    assert data["rateLimitStats"]["totalRequests"] >= 7

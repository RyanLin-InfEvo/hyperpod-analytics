# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit and respx-mocked CLI integration tests for StateStore and discover/backfill/score."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError
from typer.testing import CliRunner

from hyperpod.cli import app
from hyperpod.config import load_config
from hyperpod.models import CandidatesFile, CriteriaFile, Whitelist
from hyperpod.scoring.daily_runner import (
    run_backfill,
    run_discover,
    run_score,
    validate_output_against_schema,
)
from hyperpod.scoring.whitelist import WhitelistStateRecord
from hyperpod.storage import (
    AddressDailyState,
    FillStatsState,
    StateStore,
    address_shard,
)
from hyperpod.util.time import DAY_MS, ms_to_utc_date, utc_date_to_start_ms

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
runner = CliRunner()


def _lb_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"leaderboardRows": rows}


def _lb_row_json(
    address: str,
    *,
    av: str = "5000000.0",
    all_time_pnl: str = "1500000.0",
    month_pnl: str = "200000.0",
    month_vlm: str = "12000000.0",
    display_name: str | None = None,
) -> dict[str, Any]:
    zero = {"pnl": "0.0", "roi": "0.0", "vlm": "0.0"}
    return {
        "ethAddress": address,
        "accountValue": av,
        "windowPerformances": [
            ["day", zero],
            ["week", zero],
            ["month", {"pnl": month_pnl, "roi": "0.05", "vlm": month_vlm}],
            ["allTime", {"pnl": all_time_pnl, "roi": "0.45", "vlm": "50000000.0"}],
        ],
        "prize": 0,
        "displayName": display_name,
    }


def _meta_payload() -> dict[str, Any]:
    return {
        "universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
            {"name": "ETH", "szDecimals": 4, "maxLeverage": 50},
            {"name": "SOL", "szDecimals": 2, "maxLeverage": 20},
            {"name": "HYPE", "szDecimals": 2, "maxLeverage": 10},
            {"name": "DOGE", "szDecimals": 0, "maxLeverage": 20},
        ],
        "marginTables": [],
    }


def _clearinghouse_payload(
    av: str,
    *,
    coin: str = "BTC",
    szi: str = "2.0",
    pos_val: str = "180000.0",
) -> dict[str, Any]:
    positions = (
        [
            {
                "type": "oneWay",
                "position": {
                    "coin": coin,
                    "cumFunding": {"allTime": "0", "sinceOpen": "0", "sinceChange": "0"},
                    "entryPx": "90000.0",
                    "leverage": {"type": "cross", "value": 5},
                    "marginUsed": "10000.0",
                    "maxLeverage": 50,
                    "positionValue": pos_val,
                    "returnOnEquity": "0.1",
                    "szi": szi,
                    "unrealizedPnl": "1000.0",
                },
            }
        ]
        if float(szi) != 0.0
        else []
    )
    ms = {
        "accountValue": av,
        "totalMarginUsed": "10000.0",
        "totalNtlPos": pos_val,
        "totalRawUsd": av,
    }
    return {
        "assetPositions": positions,
        "crossMaintenanceMarginUsed": "1000.0",
        "crossMarginSummary": ms,
        "marginSummary": ms,
        "time": 1_763_078_400_000,
        "withdrawable": av,
    }


def _portfolio_payload(av: float = 5_000_000.0, pnl: float = 1_500_000.0) -> list[Any]:
    w = {
        "accountValueHistory": [
            [1_750_000_000_000, "3500000.0"],
            [1_763_078_400_000, f"{av}"],
        ],
        "pnlHistory": [
            [1_750_000_000_000, "0.0"],
            [1_763_078_400_000, f"{pnl}"],
        ],
        "vlm": "12000000.0",
    }
    return [
        [name, w]
        for name in (
            "day",
            "week",
            "month",
            "allTime",
            "perpDay",
            "perpWeek",
            "perpMonth",
            "perpAllTime",
        )
    ]


def _winning_fills_payload(now_ms: int, n_trades: int = 22) -> list[dict[str, Any]]:
    """Build 22 complete main-perp round trips spanning 45 days (passes all gates)."""
    fills: list[dict[str, Any]] = []
    start_t = now_ms - 45 * DAY_MS
    tid = 1
    for i in range(n_trades):
        t_open = start_t + i * 2 * DAY_MS
        t_close = t_open + 4 * 3_600_000
        fills.append(
            {
                "coin": "BTC",
                "px": "90000.0",
                "sz": "1.0",
                "side": "B",
                "time": t_open,
                "startPosition": "0.0",
                "dir": "Open Long",
                "closedPnl": "0.0",
                "hash": f"0x{tid:064x}",
                "oid": tid,
                "crossed": False,
                "fee": "10.0",
                "tid": tid,
            }
        )
        tid += 1
        pnl = -2000.0 if i == 0 else 5000.0
        fills.append(
            {
                "coin": "BTC",
                "px": "95000.0",
                "sz": "1.0",
                "side": "A",
                "time": t_close,
                "startPosition": "1.0",
                "dir": "Close Long",
                "closedPnl": f"{pnl}",
                "hash": f"0x{tid:064x}",
                "oid": tid,
                "crossed": True,
                "fee": "10.0",
                "tid": tid,
            }
        )
        tid += 1
    return fills


def test_state_store_ownership_isolation_shards_and_runs_cap(tmp_path: Path) -> None:
    addr = "0xab11111111111111111111111111111111111111"
    assert address_shard(addr) == "ab"

    # AddressDailyState forbids raw fills / unknown keys (Hard Invariant #4)
    with pytest.raises(ValidationError):
        AddressDailyState.model_validate({"address": addr, "rawFills": []})

    daily_store = StateStore(tmp_path, owner="daily")
    fast_store = StateStore(tmp_path, owner="fast")
    bootstrap_store = StateStore(tmp_path, owner="bootstrap")

    rec = AddressDailyState(address=addr, role="user", fillsCursorScoring=123456)
    daily_store.put_address_state(rec)
    flushed = daily_store.flush_dirty_shards()
    assert flushed == ["ab"]
    assert (tmp_path / "daily" / "addresses" / "ab.json.gz").is_file()

    # Cross-partition writes are strictly forbidden
    with pytest.raises(PermissionError, match="forbidden"):
        fast_store.put_address_state(rec)
    with pytest.raises(PermissionError, match="forbidden"):
        fast_store.append_run_log("daily", {"job": "score"})
    with pytest.raises(PermissionError, match="forbidden"):
        daily_store.append_run_log("fast", {"job": "snapshot"})

    # Fast owner CAN read daily/whitelist_state.json read-only (§6.3)
    daily_store.save_whitelist_state(WhitelistStateRecord())
    assert fast_store.load_whitelist_state() is not None

    # Bootstrap owner can write both partitions
    bootstrap_store.append_run_log("fast", {"job": "bootstrap-fast"})
    bootstrap_store.append_run_log("daily", {"job": "bootstrap-daily"})

    # runs.jsonl keeps at most max_entries=500 lines
    for i in range(510):
        daily_store.append_run_log("daily", {"seq": i}, max_entries=500)
    lines = (tmp_path / "daily" / "runs.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 500
    assert json.loads(lines[0])["seq"] == 10
    assert json.loads(lines[-1])["seq"] == 509


def test_state_store_90_day_retention_and_pruning(tmp_path: Path) -> None:
    cfg = load_config()
    now_ms = utc_date_to_start_ms("2026-11-30")
    old_date = ms_to_utc_date(now_ms - 100 * DAY_MS)
    keep_date = ms_to_utc_date(now_ms - 5 * DAY_MS)

    store = StateStore(tmp_path, owner="daily")
    addr_pruned = "0x1111111111111111111111111111111111111111"
    addr_seed = "0x2222222222222222222222222222222222222222"
    addr_ever_wl = "0x3333333333333333333333333333333333333333"
    addr_active_s1 = "0x4444444444444444444444444444444444444444"

    for a, streak in [
        (addr_pruned, 90),  # +1 -> 91 > 90 => pruned
        (addr_seed, 95),  # seed => never pruned
        (addr_ever_wl, 95),  # ever whitelisted => never pruned
        (addr_active_s1, 45),  # active Stage 1 => reset to 0
    ]:
        store.put_address_state(
            AddressDailyState(
                address=a,
                absentStage1Streak=streak,
                fillStats=FillStatsState(
                    days={old_date: (10, 5), keep_date: (3, 1)},
                    notionalDays={old_date: 1000.0, keep_date: 500.0},
                ),
            )
        )
    store.flush_dirty_shards()

    stats = store.prune_daily_state(
        now_ms=now_ms,
        cfg=cfg,
        active_stage1_addresses={addr_active_s1},
        seed_addresses={addr_seed},
        ever_whitelisted_addresses={addr_ever_wl},
    )
    assert stats["candidatesPruned"] == 1
    assert stats["fillStatsEntriesPruned"] == 8  # 4 records * (old day + old notional)

    # Reload fresh store from disk to verify persisted state
    reloaded = StateStore(tmp_path, owner="daily")
    assert reloaded.get_address_state(addr_pruned) is None
    seed_rec = reloaded.get_address_state(addr_seed)
    assert seed_rec is not None
    assert seed_rec.fillStats.days == {keep_date: (3, 1)}
    assert seed_rec.fillStats.notionalDays == {keep_date: 500.0}
    assert reloaded.get_address_state(addr_ever_wl) is not None
    active_rec = reloaded.get_address_state(addr_active_s1)
    assert active_rec is not None
    assert active_rec.absentStage1Streak == 0


def test_discover_backfill_checkpoint_resume_and_score_e2e(tmp_path: Path) -> None:
    cfg = load_config()
    now_ms = utc_date_to_start_ms("2026-11-15") + 12 * 3_600_000
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    seed_file = tmp_path / "seed_addresses.txt"
    blocklist_file = tmp_path / "blocklist.txt"

    addr_master = "0x1111111111111111111111111111111111111111"
    addr_vault = "0x2222222222222222222222222222222222222222"
    addr_sub = "0x3333333333333333333333333333333333333333"
    addr_seed_fail = "0x4444444444444444444444444444444444444444"
    addr_blocked = "0x5555555555555555555555555555555555555555"

    seed_file.write_text(f"{addr_seed_fail}\n", encoding="utf-8")
    blocklist_file.write_text(f"{addr_blocked}\n", encoding="utf-8")

    lb_url = "https://mock.hyperliquid.local/leaderboard"
    info_url = "https://mock.hyperliquid.local/info"

    fills_good = _winning_fills_payload(now_ms, n_trades=22)
    # Only 2 trades -> fails passive & sample & should NOT trigger userFunding call!
    fills_bad = _winning_fills_payload(now_ms, n_trades=2)

    sub_item = {
        "name": "doge-sub",
        "subAccountUser": addr_sub,
        "master": addr_master,
        "clearinghouseState": _clearinghouse_payload(
            "2000000.0", coin="DOGE", szi="1000000.0", pos_val="250000.0"
        ),
        "spotState": {"balances": []},
    }

    funding_called_for: list[str] = []

    with respx.mock(assert_all_called=False) as mock_router:
        mock_router.get(lb_url).mock(
            return_value=httpx.Response(
                200,
                json=_lb_payload(
                    [
                        _lb_row_json(addr_master, display_name="WhaleMaster"),
                        _lb_row_json(addr_vault, display_name="ProtocolVault"),
                        _lb_row_json(addr_blocked, display_name="Blocked"),
                    ]
                ),
            )
        )

        def info_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            req_type = body["type"]
            if req_type == "meta":
                return httpx.Response(200, json=_meta_payload())
            user = body["user"].lower()
            if req_type == "userRole":
                if user == addr_vault:
                    return httpx.Response(200, json={"role": "vault"})
                if user == addr_sub:
                    return httpx.Response(
                        200, json={"role": "subAccount", "data": {"master": addr_master}}
                    )
                return httpx.Response(200, json={"role": "user"})
            if req_type == "subAccounts":
                if user == addr_master:
                    return httpx.Response(200, json=[sub_item])
                return httpx.Response(200, json=[])
            if req_type == "userAbstraction":
                return httpx.Response(
                    200, json="unifiedAccount" if user == addr_master else "disabled"
                )
            if req_type == "clearinghouseState":
                if user == addr_sub:
                    return httpx.Response(200, json=sub_item["clearinghouseState"])
                return httpx.Response(200, json=_clearinghouse_payload("5000000.0"))
            if req_type == "spotClearinghouseState":
                return httpx.Response(
                    200,
                    json={
                        "balances": [
                            {
                                "coin": "USDC",
                                "token": 0,
                                "total": "5000000.0",
                                "hold": "0.0",
                                "entryNtl": "0.0",
                            }
                        ]
                    },
                )
            if req_type == "portfolio":
                return httpx.Response(200, json=_portfolio_payload())
            if req_type == "userFillsByTime":
                if user in (addr_master, addr_sub):
                    return httpx.Response(200, json=fills_good)
                return httpx.Response(200, json=fills_bad)
            if req_type == "userFunding":
                funding_called_for.append(user)
                return httpx.Response(
                    200,
                    json=[
                        {
                            "time": now_ms - DAY_MS,
                            "hash": "0x" + "0" * 64,
                            "delta": {
                                "type": "funding",
                                "coin": "BTC",
                                "usdc": "-1500.0",
                                "szi": "1.0",
                                "fundingRate": "0.0001",
                            },
                        }
                    ],
                )
            raise AssertionError(f"Unexpected Info API payload: {body}")

        mock_router.post(info_url).mock(side_effect=info_handler)

        async def _run_flow() -> Any:
            store = StateStore(state_dir, owner="daily")
            discovered = await run_discover(
                cfg,
                store,
                seed_file=seed_file,
                blocklist_file=blocklist_file,
                leaderboard_url=lb_url,
                now_ms=now_ms,
            )
            assert [c.address for c in discovered.candidates] == [
                addr_master,
                addr_vault,
                addr_seed_fail,
            ]

            # Backfill checkpoint pause after 1 address, then resume to completion
            bf_part = await run_backfill(
                cfg,
                store,
                seed_file=seed_file,
                blocklist_file=blocklist_file,
                checkpoint_every=1,
                limit=1,
                leaderboard_url=lb_url,
                info_url=info_url,
            )
            assert not bf_part.completed
            assert bf_part.processed_this_run == 1
            assert bf_part.expanded_subaccounts == 1  # addr_sub expanded from addr_master!
            assert bf_part.total_candidates == 4

            bf_rest = await run_backfill(
                cfg,
                store,
                seed_file=seed_file,
                blocklist_file=blocklist_file,
                checkpoint_every=1,
                leaderboard_url=lb_url,
                info_url=info_url,
            )
            assert bf_rest.completed
            assert bf_rest.processed_this_run == 3
            assert bf_rest.total_processed == 4

            # Crucial quota check (§5.1): userFunding skipped for addr_vault and addr_seed_fail!
            assert sorted(funding_called_for) == sorted([addr_master, addr_sub])

            return await run_score(
                cfg,
                store,
                seed_file=seed_file,
                blocklist_file=blocklist_file,
                schemas_dir=SCHEMAS_DIR,
                data_dir=data_dir,
                offline=True,
                now_ms=now_ms,
            )

        score_res = asyncio.run(_run_flow())

    assert score_res.whitelist_update.active_count == 2
    wl = Whitelist.model_validate(json.loads((data_dir / "whitelist.json").read_text("utf-8")))
    crit = CriteriaFile.model_validate(json.loads((data_dir / "criteria.json").read_text("utf-8")))
    cand_file = CandidatesFile.model_validate(
        json.loads((data_dir / "candidates.json").read_text("utf-8"))
    )

    validate_output_against_schema(
        wl.model_dump(mode="json", by_alias=True), SCHEMAS_DIR / "whitelist.schema.json"
    )
    validate_output_against_schema(
        crit.model_dump(mode="json", by_alias=True), SCHEMAS_DIR / "criteria.schema.json"
    )
    validate_output_against_schema(
        cand_file.model_dump(mode="json", by_alias=True), SCHEMAS_DIR / "candidates.schema.json"
    )

    assert [a.address for a in wl.addresses] == [addr_master, addr_sub]
    assert wl.addresses[1].master == addr_master
    assert crit.current.stats.candidates == 4
    assert crit.current.stats.passed == 2
    assert crit.current.stats.rejectedBy["role"] == 1
    assert crit.current.stats.rejectedBy["sample"] == 1
    tracked = {c.coin for c in score_res.whitelist_update.state.coins if c.trackedTo is None}
    assert tracked == {"BTC", "ETH", "SOL", "HYPE", "DOGE"}

    # Verify CLI score --offline and backfill --budget-minutes=0 invocation
    cli_score = runner.invoke(
        app,
        [
            "score",
            "--offline",
            "--state-dir",
            str(state_dir),
            "--data-dir",
            str(tmp_path / "cli-data"),
            "--seed-file",
            str(seed_file),
            "--blocklist-file",
            str(blocklist_file),
        ],
    )
    assert cli_score.exit_code == 0, cli_score.stdout + cli_score.stderr
    cli_payload = json.loads(cli_score.stdout)
    assert cli_payload["activeWhitelistCount"] == 2
    assert (tmp_path / "cli-data" / "whitelist.json").is_file()


def test_cli_backfill_budget_minutes_zero_stops_cleanly(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    seed_file = tmp_path / "seed.txt"
    blocklist_file = tmp_path / "block.txt"
    seed_file.write_text("", encoding="utf-8")
    blocklist_file.write_text("", encoding="utf-8")

    lb_url = "https://mock.hyperliquid.local/leaderboard"
    info_url = "https://mock.hyperliquid.local/info"
    addr = "0x1111111111111111111111111111111111111111"

    with respx.mock(assert_all_called=False) as mock_router:
        mock_router.get(lb_url).mock(
            return_value=httpx.Response(200, json=_lb_payload([_lb_row_json(addr)]))
        )
        mock_router.post(info_url).mock(return_value=httpx.Response(200, json=_meta_payload()))

        res = runner.invoke(
            app,
            [
                "backfill",
                "--budget-minutes",
                "0",
                "--state-dir",
                str(state_dir),
                "--seed-file",
                str(seed_file),
                "--blocklist-file",
                str(blocklist_file),
                "--leaderboard-url",
                lb_url,
                "--info-url",
                info_url,
            ],
        )
    assert res.exit_code == 0, res.stdout + res.stderr
    out = json.loads(res.stdout)
    assert out["completed"] is False
    assert out["budgetExhausted"] is True
    assert out["processedThisRun"] == 0
    assert (state_dir / "daily" / "backfill_cursor.json").is_file()


def test_audit_regressions_shard_validation_immutability_and_schema_missing(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path, owner="daily")

    # [FINDING-07] Invalid hex / path traversal rejected in address_shard & shard_path
    with pytest.raises(ValueError, match="Invalid EVM address"):
        address_shard("0x/t" + "a" * 38)
    with pytest.raises(ValueError, match="Invalid shard"):
        store.shard_path("/t")

    # [FINDING-08] Missing schema path must raise FileNotFoundError
    with pytest.raises(FileNotFoundError):
        validate_output_against_schema({}, tmp_path / "nonexistent.schema.json")

    # [FINDING-02] Mutating get_address_state return object without put_address_state
    # must NOT leak into _shard_cache / disk flush of another address in same shard.
    addr1 = "0xab11111111111111111111111111111111111111"
    addr2 = "0xab22222222222222222222222222222222222222"
    store.put_address_state(AddressDailyState(address=addr1, role="user", fillsCursorScoring=100))
    store.flush_dirty_shards()

    leaked = store.get_address_state(addr1)
    assert leaked is not None
    leaked.fillsCursorScoring = 999_999
    leaked.abstraction = "corrupted"

    store.put_address_state(AddressDailyState(address=addr2, role="user"))
    store.flush_dirty_shards()

    reloaded = StateStore(tmp_path, owner="daily").get_address_state(addr1)
    assert reloaded is not None
    assert reloaded.fillsCursorScoring == 100
    assert reloaded.abstraction is None


def test_audit_regressions_subaccount_cache_error_grace_offline_and_failstreak(
    tmp_path: Path,
) -> None:
    cfg = load_config().model_copy(deep=True)
    cfg.ratelimit.maxRetries.value = 0

    now_ms = utc_date_to_start_ms("2026-11-15") + 12 * 3_600_000
    state_dir = tmp_path / "state"
    seed_file = tmp_path / "seed.txt"
    blocklist_file = tmp_path / "blocklist.txt"

    addr_master = "0x1111111111111111111111111111111111111111"
    addr_sub = "0x3333333333333333333333333333333333333333"
    addr_dormant = "0x9999999999999999999999999999999999999999"
    addr_missing_then_user = "0x7777777777777777777777777777777777777777"

    seed_file.write_text(f"{addr_missing_then_user}\n", encoding="utf-8")
    blocklist_file.write_text("", encoding="utf-8")

    lb_url = "https://mock.hyperliquid.local/leaderboard"
    info_url = "https://mock.hyperliquid.local/info"
    fills_good = _winning_fills_payload(now_ms, n_trades=22)
    fills_bad = _winning_fills_payload(now_ms, n_trades=2)
    sub_item = {
        "name": "sub-1",
        "subAccountUser": addr_sub,
        "master": addr_master,
        "clearinghouseState": _clearinghouse_payload("2000000.0"),
        "spotState": {"balances": []},
    }

    fail_master_funding = False
    fail_master_clearinghouse = False
    missing_role_now_user = False

    with respx.mock(assert_all_called=False) as mock_router:
        mock_router.get(lb_url).mock(
            return_value=httpx.Response(200, json=_lb_payload([_lb_row_json(addr_master)]))
        )

        def info_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            req_type = body["type"]
            if req_type == "meta":
                return httpx.Response(200, json=_meta_payload())
            user = body["user"].lower()
            if req_type == "userRole":
                if user == addr_sub:
                    return httpx.Response(
                        200, json={"role": "subAccount", "data": {"master": addr_master}}
                    )
                if user == addr_missing_then_user and not missing_role_now_user:
                    return httpx.Response(200, json={"role": "missing"})
                return httpx.Response(200, json={"role": "user"})
            if req_type == "subAccounts":
                return httpx.Response(200, json=[sub_item] if user == addr_master else [])
            if req_type == "userAbstraction":
                return httpx.Response(200, json="disabled")
            if req_type == "clearinghouseState":
                if user == addr_master and fail_master_clearinghouse:
                    return httpx.Response(500, text="transient error")
                return httpx.Response(200, json=_clearinghouse_payload("3000000.0"))
            if req_type == "spotClearinghouseState":
                return httpx.Response(200, json={"balances": []})
            if req_type == "portfolio":
                return httpx.Response(200, json=_portfolio_payload())
            if req_type == "userFillsByTime":
                return httpx.Response(
                    200, json=fills_good if user in (addr_master, addr_sub) else fills_bad
                )
            if req_type == "userFunding":
                if user == addr_master and fail_master_funding:
                    return httpx.Response(500, text="funding transient error")
                return httpx.Response(200, json=[])
            raise AssertionError(f"Unexpected payload: {body}")

        mock_router.post(info_url).mock(side_effect=info_handler)

        async def _scenario() -> None:
            nonlocal fail_master_funding, fail_master_clearinghouse, missing_role_now_user
            store = StateStore(state_dir, owner="daily")

            # Seed a dormant non-Stage-1 shard address that would pass if erroneously scanned
            store.put_address_state(
                AddressDailyState(
                    address=addr_dormant,
                    role="user",
                    absentStage1Streak=60,
                    fillsHistoryStart=now_ms - 60 * DAY_MS,
                    lastFillTime=now_ms - DAY_MS,
                    fillStats=FillStatsState(days={ms_to_utc_date(now_ms - DAY_MS): (10, 4)}),
                    lastScore={
                        "evaluatedAt": now_ms - 60 * DAY_MS,
                        "metrics": {
                            "accountValue": 5_000_000.0,
                            "equitySource": "perp",
                            "pnlAllTime": 1_000_000.0,
                            "roiAllTime": 0.4,
                            "pnlMonth": 100_000.0,
                            "maxDrawdown": 0.1,
                            "profitFactor": 2.5,
                            "profitFactorRobust": 2.0,
                            "winRate": 0.6,
                            "nTrades": 30,
                            "historyDays": 100.0,
                            "fillsPerDay": 10.0,
                            "medianHoldMinutes": 200.0,
                            "makerRatio": 0.4,
                            "turnoverMonth": 5.0,
                            "fundingShare": 0.02,
                            "topWinShare": 0.2,
                            "hedgeRatio": 0.0,
                        },
                        "rejectReasons": [],
                        "openPositionsNotional": {},
                    },
                )
            )
            store.flush_dirty_shards()

            # 1. [FINDING-01] Backfill discovers addr_sub & caches subAccountsUpdatedAt;
            #    subsequent online run_score (within 7d cache) MUST still include addr_sub!
            bf = await run_backfill(
                cfg,
                store,
                seed_file=seed_file,
                blocklist_file=blocklist_file,
                leaderboard_url=lb_url,
                info_url=info_url,
            )
            assert bf.completed

            # [FINDING-08] addr_missing_then_user was cached as "missing", now becomes "user"
            missing_role_now_user = True
            s1 = await run_score(
                cfg,
                store,
                seed_file=seed_file,
                blocklist_file=blocklist_file,
                schemas_dir=SCHEMAS_DIR,
                offline=False,
                leaderboard_url=lb_url,
                info_url=info_url,
                now_ms=now_ms,
            )
            wl_addrs = {a.address for a in s1.whitelist_update.whitelist.addresses}
            assert addr_sub in wl_addrs
            assert addr_master in wl_addrs
            assert addr_dormant not in wl_addrs
            missing_state = store.get_address_state(addr_missing_then_user)
            assert missing_state is not None
            assert missing_state.role == "user"

            # [FINDING-04] Dormant shard was NOT resurrected and streak incremented 60 -> 61
            dormant_after = store.get_address_state(addr_dormant)
            assert dormant_after is not None
            assert dormant_after.absentStage1Streak == 61

            # [FINDING-04] Offline run_score + 10 days later recomputes time-windowed historyDays
            later_ms = now_ms + 10 * DAY_MS
            s_off = await run_score(
                cfg,
                store,
                seed_file=seed_file,
                blocklist_file=blocklist_file,
                schemas_dir=SCHEMAS_DIR,
                offline=True,
                now_ms=later_ms,
            )
            master_wl = next(
                a for a in s_off.whitelist_update.whitelist.addresses if a.address == addr_master
            )
            master_s1 = next(
                a for a in s1.whitelist_update.whitelist.addresses if a.address == addr_master
            )
            assert master_wl.metrics.historyDays == pytest.approx(
                master_s1.metrics.historyDays + 10.0, abs=1e-6
            )
            dormant_after_off = store.get_address_state(addr_dormant)
            assert dormant_after_off is not None
            assert dormant_after_off.absentStage1Streak == 62

            # [FINDING-02] Transient error on active whitelist member preserves failStreak == 0
            # and does NOT corrupt shard cursor/state
            before_master_state = store.get_address_state(addr_master)
            assert before_master_state is not None
            fail_master_funding = True
            s_err = await run_score(
                cfg,
                store,
                seed_file=seed_file,
                blocklist_file=blocklist_file,
                schemas_dir=SCHEMAS_DIR,
                offline=False,
                leaderboard_url=lb_url,
                info_url=info_url,
                now_ms=later_ms + DAY_MS,
            )
            assert s_err.errors == 1
            assert s_err.whitelist_update.state.addresses[addr_master].failStreak == 0
            assert s_err.whitelist_update.state.addresses[addr_master].is_active
            after_err_state = store.get_address_state(addr_master)
            assert after_err_state is not None
            assert after_err_state.lastScore == before_master_state.lastScore

            # [FINDING-08] When a whitelisted address fails hard gates (e.g. tightened PF),
            # AddressDailyState.failStreak syncs with WhitelistMemberRecord.failStreak
            fail_master_funding = False
            fail_master_clearinghouse = False
            cfg_tight = cfg.model_copy(deep=True)
            cfg_tight.gates.pf.minRobust.value = 100.0
            s_fail = await run_score(
                cfg_tight,
                store,
                seed_file=seed_file,
                blocklist_file=blocklist_file,
                schemas_dir=SCHEMAS_DIR,
                offline=True,
                now_ms=later_ms + 2 * DAY_MS,
            )
            assert s_fail.whitelist_update.state.addresses[addr_master].failStreak == 1
            synced_state = store.get_address_state(addr_master)
            assert synced_state is not None
            assert synced_state.failStreak == 1

        asyncio.run(_scenario())

# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline tests for Phase 4.4 ops/health, verify-live, and Phase 4 CLI commands."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import respx
from typer.testing import CliRunner

from hyperpod.cli import app
from hyperpod.config import load_config
from hyperpod.models import Snapshot, Whitelist
from hyperpod.models.snapshot import SnapshotAvSeries, SnapshotKeyframe, SnapshotSeries
from hyperpod.ops.health import evaluate_health, verify_live_positions
from hyperpod.storage.formats import read_json_file, write_json_file_atomic
from hyperpod.storage.site_writer import validate_site_data_directory
from hyperpod.storage.state_store import StateStore
from hyperpod.util.time import DAY_MS, HOUR_MS, MIN_MS, ms_to_utc_date, utc_date_to_start_ms

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "schemas"
SITE_DATA_FIXTURE = REPO_ROOT / "fixtures" / "site-data"
INFO_URL = "https://api.hyperliquid.xyz/info"
LB_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
runner = CliRunner()


def _write_synthetic_live_window(
    state_dir: Path,
    data_dir: Path,
    *,
    now_ms: int,
    hours: int = 24,
    fail_indices: set[int] | None = None,
    skip_snapshot_indices: set[int] | None = None,
    weight_per_run: int = 1200,
) -> None:
    fail_set = fail_indices or set()
    skip_set = skip_snapshot_indices or set()
    launched_at = now_ms - hours * HOUR_MS
    write_json_file_atomic(
        data_dir / "manifest.json",
        {
            "schemaVersion": 1,
            "generatedAt": now_ms,
            "latestSnapshotAt": now_ms,
            "launchedAt": launched_at,
            "whitelistVersion": ms_to_utc_date(now_ms),
            "criteriaVersion": 1,
            "coins": [
                {"coin": "BTC", "pinned": True, "trackedFrom": launched_at, "trackedTo": None}
            ],
            "files": {"snapshotDays": [], "snapshotMonths": [], "fills": {}, "candles": {}},
            "storage": {"totalBytesOnDisk": 100, "softLimitBytes": 800_000_000},
            "pipeline": {"fastLastOk": now_ms, "dailyLastOk": now_ms, "warnings": []},
        },
        compress=False,
    )

    store = StateStore(state_dir, owner="fast")
    n_ticks = (hours * 60) // 10
    ts_by_day: dict[str, list[int]] = {}
    for idx in range(n_ticks + 1):
        t = launched_at + idx * 10 * MIN_MS
        status = "aborted" if idx in fail_set else "ok"
        store.append_run_log(
            "fast",
            {
                "job": "snapshot",
                "status": status,
                "ts": t,
                "rateLimitStats": {"totalWeight": weight_per_run},
            },
            max_entries=2000,
        )
        if idx not in fail_set and idx not in skip_set:
            ts_by_day.setdefault(ms_to_utc_date(t), []).append(t)

    for day_str, ts_list in sorted(ts_by_day.items()):
        snap = Snapshot(
            schemaVersion=1,
            coins=["BTC"],
            reconstructed=False,
            tracked=[(0, ts_list[0], None)],
            keyframe=SnapshotKeyframe(t=ts_list[0], pos=[]),
            posChanges=[],
            series=SnapshotSeries(
                t=ts_list,
                mark=[[95_000.0] * len(ts_list)],
                av=SnapshotAvSeries(addrIds=[0], values=[[2_000_000.0] * len(ts_list)]),
            ),
        )
        write_json_file_atomic(
            data_dir / "snapshots" / "days" / f"{day_str}.json",
            snap.model_dump(mode="json", exclude_none=True),
            compress=False,
        )


def test_evaluate_health_passing_and_failure_conditions(tmp_path: Path) -> None:
    now_ms = utc_date_to_start_ms("2026-11-16") + 12 * HOUR_MS

    # 1. Healthy 24h window
    s_ok = tmp_path / "ok" / "state"
    d_ok = tmp_path / "ok" / "data"
    _write_synthetic_live_window(
        s_ok, d_ok, now_ms=now_ms, hours=24, fail_indices={1}, weight_per_run=1100
    )
    rep_ok = evaluate_health(s_ok, d_ok, hours=24.0, now_ms=now_ms)
    assert rep_ok.healthy is True
    assert rep_ok.fast_success_rate >= 0.95
    assert rep_ok.avg_fast_weight == 1100.0
    assert rep_ok.gaps == []

    # 2. Unhealthy: success rate < 95% (10/145 aborted)
    s_sr = tmp_path / "sr" / "state"
    d_sr = tmp_path / "sr" / "data"
    _write_synthetic_live_window(
        s_sr, d_sr, now_ms=now_ms, hours=24, fail_indices=set(range(1, 20))
    )
    rep_sr = evaluate_health(s_sr, d_sr, hours=24.0, now_ms=now_ms)
    assert rep_sr.healthy is False
    assert any("success rate" in r.lower() for r in rep_sr.reasons)

    # 3. Unhealthy: avg fast weight >= 1500
    s_w = tmp_path / "w" / "state"
    d_w = tmp_path / "w" / "data"
    _write_synthetic_live_window(s_w, d_w, now_ms=now_ms, hours=24, weight_per_run=1600)
    rep_w = evaluate_health(s_w, d_w, hours=24.0, now_ms=now_ms)
    assert rep_w.healthy is False
    assert any("weight" in r.lower() for r in rep_w.reasons)

    # 4. Unhealthy: >30 minute snapshot gap (missing ticks 10, 11, 12 -> 40m gap)
    s_gap = tmp_path / "gap" / "state"
    d_gap = tmp_path / "gap" / "data"
    _write_synthetic_live_window(
        s_gap, d_gap, now_ms=now_ms, hours=24, skip_snapshot_indices={10, 11, 12}
    )
    rep_gap = evaluate_health(s_gap, d_gap, hours=24.0, now_ms=now_ms)
    assert rep_gap.healthy is False
    assert len(rep_gap.gaps) == 1
    assert rep_gap.max_gap_minutes_observed >= 40.0


def test_verify_live_positions_reconciles_post_snapshot_fills(tmp_path: Path) -> None:
    cfg = load_config()
    data_dir = tmp_path / "data"
    shutil.copytree(SITE_DATA_FIXTURE, data_dir)

    latest = Snapshot.model_validate(read_json_file(data_dir / "snapshots" / "latest.json"))
    t_snap = latest.keyframe.t
    wl = Whitelist.model_validate(read_json_file(data_dir / "whitelist.json"))
    by_id = {a.id: a.address.lower() for a in wl.addresses}

    # In fixture latest.json:
    # id=0 has BTC +8.0, ETH +100.0; add post-snapshot fill +1.5 BTC -> live BTC = 9.5, ETH = 100.0!
    # Also return a pre-snapshot overlap fill (time <= t_snap) that MUST be ignored!
    def handle_info(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        ep = body["type"]
        if ep == "meta":
            return httpx.Response(
                200,
                json={
                    "universe": [
                        {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
                        {"name": "ETH", "szDecimals": 4, "maxLeverage": 50},
                    ],
                    "marginTables": [],
                },
            )
        user = body["user"].lower()
        if ep == "clearinghouseState":
            if user == by_id[0]:
                pos = [
                    ("BTC", "9.5", "95000.0"),
                    ("ETH", "100.0", "3080.0"),
                ]
            elif user == by_id[1]:
                pos = [("BTC", "2.0", "94000.0"), ("ETH", "60.0", "3120.0")]
            elif user == by_id[2]:
                pos = [("BTC", "-2.0", "95000.0")]
            else:
                pos = [("BTC", "5.0", "95000.0"), ("ETH", "85.0", "2972.26")]
            ms = {
                "accountValue": "5000000",
                "totalMarginUsed": "1000",
                "totalNtlPos": "100000",
                "totalRawUsd": "5000000",
            }
            return httpx.Response(
                200,
                json={
                    "assetPositions": [
                        {
                            "type": "oneWay",
                            "position": {
                                "coin": c,
                                "cumFunding": {
                                    "allTime": "0",
                                    "sinceOpen": "0",
                                    "sinceChange": "0",
                                },
                                "entryPx": epx,
                                "leverage": {"type": "cross", "value": 5},
                                "marginUsed": "100",
                                "maxLeverage": 50,
                                "positionValue": "10000",
                                "returnOnEquity": "0.1",
                                "szi": szi,
                                "unrealizedPnl": "0",
                            },
                        }
                        for c, szi, epx in pos
                    ],
                    "crossMaintenanceMarginUsed": "100",
                    "crossMarginSummary": ms,
                    "marginSummary": ms,
                    "time": t_snap + 120_000,
                    "withdrawable": "4000000",
                },
            )
        if ep == "userFillsByTime":
            if user == by_id[0]:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "coin": "BTC",
                            "px": "94000.0",
                            "sz": "10.0",
                            "side": "B",
                            "time": t_snap - 10_000,  # Overlap fill <= t_snap: MUST ignore!
                            "startPosition": "0.0",
                            "dir": "Open Long",
                            "closedPnl": "0.0",
                            "hash": "0x" + "1" * 64,
                            "oid": 1,
                            "crossed": False,
                            "fee": "1.0",
                            "tid": 1,
                        },
                        {
                            "coin": "BTC",
                            "px": "96000.0",
                            "sz": "1.5",
                            "side": "B",
                            "time": t_snap + 30_000,  # Post-snapshot fill > t_snap
                            "startPosition": "8.0",
                            "dir": "Open Long",
                            "closedPnl": "0.0",
                            "hash": "0x" + "2" * 64,
                            "oid": 2,
                            "crossed": False,
                            "fee": "1.0",
                            "tid": 2,
                        },
                    ],
                )
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected endpoint {ep}")

    with respx.mock() as mock_router:
        mock_router.post(INFO_URL).mock(side_effect=handle_info)
        rep = asyncio.run(verify_live_positions(data_dir, cfg, sample=4, seed=42))
        assert rep.passed is True
        assert rep.sampled_count == 4
        assert rep.mismatches == []

        # CLI verify-live also exits 0
        cli_res = runner.invoke(
            app,
            ["verify-live", "--sample", "4", "--seed", "42", "--data-dir", str(data_dir)],
        )
        assert cli_res.exit_code == 0, cli_res.output

    # Negative check: mismatched live position must fail verify-live with exit_code == 1
    def handle_mismatch(request: httpx.Request) -> httpx.Response:
        resp = handle_info(request)
        body = json.loads(request.content.decode("utf-8"))
        if body["type"] == "clearinghouseState" and body["user"].lower() == by_id[0]:
            payload = json.loads(resp.content.decode("utf-8"))
            payload["assetPositions"][0]["position"]["szi"] = "99.0"
            return httpx.Response(200, json=payload)
        return resp

    with respx.mock() as mock_router:
        mock_router.post(INFO_URL).mock(side_effect=handle_mismatch)
        bad_rep = asyncio.run(verify_live_positions(data_dir, cfg, sample=4, seed=42))
        assert bad_rep.passed is False
        assert len(bad_rep.mismatches) == 1
        assert bad_rep.mismatches[0].addr_id == 0
        assert bad_rep.mismatches[0].coin == "BTC"
        bad_cli = runner.invoke(
            app,
            ["verify-live", "--sample", "4", "--seed", "42", "--data-dir", str(data_dir)],
        )
        assert bad_cli.exit_code == 1


def test_cli_bootstrap_and_health_e2e(tmp_path: Path) -> None:
    # Use short reconstructDays via custom config file so bootstrap CLI completes in <1s
    cfg_text = (REPO_ROOT / "config" / "pipeline.yaml").read_text("utf-8")
    cfg_text = cfg_text.replace("value: 365", "value: 10").replace("value: 208", "value: 5")
    # Also relax sample/hft gates slightly? No, construct winning fills passing default gates!
    cfg_path = tmp_path / "pipeline.yaml"
    cfg_path.write_text(cfg_text, encoding="utf-8")

    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    seed_file = tmp_path / "seed.txt"
    blocklist_file = tmp_path / "block.txt"
    addr = "0x1111111111111111111111111111111111111111"
    seed_file.write_text(addr + "\n", encoding="utf-8")
    blocklist_file.write_text("", encoding="utf-8")

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    now_ms = int(utc_date_to_start_ms("2026-11-15") + 12 * HOUR_MS)
    # Build 22 winning round trips over 45 days ending 1h before now_ms
    fills: list[dict[str, Any]] = []
    start_t = now_ms - 45 * DAY_MS
    tid = 1
    for i in range(22):
        t_o = start_t + i * 2 * DAY_MS
        t_c = t_o + 4 * HOUR_MS
        fills.append(
            {
                "coin": "BTC",
                "px": "90000.0",
                "sz": "1.0",
                "side": "B",
                "time": t_o,
                "startPosition": "0.0",
                "dir": "Open Long",
                "closedPnl": "0.0",
                "hash": f"0x{tid:064x}",
                "oid": tid,
                "crossed": False,
                "fee": "5.0",
                "tid": tid,
            }
        )
        tid += 1
        fills.append(
            {
                "coin": "BTC",
                "px": "95000.0",
                "sz": "1.0",
                "side": "A",
                "time": t_c,
                "startPosition": "1.0",
                "dir": "Close Long",
                "closedPnl": "-1000.0" if i == 0 else "5000.0",
                "hash": f"0x{tid:064x}",
                "oid": tid,
                "crossed": True,
                "fee": "5.0",
                "tid": tid,
            }
        )
        tid += 1

    zero_win = {"pnl": "0.0", "roi": "0.0", "vlm": "0.0"}
    lb_payload = {
        "leaderboardRows": [
            {
                "ethAddress": addr,
                "accountValue": "5000000.0",
                "windowPerformances": [
                    ["day", zero_win],
                    ["week", zero_win],
                    ["month", {"pnl": "200000.0", "roi": "0.05", "vlm": "12000000.0"}],
                    ["allTime", {"pnl": "1500000.0", "roi": "0.45", "vlm": "50000000.0"}],
                ],
                "prize": 0,
                "displayName": "Bootstrap Whale",
            }
        ]
    }
    pf_win = {
        "accountValueHistory": [[start_t, "3500000.0"], [now_ms, "5000000.0"]],
        "pnlHistory": [[start_t, "0.0"], [now_ms, "1500000.0"]],
        "vlm": "12000000.0",
    }
    pf_payload = [
        [w, pf_win]
        for w in (
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
    meta_universe = [
        {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
        {"name": "ETH", "szDecimals": 4, "maxLeverage": 50},
        {"name": "SOL", "szDecimals": 2, "maxLeverage": 20},
        {"name": "HYPE", "szDecimals": 2, "maxLeverage": 10},
    ]
    ctxs = [
        {
            "funding": "0.0001",
            "openInterest": "1000",
            "prevDayPx": "90000",
            "dayNtlVlm": "1000000",
            "oraclePx": "95000",
            "markPx": "95000",
        },
        {
            "funding": "0.0001",
            "openInterest": "1000",
            "prevDayPx": "3000",
            "dayNtlVlm": "500000",
            "oraclePx": "3100",
            "markPx": "3100",
        },
        {
            "funding": "0.0001",
            "openInterest": "1000",
            "prevDayPx": "180",
            "dayNtlVlm": "300000",
            "oraclePx": "190",
            "markPx": "190",
        },
        {
            "funding": "0.0001",
            "openInterest": "1000",
            "prevDayPx": "20",
            "dayNtlVlm": "200000",
            "oraclePx": "22",
            "markPx": "22",
        },
    ]
    ms = {
        "accountValue": "5000000.0",
        "totalMarginUsed": "0",
        "totalNtlPos": "0",
        "totalRawUsd": "5000000.0",
    }

    def handle_info(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        ep = body["type"]
        if ep == "meta":
            return httpx.Response(200, json={"universe": meta_universe, "marginTables": []})
        if ep == "metaAndAssetCtxs":
            return httpx.Response(200, json=[{"universe": meta_universe, "marginTables": []}, ctxs])
        if ep == "userRole":
            return httpx.Response(200, json={"role": "user"})
        if ep == "subAccounts":
            return httpx.Response(200, json=[])
        if ep == "userAbstraction":
            return httpx.Response(200, json="disabled")
        if ep == "clearinghouseState":
            return httpx.Response(
                200,
                json={
                    "assetPositions": [],
                    "crossMaintenanceMarginUsed": "0",
                    "crossMarginSummary": ms,
                    "marginSummary": ms,
                    "time": now_ms,
                    "withdrawable": "5000000.0",
                },
            )
        if ep == "portfolio":
            return httpx.Response(200, json=pf_payload)
        if ep == "userFillsByTime":
            st = int(body["startTime"])
            return httpx.Response(200, json=[f for f in fills if f["time"] >= st])
        if ep == "userFunding":
            return httpx.Response(200, json=[])
        if ep == "candleSnapshot":
            req = body["req"]
            c = req["coin"]
            iv = req["interval"]
            px = {"BTC": "95000", "ETH": "3100", "SOL": "190", "HYPE": "22"}[c]
            t_bar = start_t
            return httpx.Response(
                200,
                json=[
                    {
                        "t": t_bar,
                        "T": t_bar + 300_000,
                        "s": c,
                        "i": iv,
                        "o": px,
                        "h": px,
                        "l": px,
                        "c": px,
                        "v": "10",
                        "n": 1,
                    }
                ],
            )
        raise AssertionError(f"Unexpected endpoint {ep}")

    with respx.mock() as mock_router:
        mock_router.get(LB_URL).mock(return_value=httpx.Response(200, json=lb_payload))
        mock_router.post(INFO_URL).mock(side_effect=handle_info)

        res = runner.invoke(
            app,
            [
                "bootstrap",
                "--config",
                str(cfg_path),
                "--state-dir",
                str(state_dir),
                "--data-dir",
                str(data_dir),
                "--seed-file",
                str(seed_file),
                "--blocklist-file",
                str(blocklist_file),
                "--push-remote",
                str(remote),
            ],
        )
        assert res.exit_code == 0, res.output
        payload = json.loads(res.stdout)
        assert payload["completed"] is True
        assert payload["activeWhitelistCount"] == 1
        assert len(payload["pushed"]) == 2
        assert (state_dir / "bootstrap" / "done.json").is_file()
        validate_site_data_directory(data_dir, schemas_dir=SCHEMAS_DIR)

        # CLI health passes immediately after bootstrap
        h_res = runner.invoke(
            app,
            ["health", "--hours", "24", "--state-dir", str(state_dir), "--data-dir", str(data_dir)],
        )
        assert h_res.exit_code == 0, h_res.output
        h_json = json.loads(h_res.stdout)
        assert h_json["healthy"] is True

        # Mutual exclusion check on --hours and --days
        bad_h = runner.invoke(app, ["health", "--hours", "24", "--days", "7"])
        assert bad_h.exit_code == 2


def test_verify_live_positions_excludes_stale_and_tolerates_intra_snapshot_suffix(
    tmp_path: Path,
) -> None:
    cfg = load_config()
    data_dir = tmp_path / "data"
    shutil.copytree(SITE_DATA_FIXTURE, data_dir)

    latest = Snapshot.model_validate(read_json_file(data_dir / "snapshots" / "latest.json"))
    t_snap = latest.keyframe.t
    # Mark addrId=1 stale in latest.json; addrId=1 should be excluded from sampling.
    latest_stale = latest.model_copy(update={"stale": [(t_snap, [1])]})
    write_json_file_atomic(
        data_dir / "snapshots" / "latest.json",
        latest_stale.model_dump(mode="json", exclude_none=True),
        compress=False,
    )

    wl = Whitelist.model_validate(read_json_file(data_dir / "whitelist.json"))
    by_id = {a.id: a.address.lower() for a in wl.addresses}

    def handle_info(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        ep = body["type"]
        if ep == "meta":
            return httpx.Response(
                200,
                json={
                    "universe": [
                        {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
                        {"name": "ETH", "szDecimals": 4, "maxLeverage": 50},
                    ],
                    "marginTables": [],
                },
            )
        user = body["user"].lower()
        if ep == "clearinghouseState":
            if user == by_id[0]:
                # addrId=0 published 8.0 BTC + post-poll +1.5 BTC = 9.5 BTC
                pos = [("BTC", "9.5", "95000.0"), ("ETH", "100.0", "3080.0")]
            elif user == by_id[1]:
                # Mismatch on stale addrId=1 -> excluded from sampling!
                pos = [("BTC", "999.0", "94000.0"), ("ETH", "60.0", "3120.0")]
            elif user == by_id[2]:
                pos = [("BTC", "-2.0", "95000.0")]
            else:
                pos = [("BTC", "5.0", "95000.0"), ("ETH", "85.0", "2972.26")]
            ms = {
                "accountValue": "5000000",
                "totalMarginUsed": "1000",
                "totalNtlPos": "100000",
                "totalRawUsd": "5000000",
            }
            return httpx.Response(
                200,
                json={
                    "assetPositions": [
                        {
                            "type": "oneWay",
                            "position": {
                                "coin": c,
                                "cumFunding": {
                                    "allTime": "0",
                                    "sinceOpen": "0",
                                    "sinceChange": "0",
                                },
                                "entryPx": epx,
                                "leverage": {"type": "cross", "value": 5},
                                "marginUsed": "100",
                                "maxLeverage": 50,
                                "positionValue": "10000",
                                "returnOnEquity": "0.1",
                                "szi": szi,
                                "unrealizedPnl": "0",
                            },
                        }
                        for c, szi, epx in pos
                    ],
                    "crossMaintenanceMarginUsed": "100",
                    "crossMarginSummary": ms,
                    "marginSummary": ms,
                    "time": t_snap + 120_000,
                    "withdrawable": "4000000",
                },
            )
        if ep == "userFillsByTime":
            if user == by_id[0]:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "coin": "BTC",
                            "px": "95000.0",
                            "sz": "1.0",
                            "side": "B",
                            "time": t_snap + 5_000,  # Intra-snap fill ALREADY reflected (7->8)
                            "startPosition": "7.0",
                            "dir": "Open Long",
                            "closedPnl": "0.0",
                            "hash": "0x" + "1" * 64,
                            "oid": 1,
                            "crossed": False,
                            "fee": "1.0",
                            "tid": 1,
                        },
                        {
                            "coin": "BTC",
                            "px": "96000.0",
                            "sz": "1.5",
                            "side": "B",
                            "time": t_snap + 30_000,  # Post-poll fill (8->9.5)
                            "startPosition": "8.0",
                            "dir": "Open Long",
                            "closedPnl": "0.0",
                            "hash": "0x" + "2" * 64,
                            "oid": 2,
                            "crossed": False,
                            "fee": "1.0",
                            "tid": 2,
                        },
                    ],
                )
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected endpoint {ep}")

    with respx.mock() as mock_router:
        mock_router.post(INFO_URL).mock(side_effect=handle_info)
        rep = asyncio.run(verify_live_positions(data_dir, cfg, sample=10, seed=42))
        assert rep.passed is True
        assert by_id[1] not in [a.lower() for a in rep.sampled_addresses]
        assert rep.sampled_count == 3
        assert rep.mismatches == []


def test_evaluate_health_catches_initial_launch_gap_and_ignores_hourly_compaction_in_max_gap(
    tmp_path: Path,
) -> None:
    now_ms = utc_date_to_start_ms("2026-11-16") + 12 * HOUR_MS

    # Case A: 2h initial gap between launchedAt and first live snapshot
    s_init = tmp_path / "init" / "state"
    d_init = tmp_path / "init" / "data"
    launched_a = now_ms - 4 * HOUR_MS
    write_json_file_atomic(
        d_init / "manifest.json",
        {
            "schemaVersion": 1,
            "generatedAt": now_ms,
            "latestSnapshotAt": now_ms,
            "launchedAt": launched_a,
            "whitelistVersion": "2026-11-16",
            "criteriaVersion": 1,
            "coins": [
                {"coin": "BTC", "pinned": True, "trackedFrom": launched_a, "trackedTo": None}
            ],
            "files": {
                "snapshotDays": ["2026-11-16"],
                "snapshotMonths": [],
                "fills": {},
                "candles": {},
            },
            "storage": {"totalBytesOnDisk": 100, "softLimitBytes": 800_000_000},
            "pipeline": {"fastLastOk": now_ms, "dailyLastOk": now_ms, "warnings": []},
        },
        compress=False,
    )
    store_a = StateStore(s_init, owner="fast")
    ts_a = [now_ms - 2 * HOUR_MS + i * 10 * MIN_MS for i in range(13)]
    for t in ts_a:
        store_a.append_run_log(
            "fast",
            {"job": "snapshot", "status": "ok", "ts": t, "rateLimitStats": {"totalWeight": 500}},
        )
    snap_a = Snapshot(
        schemaVersion=1,
        coins=["BTC"],
        reconstructed=False,
        tracked=[(0, ts_a[0], None)],
        keyframe=SnapshotKeyframe(t=ts_a[0], pos=[]),
        posChanges=[],
        series=SnapshotSeries(
            t=ts_a,
            mark=[[95_000.0] * len(ts_a)],
            av=SnapshotAvSeries(addrIds=[0], values=[[2_000_000.0] * len(ts_a)]),
        ),
    )
    write_json_file_atomic(
        d_init / "snapshots" / "days" / "2026-11-16.json",
        snap_a.model_dump(mode="json", exclude_none=True),
        compress=False,
    )
    rep_a = evaluate_health(s_init, d_init, hours=24.0, now_ms=now_ms)
    assert rep_a.healthy is False
    assert len(rep_a.gaps) >= 1
    assert rep_a.max_gap_minutes_observed >= 120.0

    # Case B: Healthy 1h compacted month points must NOT inflate maxGapMinutesObserved
    s_cmp = tmp_path / "cmp" / "state"
    d_cmp = tmp_path / "cmp" / "data"
    launched_b = now_ms - 6 * HOUR_MS
    write_json_file_atomic(
        d_cmp / "manifest.json",
        {
            "schemaVersion": 1,
            "generatedAt": now_ms,
            "latestSnapshotAt": now_ms,
            "launchedAt": launched_b,
            "whitelistVersion": "2026-11-16",
            "criteriaVersion": 1,
            "coins": [
                {"coin": "BTC", "pinned": True, "trackedFrom": launched_b, "trackedTo": None}
            ],
            "files": {
                "snapshotDays": ["2026-11-16"],
                "snapshotMonths": ["2026-11"],
                "fills": {},
                "candles": {},
            },
            "storage": {"totalBytesOnDisk": 100, "softLimitBytes": 800_000_000},
            "pipeline": {"fastLastOk": now_ms, "dailyLastOk": now_ms, "warnings": []},
        },
        compress=False,
    )
    store_b = StateStore(s_cmp, owner="fast")
    for idx in range(37):
        store_b.append_run_log(
            "fast",
            {
                "job": "snapshot",
                "status": "ok",
                "ts": launched_b + idx * 10 * MIN_MS,
                "rateLimitStats": {"totalWeight": 500},
            },
        )
    ts_month = [launched_b + i * HOUR_MS for i in range(4)]  # -6h, -5h, -4h, -3h
    snap_month = Snapshot(
        schemaVersion=1,
        coins=["BTC"],
        reconstructed=False,
        tracked=[(0, launched_b, None)],
        keyframe=SnapshotKeyframe(t=ts_month[0], pos=[]),
        posChanges=[],
        series=SnapshotSeries(
            t=ts_month,
            mark=[[95_000.0] * len(ts_month)],
            av=SnapshotAvSeries(addrIds=[0], values=[[2_000_000.0] * len(ts_month)]),
        ),
    )
    write_json_file_atomic(
        d_cmp / "snapshots" / "months" / "2026-11.json.gz",
        snap_month.model_dump(mode="json", exclude_none=True),
        compress=True,
    )
    ts_day = [now_ms - 2 * HOUR_MS + i * 10 * MIN_MS for i in range(13)]
    snap_day = Snapshot(
        schemaVersion=1,
        coins=["BTC"],
        reconstructed=False,
        tracked=[(0, ts_day[0], None)],
        keyframe=SnapshotKeyframe(t=ts_day[0], pos=[]),
        posChanges=[],
        series=SnapshotSeries(
            t=ts_day,
            mark=[[95_000.0] * len(ts_day)],
            av=SnapshotAvSeries(addrIds=[0], values=[[2_000_000.0] * len(ts_day)]),
        ),
    )
    write_json_file_atomic(
        d_cmp / "snapshots" / "days" / "2026-11-16.json",
        snap_day.model_dump(mode="json", exclude_none=True),
        compress=False,
    )
    rep_b = evaluate_health(s_cmp, d_cmp, hours=24.0, now_ms=now_ms)
    assert rep_b.healthy is True
    assert rep_b.gaps == []
    assert rep_b.max_gap_minutes_observed == 10.0

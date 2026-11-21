# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline tests for Phase 4.1 candle archiving and 10-minute fast snapshot collection."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from hyperpod.client.info_api import DEFAULT_INFO_URL
from hyperpod.config import load_config
from hyperpod.models import (
    BackfillCandles,
    Manifest,
    SingleIntervalCandles,
    Snapshot,
    WhitelistMetrics,
)
from hyperpod.models.manifest import CoinTrack
from hyperpod.models.snapshot import SnapshotAvSeries, SnapshotKeyframe, SnapshotSeries
from hyperpod.scoring.whitelist import WhitelistMemberRecord, WhitelistStateRecord
from hyperpod.snapshot.candles import resolve_effective_tracked_coins, run_candles
from hyperpod.snapshot.collect import SnapshotAbortError, run_snapshot
from hyperpod.storage.formats import (
    read_json_file,
    replay_snapshot_positions_at,
    write_json_file_atomic,
)
from hyperpod.storage.site_writer import validate_site_data_directory
from hyperpod.storage.state_store import StateStore
from hyperpod.util.time import DAY_MS, HOUR_MS, MIN_MS, utc_date_to_start_ms

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
ADDR_A = "0x1111111111111111111111111111111111111111"
ADDR_B = "0x2222222222222222222222222222222222222222"
ADDR_C = "0x3333333333333333333333333333333333333333"
ADDR_D = "0x4444444444444444444444444444444444444444"
ADDR_E = "0x5555555555555555555555555555555555555555"


def _sample_metrics(av: float = 2_000_000.0) -> WhitelistMetrics:
    return WhitelistMetrics(
        accountValue=av,
        equitySource="perp",
        pnlAllTime=500_000.0,
        roiAllTime=0.35,
        pnlMonth=80_000.0,
        maxDrawdown=0.15,
        profitFactor=2.2,
        profitFactorRobust=1.8,
        winRate=0.58,
        nTrades=60,
        historyDays=120.0,
        fillsPerDay=15.0,
        medianHoldMinutes=400.0,
        makerRatio=0.35,
        turnoverMonth=4.5,
        fundingShare=0.03,
        topWinShare=0.20,
        hedgeRatio=0.0,
    )


def _seed_daily_metadata(store: StateStore, state: WhitelistStateRecord, ts_ms: int) -> None:
    store.save_whitelist_state(state)
    from hyperpod.models import CandidateRow, CandidatesFile, CriteriaFile, Whitelist
    from hyperpod.scoring.whitelist import update_criteria_state

    cfg = load_config()
    rows = [
        CandidateRow(
            idx=idx,
            passed=True,
            rejectReasons=[],
            fillsPerDay=15.0,
            makerRatio=0.35,
            medianHoldMinutes=400.0,
            maxDrawdown=0.15,
            profitFactorRobust=1.8,
            turnoverMonth=4.5,
            nTrades=60,
            historyDays=120.0,
            fundingShare=0.03,
            hedgeRatio=0.0,
            topWinShare=0.20,
        )
        for idx, _ in enumerate(state.addresses)
    ]
    criteria = update_criteria_state(cfg, None, eval_ts_ms=ts_ms, candidates=rows)
    wl = Whitelist(
        schemaVersion=1,
        version="2026-11-15",
        addresses=[m.to_public() for m in sorted(state.addresses.values(), key=lambda x: x.id)],
    )
    cands = CandidatesFile(
        schemaVersion=1,
        generatedAt=ts_ms,
        criteriaVersion=criteria.current.version,
        candidates=rows,
    )
    CriteriaFile.model_validate(criteria.model_dump())
    store.save_daily_outputs(whitelist=wl, criteria=criteria, candidates=cands)


def _meta_and_ctxs_payload() -> list[Any]:
    meta = {
        "universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
            {"name": "ETH", "szDecimals": 4, "maxLeverage": 50},
            {"name": "SOL", "szDecimals": 2, "maxLeverage": 20},
            {"name": "HYPE", "szDecimals": 2, "maxLeverage": 10},
            {"name": "DOGE", "szDecimals": 0, "maxLeverage": 20},
        ],
        "marginTables": [],
    }
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
        {
            "funding": "0.0001",
            "openInterest": "1000",
            "prevDayPx": "0.3",
            "dayNtlVlm": "100000",
            "oraclePx": "0.35",
            "markPx": "0.35",
        },
    ]
    return [meta, ctxs]


def _ch_payload(
    av: str,
    positions: list[tuple[str, str, str]],
) -> dict[str, Any]:
    asset_pos = [
        {
            "type": "oneWay",
            "position": {
                "coin": coin,
                "cumFunding": {"allTime": "0", "sinceOpen": "0", "sinceChange": "0"},
                "entryPx": epx,
                "leverage": {"type": "cross", "value": 5},
                "marginUsed": "1000",
                "maxLeverage": 50,
                "positionValue": "100000",
                "returnOnEquity": "0.1",
                "szi": szi,
                "unrealizedPnl": "500",
            },
        }
        for coin, szi, epx in positions
        if float(szi) != 0.0
    ]
    ms = {
        "accountValue": av,
        "totalMarginUsed": "1000",
        "totalNtlPos": "100000",
        "totalRawUsd": av,
    }
    return {
        "assetPositions": asset_pos,
        "crossMaintenanceMarginUsed": "500",
        "crossMarginSummary": ms,
        "marginSummary": ms,
        "time": 1_763_164_800_000,
        "withdrawable": av,
    }


def _spot_payload(usdc_total: str) -> dict[str, Any]:
    return {
        "balances": [
            {
                "coin": "USDC",
                "token": 0,
                "total": usdc_total,
                "hold": "0.0",
                "entryNtl": "0.0",
            }
        ],
        "tokenToAvailableAfterMaintenance": [[0, usdc_total]],
    }


def test_resolve_effective_tracked_coins_next_utc_day_boundary() -> None:
    cfg = load_config()
    day1 = utc_date_to_start_ms("2026-11-15")
    day2 = day1 + DAY_MS
    state = WhitelistStateRecord(
        coins=[
            CoinTrack(coin="BTC", pinned=True, trackedFrom=day1 - DAY_MS, trackedTo=None),
            CoinTrack(coin="ETH", pinned=True, trackedFrom=day1 - DAY_MS, trackedTo=None),
            CoinTrack(coin="SOL", pinned=True, trackedFrom=day1 - DAY_MS, trackedTo=None),
            CoinTrack(coin="HYPE", pinned=True, trackedFrom=day1 - DAY_MS, trackedTo=None),
            CoinTrack(coin="DOGE", pinned=False, trackedFrom=day2, trackedTo=None),
            CoinTrack(coin="WIF", pinned=False, trackedFrom=day1 - DAY_MS, trackedTo=day2),
        ]
    )
    day1_coins = resolve_effective_tracked_coins(state, cfg, day1 + 12 * HOUR_MS)
    assert day1_coins == ["BTC", "ETH", "SOL", "HYPE", "WIF"]

    day2_coins = resolve_effective_tracked_coins(state, cfg, day2 + 60_000)
    assert day2_coins == ["BTC", "ETH", "SOL", "HYPE", "DOGE"]


def test_run_candles_archives_5m_backfill_and_overview_4h(tmp_path: Path) -> None:
    cfg = load_config()
    now_ms = utc_date_to_start_ms("2026-11-15") + 12 * HOUR_MS
    day_start = utc_date_to_start_ms("2026-11-15")
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"

    store = StateStore(state_dir, owner="daily")
    store.save_whitelist_state(
        WhitelistStateRecord(
            coins=[
                CoinTrack(
                    coin="BTC", pinned=True, trackedFrom=day_start - 10 * DAY_MS, trackedTo=None
                ),
                CoinTrack(
                    coin="DOGE", pinned=False, trackedFrom=day_start + DAY_MS, trackedTo=None
                ),
            ],
            coinBackfillQueue=["DOGE"],
        )
    )

    candle_calls: list[tuple[str, str, int, int]] = []

    def handle_info(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["type"] == "candleSnapshot"
        req = body["req"]
        coin = req["coin"]
        iv = req["interval"]
        s_t = int(req["startTime"])
        e_t = int(req["endTime"])
        candle_calls.append((coin, iv, s_t, e_t))

        px = "95000.0" if coin == "BTC" else "0.35"
        if iv == "5m":
            t0 = day_start - HOUR_MS
            return httpx.Response(
                200,
                json=[
                    {
                        "t": t0,
                        "T": t0 + 299_999,
                        "s": coin,
                        "i": "5m",
                        "o": px,
                        "h": px,
                        "l": px,
                        "c": px,
                        "v": "10.0",
                        "n": 5,
                    },
                    {
                        "t": day_start + 300_000,
                        "T": day_start + 599_999,
                        "s": coin,
                        "i": "5m",
                        "o": px,
                        "h": px,
                        "l": px,
                        "c": px,
                        "v": "10.0",
                        "n": 5,
                    },
                ],
            )
        if iv == "15m":
            t0 = now_ms - 25 * DAY_MS
            return httpx.Response(
                200,
                json=[
                    {
                        "t": t0,
                        "T": t0 + 899_999,
                        "s": coin,
                        "i": "15m",
                        "o": px,
                        "h": px,
                        "l": px,
                        "c": px,
                        "v": "20.0",
                        "n": 8,
                    }
                ],
            )
        if iv == "1h":
            t0 = now_ms - 100 * DAY_MS
            return httpx.Response(
                200,
                json=[
                    {
                        "t": t0,
                        "T": t0 + 3_599_999,
                        "s": coin,
                        "i": "1h",
                        "o": px,
                        "h": px,
                        "l": px,
                        "c": px,
                        "v": "30.0",
                        "n": 10,
                    }
                ],
            )
        t0 = now_ms - 300 * DAY_MS
        return httpx.Response(
            200,
            json=[
                {
                    "t": t0,
                    "T": t0 + 14_399_999,
                    "s": coin,
                    "i": "4h",
                    "o": px,
                    "h": px,
                    "l": px,
                    "c": px,
                    "v": "40.0",
                    "n": 15,
                }
            ],
        )

    with respx.mock(assert_all_called=True) as mock_router:
        mock_router.post(DEFAULT_INFO_URL).mock(side_effect=handle_info)
        res = asyncio.run(
            run_candles(
                cfg,
                store,
                data_dir=data_dir,
                schemas_dir=SCHEMAS_DIR,
                now_ms=now_ms,
            )
        )

    assert set(res.coins_processed) == {"BTC", "DOGE"}
    assert res.errors == 0
    for coin in ("BTC", "DOGE"):
        m5 = SingleIntervalCandles.model_validate(
            read_json_file(data_dir / "candles" / coin / "5m" / "2026-11.json.gz")
        )
        # include_today_5m=False excludes day_start + 300_000; retains yesterday's candle only.
        assert [row[0] for row in m5.c] == [day_start - HOUR_MS]

        bf = BackfillCandles.model_validate(
            read_json_file(data_dir / "candles" / coin / "backfill_15m_1h.json.gz")
        )
        assert len(bf.c15m) == 1
        assert len(bf.c1h) == 1

        ov = SingleIntervalCandles.model_validate(
            read_json_file(data_dir / "candles" / coin / "overview_4h.json.gz")
        )
        assert len(ov.c) == 1


def test_fast_snapshot_diff_conditional_fills_and_rollover(tmp_path: Path) -> None:
    cfg = load_config()
    day1_start = utc_date_to_start_ms("2026-11-15")
    t1 = day1_start + 10 * MIN_MS
    t2 = day1_start + 20 * MIN_MS
    t_day2 = day1_start + DAY_MS + 10 * MIN_MS

    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    daily_store = StateStore(state_dir, owner="daily")
    fast_store = StateStore(state_dir, owner="fast")

    wl_state = WhitelistStateRecord(
        nextAddrId=2,
        launchedAt=t1,
        coins=[
            CoinTrack(coin="BTC", pinned=True, trackedFrom=day1_start, trackedTo=None),
            CoinTrack(coin="ETH", pinned=True, trackedFrom=day1_start, trackedTo=None),
        ],
        addresses={
            ADDR_A: WhitelistMemberRecord(
                id=0,
                address=ADDR_A,
                coverageStart=day1_start - DAY_MS,
                score=0.9,
                metrics=_sample_metrics(2_000_000.0),
                membership=[(day1_start - DAY_MS, None, 1)],
                abstraction="disabled",
            ),
            ADDR_B: WhitelistMemberRecord(
                id=1,
                address=ADDR_B,
                coverageStart=day1_start - DAY_MS,
                score=0.8,
                metrics=_sample_metrics(3_000_000.0),
                membership=[(day1_start - DAY_MS, None, 1)],
                abstraction="unifiedAccount",
            ),
        },
    )
    _seed_daily_metadata(daily_store, wl_state, t1)

    phase = {"step": 1}
    fill_calls: list[str] = []
    spot_calls: list[str] = []

    def handle_info(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        ep = body["type"]
        if ep == "metaAndAssetCtxs":
            return httpx.Response(200, json=_meta_and_ctxs_payload())
        user = body["user"].lower()
        if ep == "clearinghouseState":
            if user == ADDR_A:
                szi = "1.5" if phase["step"] == 1 else "2.5"
                return httpx.Response(200, json=_ch_payload("2000000.0", [("BTC", szi, "94000.0")]))
            if user == ADDR_B:
                return httpx.Response(
                    200, json=_ch_payload("1000000.0", [("ETH", "-10.0", "3150.0")])
                )
            return httpx.Response(200, json=_ch_payload("4000000.0", [("BTC", "3.0", "95000.0")]))
        if ep == "spotClearinghouseState":
            spot_calls.append(user)
            return httpx.Response(200, json=_spot_payload("3500000.0"))
        if ep == "userFillsByTime":
            fill_calls.append(user)
            if user == ADDR_A and phase["step"] == 2:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "coin": "BTC",
                            "px": "95000.0",
                            "sz": "1.0",
                            "side": "B",
                            "time": t2 - 60_000,
                            "startPosition": "1.5",
                            "dir": "Open Long",
                            "closedPnl": "0.0",
                            "hash": "0x" + "a" * 64,
                            "oid": 101,
                            "crossed": False,
                            "fee": "5.0",
                            "tid": 101,
                        }
                    ],
                )
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected endpoint {ep}")

    with respx.mock(assert_all_called=True) as mock_router:
        mock_router.post(DEFAULT_INFO_URL).mock(side_effect=handle_info)

        # Step 1: Initial tick at t1 (A and B have positions -> poll fills; only B calls spot).
        res1 = asyncio.run(
            run_snapshot(cfg, fast_store, data_dir=data_dir, schemas_dir=SCHEMAS_DIR, now_ms=t1)
        )
        assert res1.active_count == 2
        assert res1.fills_polled_count == 2
        assert set(spot_calls) == {ADDR_B}
        assert ADDR_A not in spot_calls

        # Step 2: At t2, ADDR_A increases BTC 1.5 -> 2.5, ADDR_B unchanged, ADDR_C enters mid-day!
        phase["step"] = 2
        fill_calls.clear()
        wl_state.nextAddrId = 3
        wl_state.addresses[ADDR_C] = WhitelistMemberRecord(
            id=2,
            address=ADDR_C,
            coverageStart=t2,
            score=0.75,
            metrics=_sample_metrics(4_000_000.0),
            membership=[(t2, None, 1)],
            abstraction="disabled",
        )
        _seed_daily_metadata(daily_store, wl_state, t2)

        res2 = asyncio.run(
            run_snapshot(cfg, fast_store, data_dir=data_dir, schemas_dir=SCHEMAS_DIR, now_ms=t2)
        )
        assert res2.active_count == 3
        # Only ADDR_A (changed) and ADDR_C (newly entered non-zero) poll fills; ADDR_B skipped!
        assert set(fill_calls) == {ADDR_A, ADDR_C}
        assert res2.fills_rows_upserted == 1
        assert res2.pos_changes_emitted == 2  # A BTC 1.5->2.5, C initial BTC 3.0

        day1_snap = Snapshot.model_validate(
            read_json_file(data_dir / "snapshots" / "days" / "2026-11-15.json")
        )
        replayed_t2 = replay_snapshot_positions_at(day1_snap, t2)
        assert replayed_t2[(0, "BTC")][0] == pytest.approx(2.5)
        assert replayed_t2[(1, "ETH")][0] == pytest.approx(-10.0)
        assert replayed_t2[(2, "BTC")][0] == pytest.approx(3.0)

        # Step 3: ADDR_B exits before day2 rollover; verify day1 tracked closes at t2!
        phase["step"] = 3
        fill_calls.clear()
        wl_state.addresses[ADDR_B].membership = [(day1_start - DAY_MS, t_day2 - 60_000, 1)]
        _seed_daily_metadata(daily_store, wl_state, t_day2)

        res3 = asyncio.run(
            run_snapshot(cfg, fast_store, data_dir=data_dir, schemas_dir=SCHEMAS_DIR, now_ms=t_day2)
        )
        assert res3.active_count == 2
        day1_after = Snapshot.model_validate(
            read_json_file(data_dir / "snapshots" / "days" / "2026-11-15.json")
        )
        b_intervals = [row for row in day1_after.tracked if row[0] == 1]
        assert b_intervals == [(1, t1, t2)]
        manifest = validate_site_data_directory(data_dir, schemas_dir=SCHEMAS_DIR)
        assert manifest.files.snapshotDays == ["2026-11-15", "2026-11-16"]


def test_fast_snapshot_stale_fallback_3_streak_warning_and_20pct_abort(tmp_path: Path) -> None:
    cfg = load_config()
    day1_start = utc_date_to_start_ms("2026-11-15")
    t_base = day1_start + HOUR_MS

    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    daily_store = StateStore(state_dir, owner="daily")
    fast_store = StateStore(state_dir, owner="fast")

    # 5 active addresses: 1 failing = 20% (stale), 2 failing = 40% (> 20%, ABORT).
    addrs = [ADDR_A, ADDR_B, ADDR_C, ADDR_D, ADDR_E]
    wl_state = WhitelistStateRecord(
        nextAddrId=5,
        launchedAt=t_base,
        coins=[CoinTrack(coin="BTC", pinned=True, trackedFrom=day1_start, trackedTo=None)],
        addresses={
            a: WhitelistMemberRecord(
                id=idx,
                address=a,
                coverageStart=day1_start - DAY_MS,
                score=0.8,
                metrics=_sample_metrics(),
                membership=[(day1_start - DAY_MS, None, 1)],
                abstraction="disabled",
            )
            for idx, a in enumerate(addrs)
        },
    )
    _seed_daily_metadata(daily_store, wl_state, t_base)

    fail_set: set[str] = set()

    def handle_info(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        ep = body["type"]
        if ep == "metaAndAssetCtxs":
            return httpx.Response(200, json=_meta_and_ctxs_payload())
        user = body["user"].lower()
        if ep == "clearinghouseState":
            if user in fail_set:
                return httpx.Response(400, json={"error": "simulated failure"})
            return httpx.Response(200, json=_ch_payload("2000000.0", [("BTC", "1.0", "95000.0")]))
        if ep == "userFillsByTime":
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected endpoint {ep}")

    with respx.mock() as mock_router:
        mock_router.post(DEFAULT_INFO_URL).mock(side_effect=handle_info)

        # Tick 0: all succeed
        asyncio.run(
            run_snapshot(cfg, fast_store, data_dir=data_dir, schemas_dir=SCHEMAS_DIR, now_ms=t_base)
        )

        # Ticks 1..3: ADDR_E (id=4) fails 3 consecutive times -> stale & streak warning!
        fail_set.add(ADDR_E)
        for k in (1, 2, 3):
            res = asyncio.run(
                run_snapshot(
                    cfg,
                    fast_store,
                    data_dir=data_dir,
                    schemas_dir=SCHEMAS_DIR,
                    now_ms=t_base + k * 10 * MIN_MS,
                )
            )
            assert res.stale_ids == [4]
            if k == 3:
                assert any("stale for 3 consecutive snapshots" in w for w in res.warnings)
                manifest = Manifest.model_validate(read_json_file(data_dir / "manifest.json"))
                assert any(
                    "stale for 3 consecutive snapshots" in w for w in manifest.pipeline.warnings
                )

        # Latest day snapshot preserves id=4 carried position while marking av=None and stale entry.
        day_snap = Snapshot.model_validate(
            read_json_file(data_dir / "snapshots" / "days" / "2026-11-15.json")
        )
        assert day_snap.stale is not None
        assert len(day_snap.stale) == 3
        av_idx_4 = day_snap.series.av.addrIds.index(4)
        assert day_snap.series.av.values[av_idx_4][-1] is None
        assert (4, "BTC") in replay_snapshot_positions_at(day_snap, t_base + 30 * MIN_MS)

        # Tick 4: 2/5 (40% > 20%) fail -> aborts WITHOUT mutating state or latest.json!
        before_latest = (data_dir / "snapshots" / "latest.json").read_bytes()
        before_state = (state_dir / "fast" / "last_snapshot_state.json.gz").read_bytes()
        fail_set.add(ADDR_D)
        with pytest.raises(SnapshotAbortError):
            asyncio.run(
                run_snapshot(
                    cfg,
                    fast_store,
                    data_dir=data_dir,
                    schemas_dir=SCHEMAS_DIR,
                    now_ms=t_base + 40 * MIN_MS,
                )
            )
        assert (data_dir / "snapshots" / "latest.json").read_bytes() == before_latest
        assert (state_dir / "fast" / "last_snapshot_state.json.gz").read_bytes() == before_state
        runs_lines = (state_dir / "fast" / "runs.jsonl").read_text("utf-8").splitlines()
        assert json.loads(runs_lines[-1])["status"] == "aborted"


def test_fast_snapshot_same_day_exit_reentry_flat_and_launch_day_bridging(
    tmp_path: Path,
) -> None:
    cfg = load_config()
    day1_start = utc_date_to_start_ms("2026-11-15")
    t1 = day1_start + 10 * MIN_MS
    t2 = day1_start + 20 * MIN_MS
    t3 = day1_start + 30 * MIN_MS

    # Part A: Same-day exit with open position then re-entry flat on the same day -> no phantom!
    state_dir = tmp_path / "state_a"
    data_dir = tmp_path / "data_a"
    daily_store = StateStore(state_dir, owner="daily")
    fast_store = StateStore(state_dir, owner="fast")

    wl_state = WhitelistStateRecord(
        nextAddrId=2,
        launchedAt=t1,
        coins=[CoinTrack(coin="BTC", pinned=True, trackedFrom=day1_start, trackedTo=None)],
        addresses={
            ADDR_A: WhitelistMemberRecord(
                id=0,
                address=ADDR_A,
                coverageStart=day1_start - DAY_MS,
                score=0.9,
                metrics=_sample_metrics(),
                membership=[(day1_start - DAY_MS, None, 1)],
                abstraction="disabled",
            ),
            ADDR_B: WhitelistMemberRecord(
                id=1,
                address=ADDR_B,
                coverageStart=day1_start - DAY_MS,
                score=0.85,
                metrics=_sample_metrics(),
                membership=[(day1_start - DAY_MS, None, 1)],
                abstraction="disabled",
            ),
        },
    )
    _seed_daily_metadata(daily_store, wl_state, t1)

    phase = {"step": 1}

    def handle_info_a(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        ep = body["type"]
        if ep == "metaAndAssetCtxs":
            return httpx.Response(200, json=_meta_and_ctxs_payload())
        user = body["user"].lower()
        if ep == "clearinghouseState":
            if user == ADDR_A:
                pos = [("BTC", "1.5", "95000.0")] if phase["step"] == 1 else []
                return httpx.Response(200, json=_ch_payload("2000000.0", pos))
            return httpx.Response(200, json=_ch_payload("2000000.0", []))
        if ep == "userFillsByTime":
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected endpoint {ep}")

    with respx.mock() as mock_router:
        mock_router.post(DEFAULT_INFO_URL).mock(side_effect=handle_info_a)

        # t1: ADDR_A holds 1.5 BTC
        asyncio.run(
            run_snapshot(cfg, fast_store, data_dir=data_dir, schemas_dir=SCHEMAS_DIR, now_ms=t1)
        )

        # t2: ADDR_A exits whitelist
        phase["step"] = 2
        wl_state.addresses[ADDR_A].membership = [(day1_start - DAY_MS, t2 - 1_000, 1)]
        _seed_daily_metadata(daily_store, wl_state, t2)
        asyncio.run(
            run_snapshot(cfg, fast_store, data_dir=data_dir, schemas_dir=SCHEMAS_DIR, now_ms=t2)
        )

        # t3: ADDR_A re-enters whitelist on SAME UTC day with flat positions (0.0 BTC)
        phase["step"] = 3
        wl_state.addresses[ADDR_A].membership = [
            (day1_start - DAY_MS, t2 - 1_000, 1),
            (t3 - 1_000, None, 1),
        ]
        _seed_daily_metadata(daily_store, wl_state, t3)
        asyncio.run(
            run_snapshot(cfg, fast_store, data_dir=data_dir, schemas_dir=SCHEMAS_DIR, now_ms=t3)
        )

    day_snap = Snapshot.model_validate(
        read_json_file(data_dir / "snapshots" / "days" / "2026-11-15.json")
    )
    assert (0, "BTC") not in replay_snapshot_positions_at(day_snap, t3)

    # Part B: Launch-day bridging uses full existing posChanges past series.t[-1]
    state_b = tmp_path / "state_b"
    data_b = tmp_path / "data_b"
    d_store_b = StateStore(state_b, owner="daily")
    f_store_b = StateStore(state_b, owner="fast")
    t_14_00 = day1_start + 14 * HOUR_MS
    t_14_15 = t_14_00 + 15 * MIN_MS
    t_14_20 = t_14_00 + 20 * MIN_MS

    wl_b = WhitelistStateRecord(
        nextAddrId=1,
        launchedAt=t_14_20,
        coins=[CoinTrack(coin="BTC", pinned=True, trackedFrom=day1_start, trackedTo=None)],
        addresses={
            ADDR_A: WhitelistMemberRecord(
                id=0,
                address=ADDR_A,
                coverageStart=day1_start - DAY_MS,
                score=0.9,
                metrics=_sample_metrics(),
                membership=[(day1_start, t_14_20 - 60_000, 0), (t_14_20 - 60_000, None, 1)],
                abstraction="disabled",
            )
        },
    )
    _seed_daily_metadata(d_store_b, wl_b, t_14_20)
    # Seed launch-day reconstructed snapshot: series.t ends 14:00, posChanges has fill at 14:15
    pre_day = Snapshot(
        schemaVersion=1,
        coins=["BTC"],
        reconstructed=True,
        reconstructedBefore=t_14_20,
        markResolution="1h",
        tracked=[(0, day1_start, t_14_15)],
        keyframe=SnapshotKeyframe(t=day1_start, pos=[]),
        posChanges=[(t_14_15, 0, 0, 2.5, 95_000.0)],
        series=SnapshotSeries(
            t=[t_14_00],
            mark=[[95_000.0]],
            av=SnapshotAvSeries(addrIds=[0], values=[[2_000_000.0]]),
        ),
    )
    write_json_file_atomic(
        data_b / "snapshots" / "days" / "2026-11-15.json",
        pre_day.model_dump(mode="json", exclude_none=True),
        compress=False,
    )

    def handle_info_b(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        ep = body["type"]
        if ep == "metaAndAssetCtxs":
            return httpx.Response(200, json=_meta_and_ctxs_payload())
        if ep == "clearinghouseState":
            return httpx.Response(200, json=_ch_payload("2000000.0", [("BTC", "2.5", "95000.0")]))
        if ep == "userFillsByTime":
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected endpoint {ep}")

    with respx.mock() as mock_router:
        mock_router.post(DEFAULT_INFO_URL).mock(side_effect=handle_info_b)
        res_b = asyncio.run(
            run_snapshot(cfg, f_store_b, data_dir=data_b, schemas_dir=SCHEMAS_DIR, now_ms=t_14_20)
        )
    assert res_b.pos_changes_emitted == 0
    post_day = Snapshot.model_validate(
        read_json_file(data_b / "snapshots" / "days" / "2026-11-15.json")
    )
    assert len(post_day.posChanges) == 1
    assert replay_snapshot_positions_at(post_day, t_14_20) == {(0, "BTC"): (2.5, 95_000.0)}

# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline tests for Phase 4.2 historical snapshot reconstruction (§6.5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from hyperpod.client.info_api import NormalizedFill
from hyperpod.config import load_config
from hyperpod.models import (
    BackfillCandles,
    ClearinghouseStateResponse,
    PortfolioResponse,
    SingleIntervalCandles,
    Snapshot,
    Whitelist,
    WhitelistMetrics,
)
from hyperpod.models.manifest import CoinTrack, Manifest
from hyperpod.scoring.whitelist import WhitelistMemberRecord, WhitelistStateRecord
from hyperpod.snapshot import reconstruct as reconstruct_mod
from hyperpod.snapshot.candles import FOUR_HOUR_MS, HOUR_MS
from hyperpod.snapshot.reconstruct import (
    ReconstructAddressInputs,
    build_av_curve,
    interpolate_account_value,
    reconstruct_address_coin_timeline,
    run_reconstruct,
)
from hyperpod.storage.formats import (
    is_addr_tracked_at,
    read_json_file,
    replay_snapshot_positions_at,
    write_json_file_atomic,
)
from hyperpod.storage.site_writer import rebuild_manifest, validate_site_data_directory
from hyperpod.storage.state_store import StateStore
from hyperpod.util.time import DAY_MS, MIN_MS, ms_to_utc_date, utc_date_to_start_ms

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
ADDR_0 = "0x1111111111111111111111111111111111111111"
ADDR_1 = "0x2222222222222222222222222222222222222222"


def _sample_metrics(av: float = 3_000_000.0) -> WhitelistMetrics:
    return WhitelistMetrics(
        accountValue=av,
        equitySource="perp",
        pnlAllTime=800_000.0,
        roiAllTime=0.40,
        pnlMonth=120_000.0,
        maxDrawdown=0.12,
        profitFactor=2.4,
        profitFactorRobust=1.9,
        winRate=0.60,
        nTrades=40,
        historyDays=250.0,
        fillsPerDay=10.0,
        medianHoldMinutes=600.0,
        makerRatio=0.4,
        turnoverMonth=3.0,
        fundingShare=0.02,
        topWinShare=0.18,
        hedgeRatio=0.0,
    )


def _clearinghouse(
    positions: list[tuple[str, str, str]], av: str = "3000000.0"
) -> ClearinghouseStateResponse:
    ms = {"accountValue": av, "totalMarginUsed": "5000", "totalNtlPos": "50000", "totalRawUsd": av}
    return ClearinghouseStateResponse.model_validate(
        {
            "assetPositions": [
                {
                    "type": "oneWay",
                    "position": {
                        "coin": coin,
                        "cumFunding": {"allTime": "0", "sinceOpen": "0", "sinceChange": "0"},
                        "entryPx": epx,
                        "leverage": {"type": "cross", "value": 5},
                        "marginUsed": "1000",
                        "maxLeverage": 50,
                        "positionValue": "50000",
                        "returnOnEquity": "0.1",
                        "szi": szi,
                        "unrealizedPnl": "500",
                    },
                }
                for coin, szi, epx in positions
            ],
            "crossMaintenanceMarginUsed": "200",
            "crossMarginSummary": ms,
            "marginSummary": ms,
            "time": 1_763_164_800_000,
            "withdrawable": av,
        }
    )


def _portfolio(av_points: list[tuple[int, float]]) -> PortfolioResponse:
    pnl_points = [[ts, "0.0"] for ts, _ in av_points]
    av_hist = [[ts, f"{av}"] for ts, av in av_points]
    w = {"accountValueHistory": av_hist, "pnlHistory": pnl_points, "vlm": "1000000.0"}
    return PortfolioResponse.model_validate(
        [
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
    )


def _norm_fill(
    *,
    coin: str,
    t: int,
    seq: int,
    dir_code: str,
    px: float,
    sz: float,
    start_pos: float,
    leg_idx: int = 0,
) -> NormalizedFill:
    return NormalizedFill(
        coin=coin,
        time=t,
        api_seq_index=seq,
        leg_idx=leg_idx,
        dir_code=dir_code,  # type: ignore[arg-type]
        px=px,
        sz=sz,
        notional=px * sz,
        closed_pnl=0.0,
        fee=1.0,
        start_position=start_pos,
        crossed=False,
        liq=0,
        tid=seq + 1,
        oid=seq + 1,
        hash=f"0x{seq + 1:064x}",
    )


def test_reconstruct_coin_timeline_weighted_entry_truncated_null_and_zero_fill() -> None:
    t0 = utc_date_to_start_ms("2026-10-01")
    t_launch = t0 + 10 * DAY_MS

    # Truncated position (+3.0 -> close to 0 -> open +2.0 @ 90k -> add +2.0 @ 94k -> avg 92k)
    fills = [
        _norm_fill(
            coin="BTC", t=t0 + HOUR_MS, seq=0, dir_code="CL", px=88_000.0, sz=3.0, start_pos=3.0
        ),
        _norm_fill(
            coin="BTC", t=t0 + 2 * HOUR_MS, seq=1, dir_code="OL", px=90_000.0, sz=2.0, start_pos=0.0
        ),
        _norm_fill(
            coin="BTC", t=t0 + 3 * HOUR_MS, seq=2, dir_code="OL", px=94_000.0, sz=2.0, start_pos=2.0
        ),
    ]
    res = reconstruct_address_coin_timeline(
        fills,
        address=ADDR_0,
        coin="BTC",
        eff_start_ms=t0,
        launched_at_ms=t_launch,
        current_szi=4.0,
        current_entry_px=92_000.0,
        sz_decimals=5,
    )
    assert res.init_pos == (3.0, None)  # Pre-existing truncated position has null entryPx
    assert res.events == [
        (t0 + HOUR_MS, 0.0, None),
        (t0 + 2 * HOUR_MS, 2.0, 90_000.0),
        (t0 + 3 * HOUR_MS, 4.0, 92_000.0),
    ]
    assert res.mismatch_warning is None

    # Zero-fill open position constant backfill
    zero_res = reconstruct_address_coin_timeline(
        [],
        address=ADDR_0,
        coin="ETH",
        eff_start_ms=t0,
        launched_at_ms=t_launch,
        current_szi=-50.0,
        current_entry_px=3_050.0,
        sz_decimals=4,
    )
    assert zero_res.init_pos == (-50.0, 3_050.0)
    assert zero_res.events == []
    assert zero_res.mismatch_warning is None

    # Mismatch triggers warning
    bad_res = reconstruct_address_coin_timeline(
        fills,
        address=ADDR_0,
        coin="BTC",
        eff_start_ms=t0,
        launched_at_ms=t_launch,
        current_szi=10.0,
        current_entry_px=92_000.0,
        sz_decimals=5,
    )
    assert bad_res.mismatch_warning is not None


def test_portfolio_av_linear_interpolation_and_unknown_exclusion() -> None:
    t0 = utc_date_to_start_ms("2026-10-01")
    pf = _portfolio([(t0, 1_000_000.0), (t0 + 10 * DAY_MS, 2_000_000.0)])
    curve = build_av_curve(pf, current_equity=2_500_000.0, abstraction="unifiedAccount")
    # Unified spot offset +500k shifts curve to [1.5M, 2.5M]
    assert interpolate_account_value(curve, t0 + 2 * DAY_MS, eff_start_ms=t0 + 5 * DAY_MS) is None
    mid_av = interpolate_account_value(curve, t0 + 5 * DAY_MS, eff_start_ms=t0)
    assert mid_av == pytest.approx(2_000_000.0)


def test_run_reconstruct_208d_1h_and_4h_with_exact_snapshot_replay(tmp_path: Path) -> None:
    cfg = load_config()
    # Bounded config (220d total, 208d at 1h, 12d at 4h) exercising both tiers quickly.
    cfg = cfg.model_copy(
        update={
            "snapshot": cfg.snapshot.model_copy(
                update={
                    "reconstructDays": cfg.snapshot.reconstructDays.model_copy(
                        update={"value": 220}
                    ),
                }
            )
        }
    )

    launched_at = utc_date_to_start_ms("2026-11-15")
    recon_start = utc_date_to_start_ms(ms_to_utc_date(launched_at - 220 * DAY_MS))
    cutoff_1h = utc_date_to_start_ms(ms_to_utc_date(launched_at - 208 * DAY_MS))

    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    store = StateStore(state_dir, owner="daily")

    cov_1 = cutoff_1h + 3 * DAY_MS + 12 * HOUR_MS  # Mid-day coverageStart inside 1h zone

    wl_state = WhitelistStateRecord(
        nextAddrId=2,
        coins=[
            CoinTrack(coin="BTC", pinned=True, trackedFrom=recon_start, trackedTo=None),
            CoinTrack(coin="ETH", pinned=True, trackedFrom=recon_start, trackedTo=None),
        ],
        addresses={
            ADDR_0: WhitelistMemberRecord(
                id=0,
                address=ADDR_0,
                coverageStart=recon_start,
                score=0.9,
                metrics=_sample_metrics(3_000_000.0),
                membership=[(launched_at, None, 1)],
                abstraction="disabled",
            ),
            ADDR_1: WhitelistMemberRecord(
                id=1,
                address=ADDR_1,
                coverageStart=cov_1,
                score=0.8,
                metrics=_sample_metrics(2_000_000.0),
                membership=[(launched_at, None, 1)],
                abstraction="disabled",
            ),
        },
    )
    store.save_whitelist_state(wl_state)

    from hyperpod.models import CandidateRow, CandidatesFile, CriteriaFile
    from hyperpod.scoring.whitelist import update_criteria_state

    rows = [
        CandidateRow(
            idx=idx,
            passed=True,
            rejectReasons=[],
            fillsPerDay=10.0,
            makerRatio=0.4,
            medianHoldMinutes=600.0,
            maxDrawdown=0.12,
            profitFactorRobust=1.9,
            turnoverMonth=3.0,
        )
        for idx in range(2)
    ]
    criteria = update_criteria_state(cfg, None, eval_ts_ms=launched_at, candidates=rows)
    wl_pub = Whitelist(
        schemaVersion=1,
        version="2026-11-15",
        addresses=[m.to_public() for m in sorted(wl_state.addresses.values(), key=lambda x: x.id)],
    )
    cands = CandidatesFile(
        schemaVersion=1,
        generatedAt=launched_at,
        criteriaVersion=criteria.current.version,
        candidates=rows,
    )
    CriteriaFile.model_validate(criteria.model_dump())
    store.save_daily_outputs(whitelist=wl_pub, criteria=criteria, candidates=cands)

    # Seed minimal archived candles spanning [recon_start, launched_at) for BTC & ETH
    for coin, px in (("BTC", 95_000.0), ("ETH", 3_100.0)):
        bars_4h = [
            (t, px, px * 1.01, px * 0.99, px, 100.0)
            for t in range(recon_start, cutoff_1h, FOUR_HOUR_MS)
        ]
        bars_1h = [
            (t, px, px * 1.01, px * 0.99, px, 50.0)
            for t in range(cutoff_1h, launched_at - 17 * DAY_MS, HOUR_MS)
        ]
        bars_5m = [
            (t, px, px * 1.01, px * 0.99, px, 10.0)
            for t in range(launched_at - 17 * DAY_MS, launched_at, 3_600_000)
        ]
        write_json_file_atomic(
            data_dir / "candles" / coin / "overview_4h.json.gz",
            SingleIntervalCandles(schemaVersion=1, coin=coin, interval="4h", c=bars_4h).model_dump(
                mode="json"
            ),
            compress=True,
        )
        write_json_file_atomic(
            data_dir / "candles" / coin / "backfill_15m_1h.json.gz",
            BackfillCandles(
                schemaVersion=1, coin=coin, interval="15m_1h", c15m=[], c1h=bars_1h
            ).model_dump(mode="json"),
            compress=True,
        )
        # Partition 5m by month
        by_m: dict[str, list[Any]] = {}
        for b in bars_5m:
            m = ms_to_utc_date(b[0])[:7]
            by_m.setdefault(m, []).append(b)
        for m_str, m_rows in by_m.items():
            write_json_file_atomic(
                data_dir / "candles" / coin / "5m" / f"{m_str}.json.gz",
                SingleIntervalCandles(
                    schemaVersion=1, coin=coin, interval="5m", c=m_rows
                ).model_dump(mode="json"),
                compress=True,
            )

    # ADDR_0 trades BTC at 4h tier and ETH zero-fill constant backfill; ADDR_1 opens BTC after cov_1
    t_4h_open = recon_start + 2 * DAY_MS + 4 * HOUR_MS
    t_1h_add = cutoff_1h + 10 * DAY_MS + 6 * HOUR_MS
    t_1_open = cov_1 + 2 * HOUR_MS

    addr0_fills = [
        _norm_fill(
            coin="BTC", t=t_4h_open, seq=0, dir_code="OL", px=90_000.0, sz=2.0, start_pos=0.0
        ),
        _norm_fill(
            coin="BTC", t=t_1h_add, seq=1, dir_code="OL", px=96_000.0, sz=2.0, start_pos=2.0
        ),
    ]
    addr1_fills = [
        _norm_fill(
            coin="BTC", t=t_1_open, seq=0, dir_code="OS", px=94_000.0, sz=1.5, start_pos=0.0
        ),
    ]

    prefetched = {
        ADDR_0: ReconstructAddressInputs(
            clearinghouse=_clearinghouse(
                [("BTC", "4.0", "93000.0"), ("ETH", "100.0", "3050.0")], av="3000000.0"
            ),
            abstraction="disabled",
            spot=None,
            portfolio=_portfolio([(recon_start, 2_500_000.0), (launched_at, 3_000_000.0)]),
            normalized_fills=addr0_fills,
            last_fill_cursor_ms=t_1h_add,
        ),
        ADDR_1: ReconstructAddressInputs(
            clearinghouse=_clearinghouse([("BTC", "-1.5", "94000.0")], av="2000000.0"),
            abstraction="disabled",
            spot=None,
            portfolio=_portfolio([(cov_1, 1_800_000.0), (launched_at, 2_000_000.0)]),
            normalized_fills=addr1_fills,
            last_fill_cursor_ms=t_1_open,
        ),
    }

    res = asyncio.run(
        run_reconstruct(
            cfg,
            store,
            data_dir=data_dir,
            schemas_dir=SCHEMAS_DIR,
            launched_at_ms=launched_at,
            prefetched_address_data=prefetched,
            sz_decimals_override={"BTC": 5, "ETH": 4},
        )
    )
    assert res.days_reconstructed == 220
    assert (
        res.hot_days_written == 6
    )  # last 6 days < launched_at (7th hot day is launched_at day itself)
    assert res.szi_mismatches == 0
    assert res.fills_rows_upserted == 3

    # Verify retrospective criteriaVersion=0 membership in whitelist.json
    wl_out = Whitelist.model_validate(read_json_file(data_dir / "whitelist.json"))
    assert wl_out.addresses[0].membership == [(recon_start, launched_at, 0), (launched_at, None, 1)]
    assert wl_out.addresses[1].membership == [(cov_1, launched_at, 0), (launched_at, None, 1)]

    # Verify intermediate and terminal snapshots match ground truth positions!
    # 1. At t_4h_open (4h month file): ADDR_0 has BTC +2.0, ETH +100.0; ADDR_1 is UNKNOWN.
    m_4h_path = data_dir / "snapshots" / "months" / f"{ms_to_utc_date(t_4h_open)[:7]}.json.gz"
    m_4h_snap = Snapshot.model_validate(read_json_file(m_4h_path))
    assert m_4h_snap.reconstructed is True
    assert m_4h_snap.markResolution in {"4h", "mixed"}
    pos_at_4h = replay_snapshot_positions_at(m_4h_snap, t_4h_open)
    assert pos_at_4h[(0, "BTC")] == (2.0, 90_000.0)
    assert pos_at_4h[(0, "ETH")] == (100.0, 3_050.0)
    assert not is_addr_tracked_at(m_4h_snap.tracked, 1, t_4h_open)

    # 2. At t_1h_add (1h month file): ADDR_0 BTC becomes +4.0 @ 93k, ADDR_1 BTC is -1.5 @ 94k
    m_1h_path = data_dir / "snapshots" / "months" / f"{ms_to_utc_date(t_1h_add)[:7]}.json.gz"
    m_1h_snap = Snapshot.model_validate(read_json_file(m_1h_path))
    pos_at_1h = replay_snapshot_positions_at(m_1h_snap, t_1h_add)
    assert pos_at_1h[(0, "BTC")] == (4.0, 93_000.0)
    assert pos_at_1h[(0, "ETH")] == (100.0, 3_050.0)
    assert pos_at_1h[(1, "BTC")] == (-1.5, 94_000.0)

    # 3. Terminal day (launched_at - 1h) matches clearinghouseState 100%!
    last_day_str = ms_to_utc_date(launched_at - DAY_MS)
    last_day_snap = Snapshot.model_validate(
        read_json_file(data_dir / "snapshots" / "days" / f"{last_day_str}.json")
    )
    assert last_day_snap.markResolution == "1h"
    pos_terminal = replay_snapshot_positions_at(last_day_snap, launched_at - HOUR_MS)
    assert pos_terminal == {
        (0, "BTC"): (4.0, 93_000.0),
        (0, "ETH"): (100.0, 3_050.0),
        (1, "BTC"): (-1.5, 94_000.0),
    }

    # Write a latest.json matching terminal state so full validate_site_data_directory runs cleanly
    write_json_file_atomic(
        data_dir / "snapshots" / "latest.json",
        last_day_snap.model_copy(
            update={"reconstructed": False, "reconstructedBefore": None, "markResolution": None}
        ).model_dump(mode="json", exclude_none=True),
        compress=False,
    )

    rebuild_manifest(
        data_dir, cfg, whitelist_state=wl_state, now_ms=launched_at, schemas_dir=SCHEMAS_DIR
    )
    validate_site_data_directory(data_dir, schemas_dir=SCHEMAS_DIR)


def test_reconstruct_clears_backfill_dedups_midday_init_tracked_from_and_latest(
    tmp_path: Path,
) -> None:
    cfg = load_config().model_copy(deep=True)
    cfg.snapshot.reconstructDays.value = 3

    launched_at = utc_date_to_start_ms("2026-11-15")
    recon_start = launched_at - 3 * DAY_MS
    cov_mid = recon_start + DAY_MS + 12 * HOUR_MS  # Mid-day coverageStart on day 2

    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    store = StateStore(state_dir, owner="daily")

    # Seed ETH trackedFrom=launched_at and coinBackfillQueue=["ETH"] (FINDING-01 & 04)
    wl_state = WhitelistStateRecord(
        nextAddrId=1,
        coins=[
            CoinTrack(coin="BTC", pinned=True, trackedFrom=launched_at, trackedTo=None),
            CoinTrack(coin="ETH", pinned=True, trackedFrom=launched_at, trackedTo=None),
        ],
        coinBackfillQueue=["ETH"],
        addresses={
            ADDR_0: WhitelistMemberRecord(
                id=0,
                address=ADDR_0,
                coverageStart=cov_mid,
                score=0.9,
                metrics=_sample_metrics(2_000_000.0),
                membership=[(launched_at, None, 1)],
                abstraction="disabled",
            )
        },
    )
    store.save_whitelist_state(wl_state)

    from hyperpod.models import CandidateRow, CandidatesFile, Whitelist
    from hyperpod.scoring.whitelist import update_criteria_state

    rows = [
        CandidateRow(
            idx=0,
            passed=True,
            rejectReasons=[],
            fillsPerDay=10.0,
            makerRatio=0.4,
            medianHoldMinutes=600.0,
            maxDrawdown=0.12,
            profitFactorRobust=1.9,
            turnoverMonth=3.0,
        )
    ]
    criteria = update_criteria_state(cfg, None, eval_ts_ms=launched_at, candidates=rows)
    wl_pub = Whitelist(
        schemaVersion=1,
        version="2026-11-15",
        addresses=[wl_state.addresses[ADDR_0].to_public()],
    )
    cands = CandidatesFile(
        schemaVersion=1,
        generatedAt=launched_at,
        criteriaVersion=criteria.current.version,
        candidates=rows,
    )
    store.save_daily_outputs(whitelist=wl_pub, criteria=criteria, candidates=cands)

    for coin, px in (("BTC", 95_000.0), ("ETH", 3_100.0)):
        bars_5m = [
            (t, px, px * 1.01, px * 0.99, px, 10.0)
            for t in range(recon_start, launched_at, HOUR_MS)
        ]
        write_json_file_atomic(
            data_dir / "candles" / coin / "5m" / "2026-11.json.gz",
            SingleIntervalCandles(schemaVersion=1, coin=coin, interval="5m", c=bars_5m).model_dump(
                mode="json"
            ),
            compress=True,
        )

    # Fill EXACTLY AT cov_mid (eff_start_ms) taking truncated BTC from +2.0 to +3.0 (FINDING-04)
    prefetched = {
        ADDR_0: ReconstructAddressInputs(
            clearinghouse=_clearinghouse([("BTC", "3.0", "95000.0")], av="2000000.0"),
            abstraction="disabled",
            spot=None,
            portfolio=_portfolio([(cov_mid, 1_900_000.0), (launched_at, 2_000_000.0)]),
            normalized_fills=[
                _norm_fill(
                    coin="BTC",
                    t=cov_mid,
                    seq=0,
                    dir_code="OL",
                    px=95_000.0,
                    sz=1.0,
                    start_pos=2.0,
                )
            ],
            last_fill_cursor_ms=cov_mid,
        )
    }

    res = asyncio.run(
        run_reconstruct(
            cfg,
            store,
            data_dir=data_dir,
            schemas_dir=SCHEMAS_DIR,
            launched_at_ms=launched_at,
            write_fills=True,
            prefetched_address_data=prefetched,
            sz_decimals_override={"BTC": 5, "ETH": 4},
        )
    )
    assert res.szi_mismatches == 0

    # FINDING-01: reconstruct(write_fills=True) clears coinBackfillQueue
    saved_wl = store.load_whitelist_state()
    assert saved_wl is not None
    assert saved_wl.coinBackfillQueue == []

    # FINDING-04 (1): No duplicate same-ms posChanges at eff_start_ms == cov_mid
    mid_day_str = ms_to_utc_date(cov_mid)
    mid_snap = Snapshot.model_validate(
        read_json_file(data_dir / "snapshots" / "days" / f"{mid_day_str}.json")
    )
    changes_at_cov = [c for c in mid_snap.posChanges if c[0] == cov_mid and c[1] == 0]
    assert len(changes_at_cov) == 1
    assert changes_at_cov[0][3] == pytest.approx(3.0)
    assert replay_snapshot_positions_at(mid_snap, cov_mid) == {(0, "BTC"): (3.0, None)}

    # FINDING-04 (2): manifest.coins trackedFrom <= reconstruct_start_ms
    manifest = Manifest.model_validate(read_json_file(data_dir / "manifest.json"))
    for c_track in manifest.coins:
        assert c_track.trackedFrom <= recon_start

    # FINDING-04 (3): latest.json is written automatically by standalone run_reconstruct
    assert (data_dir / "snapshots" / "latest.json").is_file()
    validate_site_data_directory(data_dir, schemas_dir=SCHEMAS_DIR)


def test_reconstruct_in_flight_post_launch_fills_and_cursor_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config().model_copy(deep=True)
    cfg.snapshot.reconstructDays.value = 2

    t_launch = utc_date_to_start_ms("2026-11-15")
    recon_start = t_launch - 2 * DAY_MS
    t_pre = t_launch - HOUR_MS
    t_after = t_launch + 5_000  # In-flight fill after t_launch

    # 1. reconstruct_address_coin_timeline subtracts post-launch delta when verifying szi
    fills_with_post = [
        _norm_fill(coin="BTC", t=t_pre, seq=0, dir_code="OL", px=94_000.0, sz=4.0, start_pos=0.0),
        _norm_fill(coin="BTC", t=t_after, seq=1, dir_code="OL", px=96_000.0, sz=1.0, start_pos=4.0),
    ]
    tl = reconstruct_address_coin_timeline(
        fills_with_post,
        address=ADDR_0,
        coin="BTC",
        eff_start_ms=recon_start,
        launched_at_ms=t_launch,
        current_szi=5.0,
        current_entry_px=94_400.0,
        sz_decimals=5,
    )
    assert tl.mismatch_warning is None
    assert tl.events == [(t_pre, 4.0, 94_000.0)]

    # 2. run_reconstruct caps fill_cursors_by_id <= t_launch when any fill exceeds t_launch
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    store = StateStore(state_dir, owner="daily")
    wl_state = WhitelistStateRecord(
        nextAddrId=1,
        coins=[CoinTrack(coin="BTC", pinned=True, trackedFrom=recon_start, trackedTo=None)],
        addresses={
            ADDR_0: WhitelistMemberRecord(
                id=0,
                address=ADDR_0,
                coverageStart=recon_start,
                score=0.9,
                metrics=_sample_metrics(),
                membership=[(t_launch, None, 1)],
                abstraction="disabled",
            )
        },
    )
    store.save_whitelist_state(wl_state)

    from hyperpod.models import CandidateRow, CandidatesFile, Whitelist
    from hyperpod.scoring.whitelist import update_criteria_state

    rows = [
        CandidateRow(
            idx=0,
            passed=True,
            rejectReasons=[],
            fillsPerDay=10.0,
            makerRatio=0.4,
            medianHoldMinutes=600.0,
            maxDrawdown=0.12,
            profitFactorRobust=1.9,
            turnoverMonth=3.0,
        )
    ]
    crit = update_criteria_state(cfg, None, eval_ts_ms=t_launch, candidates=rows)
    wl_pub = Whitelist(
        schemaVersion=1,
        version="2026-11-15",
        addresses=[wl_state.addresses[ADDR_0].to_public()],
    )
    cands = CandidatesFile(
        schemaVersion=1, generatedAt=t_launch, criteriaVersion=crit.current.version, candidates=rows
    )
    store.save_daily_outputs(whitelist=wl_pub, criteria=crit, candidates=cands)
    c_rows = [
        (t, 95_000.0, 95_000.0, 95_000.0, 95_000.0, 10.0)
        for t in range(recon_start, t_launch, HOUR_MS)
    ]
    write_json_file_atomic(
        data_dir / "candles" / "BTC" / "5m" / "2026-11.json.gz",
        SingleIntervalCandles(
            schemaVersion=1,
            coin="BTC",
            interval="5m",
            c=c_rows,
        ).model_dump(mode="json"),
        compress=True,
    )

    prefetched = {
        ADDR_0: ReconstructAddressInputs(
            clearinghouse=_clearinghouse([("BTC", "5.0", "94400.0")]),
            abstraction="disabled",
            spot=None,
            portfolio=_portfolio([(recon_start, 2_500_000.0), (t_launch, 3_000_000.0)]),
            normalized_fills=fills_with_post,
            last_fill_cursor_ms=t_after,
        )
    }
    res = asyncio.run(
        run_reconstruct(
            cfg,
            store,
            data_dir=data_dir,
            schemas_dir=SCHEMAS_DIR,
            launched_at_ms=t_launch,
            prefetched_address_data=prefetched,
            sz_decimals_override={"BTC": 5},
        )
    )
    assert res.szi_mismatches == 0
    assert res.fill_cursors_by_id[0] <= t_launch
    assert res.fill_cursors_by_id[0] == t_pre

    # 3. When launched_at_ms=None in live mode, t_launch is captured AFTER the fetch loop completes
    clock = {"now": t_launch - 60_000}
    monkeypatch.setattr(reconstruct_mod, "now_utc_ms", lambda: clock["now"])

    import httpx
    import respx

    from hyperpod.client.info_api import DEFAULT_INFO_URL

    def handle_info(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content.decode("utf-8"))
        ep = body["type"]
        if ep == "meta":
            return httpx.Response(
                200,
                json={
                    "universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}],
                    "marginTables": [],
                },
            )
        if ep == "clearinghouseState":
            ch_payload = _clearinghouse([("BTC", "5.0", "94400.0")]).model_dump()
            return httpx.Response(200, json=ch_payload)
        if ep == "portfolio":
            pf_payload = _portfolio(
                [(recon_start, 2_500_000.0), (t_after, 3_000_000.0)]
            ).model_dump()
            return httpx.Response(200, json=pf_payload)
        if ep == "userFillsByTime":
            clock["now"] = t_after + MIN_MS
            return httpx.Response(
                200,
                json=[
                    {
                        "coin": "BTC",
                        "px": "94000.0",
                        "sz": "4.0",
                        "side": "B",
                        "time": t_pre,
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
                        "sz": "1.0",
                        "side": "B",
                        "time": t_after,
                        "startPosition": "4.0",
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
        raise AssertionError(f"Unexpected endpoint {ep}")

    data_live = tmp_path / "data_live"
    c_rows_live = [
        (t, 95_000.0, 95_000.0, 95_000.0, 95_000.0, 10.0)
        for t in range(recon_start, t_launch + DAY_MS, HOUR_MS)
    ]
    write_json_file_atomic(
        data_live / "candles" / "BTC" / "5m" / "2026-11.json.gz",
        SingleIntervalCandles(
            schemaVersion=1,
            coin="BTC",
            interval="5m",
            c=c_rows_live,
        ).model_dump(mode="json"),
        compress=True,
    )
    with respx.mock() as mock_router:
        mock_router.post(DEFAULT_INFO_URL).mock(side_effect=handle_info)
        res_live = asyncio.run(
            run_reconstruct(
                cfg,
                store,
                data_dir=data_live,
                schemas_dir=SCHEMAS_DIR,
                launched_at_ms=None,
            )
        )
    assert res_live.launched_at == t_after + MIN_MS
    assert res_live.szi_mismatches == 0
    assert res_live.fill_cursors_by_id[0] <= res_live.launched_at

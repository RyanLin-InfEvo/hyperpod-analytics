# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline tests for Phase 4.3 site_writer compaction, manifest rebuild & git_publish."""

from __future__ import annotations

import asyncio
import gzip
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from hyperpod.client.info_api import DEFAULT_INFO_URL
from hyperpod.config import load_config
from hyperpod.models import FillRow, FillsFile, Manifest, Snapshot, WhitelistMetrics
from hyperpod.models.manifest import CoinTrack
from hyperpod.models.snapshot import SnapshotAvSeries, SnapshotKeyframe, SnapshotSeries
from hyperpod.scoring.whitelist import WhitelistMemberRecord, WhitelistStateRecord
from hyperpod.storage import git_publish, site_writer
from hyperpod.storage.formats import (
    read_json_file,
    replay_snapshot_positions_at,
    write_json_file_atomic,
)
from hyperpod.storage.git_publish import (
    GitPublishError,
    PublishOwner,
    apply_scoped_overlay,
    publish_orphan_branch,
    scan_directory_for_secrets,
)
from hyperpod.storage.site_writer import (
    compact_old_daily_files,
    merge_snapshots_to_month,
    rebuild_manifest,
    run_daily_publish,
    upsert_coin_fills,
    validate_site_data_directory,
    verify_snapshot_compaction_equivalence,
)
from hyperpod.storage.state_store import StateStore
from hyperpod.util.time import (
    HOUR_MS,
    MIN_MS,
    recent_utc_dates,
    utc_date_to_start_ms,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "schemas"
SITE_DATA_FIXTURE = REPO_ROOT / "fixtures" / "site-data"


def _make_daily_snap(
    day_str: str,
    *,
    coins: list[str],
    kf_pos: list[tuple[int, int, float, float | None]],
    pos_changes: list[tuple[int, int, int, float, float | None]],
    tracked: list[tuple[int, int, int | None]],
    stale: list[tuple[int, list[int]]] | None = None,
) -> Snapshot:
    d0 = utc_date_to_start_ms(day_str)
    # 12 points across the day (every 2h -> or 24 points every 1h / 144 points every 10m)
    t_vals = [d0 + i * 10 * MIN_MS for i in range(18)]  # first 3 hours (00:00..02:50)
    mark_rows: list[list[float | None]] = [[95000.0 + i for i in range(len(t_vals))] for _ in coins]
    addr_ids = sorted({t[0] for t in tracked})
    av_rows: list[list[float | None]] = [
        [2_000_000.0 + i * 100.0 for i in range(len(t_vals))] for _ in addr_ids
    ]
    return Snapshot(
        schemaVersion=1,
        coins=coins,
        reconstructed=False,
        tracked=tracked,
        keyframe=SnapshotKeyframe(t=d0, pos=kf_pos),
        posChanges=pos_changes,
        series=SnapshotSeries(
            t=t_vals,
            mark=mark_rows,
            av=SnapshotAvSeries(addrIds=addr_ids, values=av_rows),
        ),
        stale=stale,
    )


def test_upsert_coin_fills_dedup_and_month_mutual_exclusion(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    now_ms = utc_date_to_start_ms("2026-11-15")
    hot_days = recent_utc_dates(now_ms, 7)

    old_t = utc_date_to_start_ms("2026-11-01") + 3600_000
    hot_t = utc_date_to_start_ms("2026-11-14") + 3600_000
    # Seed hot_t already compacted in months/2026-11.json.gz to verify mutual exclusion guard
    upsert_coin_fills(
        data_dir,
        "BTC",
        [(old_t, 0, "OL", 95_000.0, 1.0, 95_000.0, 0)],
        hot_days=hot_days,
        sz_decimals=5,
        schemas_dir=SCHEMAS_DIR,
    )
    # Duplicate key overwrites cleanly; hot_t routes to days/2026-11-14.json
    upsert_coin_fills(
        data_dir,
        "BTC",
        [
            (old_t, 0, "OL", 95_100.0, 1.2, 114_120.0, 0),
            (hot_t, 0, "CL", 96_000.0, 0.5, 48_000.0, 0),
        ],
        hot_days=hot_days,
        sz_decimals=5,
        schemas_dir=SCHEMAS_DIR,
    )
    month_f = FillsFile.model_validate(
        read_json_file(data_dir / "fills" / "BTC" / "months" / "2026-11.json.gz")
    )
    assert len(month_f.fills) == 1
    assert month_f.fills[0][3] == pytest.approx(95_100.0)
    day_f = FillsFile.model_validate(
        read_json_file(data_dir / "fills" / "BTC" / "days" / "2026-11-14.json")
    )
    assert len(day_f.fills) == 1
    assert day_f.fills[0][2] == "CL"


def test_compact_old_daily_files_sparsifies_series_verifies_replay_and_aborts_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config()
    data_dir = tmp_path / "data"
    d1 = "2026-11-01"
    d2 = "2026-11-02"
    d_hot = "2026-11-14"
    d1_ms = utc_date_to_start_ms(d1)
    d2_ms = utc_date_to_start_ms(d2)
    now_ms = utc_date_to_start_ms("2026-11-15") + HOUR_MS

    snap1 = _make_daily_snap(
        d1,
        coins=["BTC"],
        kf_pos=[(0, 0, 2.0, 94_000.0)],
        pos_changes=[(d1_ms + 30 * MIN_MS, 0, 0, 3.0, 94_500.0)],
        tracked=[(0, d1_ms, None)],
        stale=[(d1_ms + 20 * MIN_MS, [0])],
    )
    snap2 = _make_daily_snap(
        d2,
        coins=["BTC", "ETH"],
        kf_pos=[(0, 0, 3.0, 94_500.0), (1, 1, -10.0, 3_100.0)],
        pos_changes=[(d2_ms + 40 * MIN_MS, 0, 0, 0.0, None)],
        tracked=[(0, d2_ms, None), (1, d2_ms, None)],
    )
    snap_hot = _make_daily_snap(
        d_hot,
        coins=["BTC", "ETH"],
        kf_pos=[(1, 1, -10.0, 3_100.0)],
        pos_changes=[],
        tracked=[(1, utc_date_to_start_ms(d_hot), None)],
    )
    for d_str, s in ((d1, snap1), (d2, snap2), (d_hot, snap_hot)):
        write_json_file_atomic(
            data_dir / "snapshots" / "days" / f"{d_str}.json",
            s.model_dump(mode="json", exclude_none=True),
            compress=False,
        )
    # Also seed >7d fill day and hot fill day
    write_json_file_atomic(
        data_dir / "fills" / "BTC" / "days" / f"{d1}.json",
        FillsFile(
            schemaVersion=1,
            coin="BTC",
            fills=[(d1_ms + 30 * MIN_MS, 0, "OL", 95_000.0, 1.0, 95_000.0, 0)],
        ).model_dump(mode="json"),
        compress=False,
    )
    write_json_file_atomic(
        data_dir / "fills" / "BTC" / "days" / f"{d_hot}.json",
        FillsFile(
            schemaVersion=1,
            coin="BTC",
            fills=[(utc_date_to_start_ms(d_hot) + MIN_MS, 1, "OS", 96_000.0, 0.5, 48_000.0, 0)],
        ).model_dump(mode="json"),
        compress=False,
    )

    # Negative safety test (Invariant #5): failed verification must keep daily files!
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise ValueError("simulated replay failure")

    monkeypatch.setattr(site_writer, "verify_snapshot_compaction_equivalence", _boom)
    with pytest.raises(ValueError, match="simulated replay failure"):
        compact_old_daily_files(data_dir, cfg, now_ms=now_ms, schemas_dir=SCHEMAS_DIR)
    assert (data_dir / "snapshots" / "days" / f"{d1}.json").is_file()
    assert (data_dir / "snapshots" / "days" / f"{d2}.json").is_file()
    monkeypatch.undo()

    # Genuine compaction sparsifies to hourly, keeps stale/posChanges & hot day.
    comp = compact_old_daily_files(data_dir, cfg, now_ms=now_ms, schemas_dir=SCHEMAS_DIR)
    assert comp.snapshot_days_compacted == 2
    assert comp.snapshot_months_updated == ["2026-11"]
    assert comp.fill_days_compacted == 1
    assert not (data_dir / "snapshots" / "days" / f"{d1}.json").exists()
    assert not (data_dir / "snapshots" / "days" / f"{d2}.json").exists()
    assert (data_dir / "snapshots" / "days" / f"{d_hot}.json").is_file()
    assert not (data_dir / "fills" / "BTC" / "days" / f"{d1}.json").exists()
    assert (data_dir / "fills" / "BTC" / "days" / f"{d_hot}.json").is_file()

    month_snap = Snapshot.model_validate(
        read_json_file(data_dir / "snapshots" / "months" / "2026-11.json.gz")
    )
    # 18 10m points per day spanning 3 UTC hours => 3 hourly points per day = 6 points total!
    assert len(month_snap.series.t) == 6
    assert month_snap.stale == [(d1_ms + 20 * MIN_MS, [0])]
    assert replay_snapshot_positions_at(month_snap, d1_ms + 30 * MIN_MS)[(0, "BTC")] == (
        3.0,
        94_500.0,
    )
    assert replay_snapshot_positions_at(month_snap, d2_ms + 40 * MIN_MS) == {
        (1, "ETH"): (-10.0, 3_100.0)
    }


def test_rebuild_manifest_800mb_soft_limit_warning_never_deletes(tmp_path: Path) -> None:
    cfg = load_config()
    small_limit_cfg = cfg.model_copy(
        update={
            "snapshot": cfg.snapshot.model_copy(
                update={
                    "softLimitBytes": cfg.snapshot.softLimitBytes.model_copy(update={"value": 100})
                }
            )
        }
    )
    data_dir = tmp_path / "data"
    shutil.copytree(SITE_DATA_FIXTURE, data_dir)
    before_count = len(list(data_dir.rglob("*")))

    manifest = rebuild_manifest(
        data_dir,
        small_limit_cfg,
        now_ms=1_763_127_600_000,
        schemas_dir=SCHEMAS_DIR,
    )
    assert manifest.storage.totalBytesOnDisk > 100
    assert any("exceeds soft limit" in w for w in manifest.pipeline.warnings)
    assert len(list(data_dir.rglob("*"))) == before_count
    validate_site_data_directory(data_dir, schemas_dir=SCHEMAS_DIR)


def test_git_publish_single_commit_isolation_and_lease_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config()
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    state_src = tmp_path / "state_src"
    (state_src / "fast").mkdir(parents=True)
    (state_src / "fast" / "fill_cursors.json").write_text('{"0":100}\n', encoding="utf-8")
    (state_src / "daily").mkdir(parents=True)
    (state_src / "daily" / "whitelist_state.json").write_text('{"version":1}\n', encoding="utf-8")
    (state_src / "bootstrap").mkdir(parents=True)
    (state_src / "bootstrap" / "done.json").write_text('{"ok":true}\n', encoding="utf-8")

    data_src = tmp_path / "data_src"
    shutil.copytree(SITE_DATA_FIXTURE, data_src)

    # 1. Bootstrap push to state & gh-pages -> single commit on each branch
    res_st = publish_orphan_branch(
        state_src,
        remote_url=str(remote),
        branch="state",
        owner="bootstrap",
        commit_message="bootstrap state",
        cfg=cfg,
        schemas_dir=SCHEMAS_DIR,
    )
    assert res_st.attempts == 1
    assert not res_st.had_remote_branch

    res_gp = publish_orphan_branch(
        data_src,
        remote_url=str(remote),
        branch="gh-pages",
        owner="bootstrap",
        commit_message="bootstrap data",
        cfg=cfg,
        schemas_dir=SCHEMAS_DIR,
    )
    assert res_gp.attempts == 1

    # 2. Frontend publish preserves gh-pages/data/ while updating root web assets
    web_src = tmp_path / "web_src"
    web_src.mkdir()
    (web_src / "index.html").write_text("<html>HyperPod</html>\n", encoding="utf-8")
    (web_src / "main.dart.js").write_text("// web bundle\n", encoding="utf-8")
    publish_orphan_branch(
        web_src,
        remote_url=str(remote),
        branch="gh-pages",
        owner="frontend",
        commit_message="deploy frontend",
        cfg=cfg,
        schemas_dir=SCHEMAS_DIR,
    )

    # 3. Fast publish on state preserves daily/ and bootstrap/, updates only fast/
    fast_only_state = tmp_path / "fast_only_state"
    (fast_only_state / "fast").mkdir(parents=True)
    (fast_only_state / "fast" / "fill_cursors.json").write_text('{"0":200}\n', encoding="utf-8")
    (fast_only_state / "daily").mkdir(parents=True)
    (fast_only_state / "daily" / "should_not_overwrite.txt").write_text("bad", encoding="utf-8")

    publish_orphan_branch(
        fast_only_state,
        remote_url=str(remote),
        branch="state",
        owner="fast",
        commit_message="fast state update",
        cfg=cfg,
        schemas_dir=SCHEMAS_DIR,
    )

    # 4. Daily publish on state preserves fast/ and bootstrap/, updates only daily/
    daily_only_state = tmp_path / "daily_only_state"
    (daily_only_state / "daily").mkdir(parents=True)
    (daily_only_state / "daily" / "whitelist_state.json").write_text(
        '{"version":2}\n', encoding="utf-8"
    )
    publish_orphan_branch(
        daily_only_state,
        remote_url=str(remote),
        branch="state",
        owner="daily",
        commit_message="daily state update",
        cfg=cfg,
        schemas_dir=SCHEMAS_DIR,
    )

    # Verify single commit count and isolated contents on both orphan branches
    for br in ("state", "gh-pages"):
        verify_clone = tmp_path / f"verify_{br}"
        subprocess.run(
            ["git", "clone", "--branch", br, str(remote), str(verify_clone)],
            check=True,
            capture_output=True,
        )
        cnt = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"], cwd=verify_clone, text=True
        ).strip()
        assert cnt == "1"
        if br == "state":
            assert (verify_clone / "fast" / "fill_cursors.json").read_text(
                "utf-8"
            ).strip() == '{"0":200}'
            assert (verify_clone / "daily" / "whitelist_state.json").read_text(
                "utf-8"
            ).strip() == '{"version":2}'
            assert not (verify_clone / "daily" / "should_not_overwrite.txt").exists()
            assert (verify_clone / "bootstrap" / "done.json").is_file()
        else:
            assert (verify_clone / ".nojekyll").is_file()
            assert (verify_clone / "index.html").is_file()
            validate_site_data_directory(verify_clone / "data", schemas_dir=SCHEMAS_DIR)

    # 5. Simulate --force-with-lease race failure on first push attempt and verify retry succeeds!
    real_run_git = git_publish._run_git
    push_attempts = {"count": 0}

    def flaky_push(
        args: list[str], *, cwd: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "push":
            push_attempts["count"] += 1
            if push_attempts["count"] == 1:
                return subprocess.CompletedProcess(
                    args=["git", *args], returncode=1, stdout="", stderr="! [rejected] (stale info)"
                )
        return real_run_git(args, cwd=cwd, check=check)

    monkeypatch.setattr(git_publish, "_run_git", flaky_push)
    retry_res = publish_orphan_branch(
        data_src,
        remote_url=str(remote),
        branch="gh-pages",
        owner="fast",
        commit_message="fast retry test",
        max_retries=3,
        cfg=cfg,
        schemas_dir=SCHEMAS_DIR,
    )
    monkeypatch.undo()
    assert retry_res.attempts == 2

    # 6. Secret scan rejects credential leaks
    bad_dir = tmp_path / "bad_secret"
    bad_dir.mkdir()
    (bad_dir / "leak.txt").write_text("token = github_pat_1234567890abcdef", encoding="utf-8")
    with pytest.raises(GitPublishError, match="credential pattern"):
        scan_directory_for_secrets(bad_dir)


def test_run_daily_publish_artifact_merge_and_compaction(tmp_path: Path) -> None:
    cfg = load_config()
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    artifact_dir = tmp_path / "artifact"

    # Fast partition pre-exists in state/fast/ and must remain untouched by daily publish
    fast_store = StateStore(state_dir, owner="fast")
    fast_store.save_fast_fill_cursors({0: 999999})

    # Stage daily artifact outputs
    (artifact_dir / "state" / "daily" / "latest_outputs").mkdir(parents=True)
    for fname in ("whitelist.json", "criteria.json", "candidates.json"):
        shutil.copy2(
            SITE_DATA_FIXTURE / fname, artifact_dir / "state" / "daily" / "latest_outputs" / fname
        )
    shutil.copytree(SITE_DATA_FIXTURE / "candles", artifact_dir / "data" / "candles")
    # Also pre-seed snapshots & fills from fixture into data_dir
    shutil.copytree(SITE_DATA_FIXTURE / "snapshots", data_dir / "snapshots")
    shutil.copytree(SITE_DATA_FIXTURE / "fills", data_dir / "fills")

    # End timestamp of fixture is 2026-11-05; publish 10 days later compacts old days
    eval_ts = utc_date_to_start_ms("2026-11-15") + HOUR_MS
    daily_store = StateStore(state_dir, owner="daily")
    summary = asyncio.run(
        run_daily_publish(
            cfg,
            daily_store,
            data_dir=data_dir,
            schemas_dir=SCHEMAS_DIR,
            artifact_dir=artifact_dir,
            now_ms=eval_ts,
            fetch_missing_fills=False,
        )
    )
    assert summary.compaction.snapshot_days_compacted == 7
    assert summary.compaction.fill_days_compacted == 14
    assert fast_store.load_fast_fill_cursors() == {0: 999999}
    validate_site_data_directory(data_dir, schemas_dir=SCHEMAS_DIR)
    assert (
        len(Manifest.model_validate(read_json_file(data_dir / "manifest.json")).files.snapshotDays)
        == 0
    )


def _min_metrics() -> WhitelistMetrics:
    return WhitelistMetrics(
        accountValue=2_000_000.0,
        equitySource="perp",
        pnlAllTime=500_000.0,
        roiAllTime=0.30,
        pnlMonth=80_000.0,
        maxDrawdown=0.10,
        profitFactor=2.0,
        profitFactorRobust=1.7,
        winRate=0.58,
        nTrades=40,
        historyDays=120.0,
        fillsPerDay=10.0,
        medianHoldMinutes=300.0,
        makerRatio=0.3,
        turnoverMonth=4.0,
        fundingShare=0.02,
        topWinShare=0.15,
        hedgeRatio=0.0,
    )


def test_run_daily_publish_backfill_queue_respects_existing_coin_cursors_and_artifact_fill_merge(
    tmp_path: Path,
) -> None:
    cfg = load_config()
    overlap_ms = cfg.ratelimit.fillOverlapMs.value
    eval_ts = utc_date_to_start_ms("2026-11-15") + HOUR_MS
    t_cursor = utc_date_to_start_ms("2026-11-14")
    t_old = utc_date_to_start_ms("2026-10-15") + HOUR_MS
    t_new = t_cursor + 2 * HOUR_MS
    assert t_old < t_cursor - overlap_ms

    # 1. Backfill queue for ETH keeps old ETH fills while filtering old BTC fills < cursor - overlap
    state_dir = tmp_path / "s1"
    data_dir = tmp_path / "d1"
    shutil.copytree(SITE_DATA_FIXTURE, data_dir)
    store = StateStore(state_dir, owner="daily")
    for fname in ("whitelist.json", "criteria.json", "candidates.json"):
        (store.latest_outputs_dir / fname).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SITE_DATA_FIXTURE / fname, store.latest_outputs_dir / fname)

    addr0 = "0x1111111111111111111111111111111111111111"
    wl_state = WhitelistStateRecord(
        nextAddrId=1,
        launchedAt=utc_date_to_start_ms("2026-11-01"),
        coins=[
            CoinTrack(
                coin="BTC",
                pinned=True,
                trackedFrom=utc_date_to_start_ms("2026-10-01"),
                trackedTo=None,
            ),
            CoinTrack(
                coin="ETH",
                pinned=False,
                trackedFrom=eval_ts,
                trackedTo=None,
            ),
        ],
        coinBackfillQueue=["ETH"],
        addresses={
            addr0: WhitelistMemberRecord(
                id=0,
                address=addr0,
                coverageStart=utc_date_to_start_ms("2026-10-01"),
                score=0.9,
                metrics=_min_metrics(),
                membership=[(utc_date_to_start_ms("2026-10-01"), None, 1)],
                abstraction="disabled",
            )
        },
    )
    store.save_whitelist_state(wl_state)
    store.save_daily_publish_fill_cursors({0: t_cursor})

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
        if ep == "userFillsByTime":
            return httpx.Response(
                200,
                json=[
                    {
                        "coin": "BTC",
                        "px": "80000.0",
                        "sz": "1.0",
                        "side": "B",
                        "time": t_old,
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
                        "coin": "ETH",
                        "px": "2500.0",
                        "sz": "10.0",
                        "side": "B",
                        "time": t_old,
                        "startPosition": "0.0",
                        "dir": "Open Long",
                        "closedPnl": "0.0",
                        "hash": "0x" + "2" * 64,
                        "oid": 2,
                        "crossed": False,
                        "fee": "1.0",
                        "tid": 2,
                    },
                    {
                        "coin": "BTC",
                        "px": "95000.0",
                        "sz": "0.5",
                        "side": "B",
                        "time": t_new,
                        "startPosition": "1.0",
                        "dir": "Open Long",
                        "closedPnl": "0.0",
                        "hash": "0x" + "3" * 64,
                        "oid": 3,
                        "crossed": False,
                        "fee": "1.0",
                        "tid": 3,
                    },
                ],
            )
        raise AssertionError(f"Unexpected endpoint {ep}")

    with respx.mock() as mock_router:
        mock_router.post(DEFAULT_INFO_URL).mock(side_effect=handle_info)
        asyncio.run(
            run_daily_publish(
                cfg,
                store,
                data_dir=data_dir,
                schemas_dir=SCHEMAS_DIR,
                now_ms=eval_ts,
                fetch_missing_fills=True,
            )
        )

    eth_old_month = FillsFile.model_validate(
        read_json_file(data_dir / "fills" / "ETH" / "months" / "2026-10.json.gz")
    )
    assert any(r[0] == t_old for r in eth_old_month.fills)
    # BTC old fill at t_old MUST NOT be re-upserted into 2026-10
    btc_old_month = FillsFile.model_validate(
        read_json_file(data_dir / "fills" / "BTC" / "months" / "2026-10.json.gz")
    )
    assert not any(r[0] == t_old for r in btc_old_month.fills)
    btc_new_day = FillsFile.model_validate(
        read_json_file(data_dir / "fills" / "BTC" / "days" / "2026-11-14.json")
    )
    assert any(r[0] == t_new for r in btc_new_day.fills)
    saved_wl = store.load_whitelist_state()
    assert saved_wl is not None
    assert saved_wl.coinBackfillQueue == []

    # 2. Artifact merges data/fills by _fill_key and copies publish_fill_cursors without network
    state_dir2 = tmp_path / "s2"
    data_dir2 = tmp_path / "d2"
    artifact_dir2 = tmp_path / "art2"
    shutil.copytree(SITE_DATA_FIXTURE, data_dir2)
    (artifact_dir2 / "state" / "daily" / "latest_outputs").mkdir(parents=True)
    for fname in ("whitelist.json", "criteria.json", "candidates.json"):
        shutil.copy2(
            SITE_DATA_FIXTURE / fname, artifact_dir2 / "state" / "daily" / "latest_outputs" / fname
        )
    (artifact_dir2 / "state" / "daily" / "publish_fill_cursors.json").write_text(
        json.dumps({"0": t_new}), encoding="utf-8"
    )
    t_hot = utc_date_to_start_ms("2026-11-05") + 3 * HOUR_MS
    existing_day_fills = FillsFile.model_validate(
        read_json_file(data_dir2 / "fills" / "BTC" / "days" / "2026-11-05.json")
    )
    art_fill_row: FillRow = (t_hot, 0, "OL", 95500.0, 2.0, 191000.0, 0)
    write_json_file_atomic(
        artifact_dir2 / "data" / "fills" / "BTC" / "days" / "2026-11-05.json",
        FillsFile(schemaVersion=1, coin="BTC", fills=[art_fill_row]).model_dump(mode="json"),
        compress=False,
    )

    store2 = StateStore(state_dir2, owner="daily")
    with respx.mock():  # zero routes -> fails if any network call attempted
        asyncio.run(
            run_daily_publish(
                cfg,
                store2,
                data_dir=data_dir2,
                schemas_dir=SCHEMAS_DIR,
                artifact_dir=artifact_dir2,
                now_ms=utc_date_to_start_ms("2026-11-05") + 4 * HOUR_MS,
                fetch_missing_fills=False,
            )
        )
    merged_day_fills = FillsFile.model_validate(
        read_json_file(data_dir2 / "fills" / "BTC" / "days" / "2026-11-05.json")
    )
    assert len(merged_day_fills.fills) == len(existing_day_fills.fills) + 1
    assert any(r[0] == t_hot for r in merged_day_fills.fills)
    assert store2.load_daily_publish_fill_cursors() == {0: t_new}


def test_merge_snapshots_to_month_exits_and_reenters_flat_without_phantom() -> None:
    d1_ms = utc_date_to_start_ms("2026-11-01")
    d2_ms = utc_date_to_start_ms("2026-11-02")
    d3_ms = utc_date_to_start_ms("2026-11-03")

    snap1 = _make_daily_snap(
        "2026-11-01",
        coins=["BTC"],
        kf_pos=[(0, 0, 5.0, 95_000.0)],
        pos_changes=[],
        tracked=[(0, d1_ms, d1_ms + 2 * HOUR_MS)],
    )
    snap2 = _make_daily_snap(
        "2026-11-02",
        coins=["BTC"],
        kf_pos=[(1, 0, 1.0, 95_000.0)],
        pos_changes=[],
        tracked=[(1, d2_ms, None)],
    )
    snap3 = _make_daily_snap(
        "2026-11-03",
        coins=["BTC"],
        kf_pos=[(1, 0, 1.0, 95_000.0)],
        pos_changes=[],
        tracked=[(0, d3_ms, None), (1, d3_ms, None)],
    )
    month_snap = merge_snapshots_to_month([snap1, snap2, snap3], sparse_hourly=True)
    verify_snapshot_compaction_equivalence([snap1, snap2, snap3], month_snap)
    assert (0, "BTC") not in replay_snapshot_positions_at(month_snap, d3_ms)


def test_scan_secrets_checks_gzip_and_allows_short_tokens_and_overlay_merges(
    tmp_path: Path,
) -> None:
    # 1. Gzipped secret must fail scan; short byte string ghp_1 must NOT false-positive
    gz_dir = tmp_path / "gz_secret"
    gz_dir.mkdir()
    (gz_dir / "leak.json.gz").write_bytes(
        gzip.compress(b'{"token":"github_pat_1234567890abcdef"}', mtime=0)
    )
    with pytest.raises(GitPublishError, match="credential pattern"):
        scan_directory_for_secrets(gz_dir)

    benign_dir = tmp_path / "benign"
    benign_dir.mkdir()
    (benign_dir / "short.bin").write_bytes(b"prefix ghp_1 suffix")
    scan_directory_for_secrets(benign_dir)

    # 2. apply_scoped_overlay merges fills/days/*.json by key & daily preserves newer fast snapshots
    t_hot_fill_1 = utc_date_to_start_ms("2026-11-05") + HOUR_MS
    t_hot_fill_2 = utc_date_to_start_ms("2026-11-05") + 2 * HOUR_MS
    row1: FillRow = (t_hot_fill_1, 0, "OL", 95_000.0, 1.0, 95_000.0, 0)
    row2: FillRow = (t_hot_fill_2, 1, "OS", 96_000.0, 0.5, 48_000.0, 0)
    owners: tuple[PublishOwner, ...] = ("fast", "daily")

    for owner_name in owners:
        src = tmp_path / f"src_{owner_name}"
        wt = tmp_path / f"wt_{owner_name}"
        shutil.copytree(SITE_DATA_FIXTURE, src)
        shutil.copytree(SITE_DATA_FIXTURE, wt / "data")

        # Put row1 in dst, row2 in src
        write_json_file_atomic(
            wt / "data" / "fills" / "BTC" / "days" / "2026-11-05.json",
            FillsFile(schemaVersion=1, coin="BTC", fills=[row1]).model_dump(mode="json"),
            compress=False,
        )
        write_json_file_atomic(
            src / "fills" / "BTC" / "days" / "2026-11-05.json",
            FillsFile(schemaVersion=1, coin="BTC", fills=[row2]).model_dump(mode="json"),
            compress=False,
        )

        if owner_name == "daily":
            # Make wt/data snapshots newer than src snapshots
            dst_latest = Snapshot.model_validate(
                read_json_file(wt / "data" / "snapshots" / "latest.json")
            )
            newer_t = dst_latest.keyframe.t + 600_000
            newer_latest = dst_latest.model_copy(
                update={
                    "keyframe": SnapshotKeyframe(t=newer_t, pos=dst_latest.keyframe.pos),
                    "series": dst_latest.series.model_copy(update={"t": [newer_t]}),
                }
            )
            write_json_file_atomic(
                wt / "data" / "snapshots" / "latest.json",
                newer_latest.model_dump(mode="json", exclude_none=True),
                compress=False,
            )
            dst_day_path = wt / "data" / "snapshots" / "days" / "2026-11-05.json"
            dst_day = Snapshot.model_validate(read_json_file(dst_day_path))
            newer_day = dst_day.model_copy(
                update={
                    "series": SnapshotSeries(
                        t=[*dst_day.series.t, newer_t],
                        mark=[[*(row), row[-1]] for row in dst_day.series.mark],
                        av=SnapshotAvSeries(
                            addrIds=dst_day.series.av.addrIds,
                            values=[[*(row), row[-1]] for row in dst_day.series.av.values],
                        ),
                    )
                }
            )
            write_json_file_atomic(
                dst_day_path, newer_day.model_dump(mode="json", exclude_none=True), compress=False
            )

        apply_scoped_overlay(
            src,
            wt,
            branch="gh-pages",
            owner=owner_name,
        )

        merged_day_fills = FillsFile.model_validate(
            read_json_file(wt / "data" / "fills" / "BTC" / "days" / "2026-11-05.json")
        )
        assert [r[0] for r in merged_day_fills.fills] == [t_hot_fill_1, t_hot_fill_2]

        if owner_name == "daily":
            after_latest = Snapshot.model_validate(
                read_json_file(wt / "data" / "snapshots" / "latest.json")
            )
            assert after_latest.keyframe.t == dst_latest.keyframe.t + 600_000
            after_day = Snapshot.model_validate(
                read_json_file(wt / "data" / "snapshots" / "days" / "2026-11-05.json")
            )
            assert after_day.series.t[-1] == dst_latest.keyframe.t + 600_000

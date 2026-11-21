# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public ``gh-pages/data/`` site writer, monthly gzip compaction, manifest & daily publish."""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from hyperpod.client.info_api import (
    InfoApiClient,
    InfoApiError,
    aggregate_public_fill_rows,
    normalize_fills,
)
from hyperpod.client.ratelimit import WeightRateLimiter
from hyperpod.config import PipelineConfig
from hyperpod.models.candidates import CandidatesFile
from hyperpod.models.candles import validate_candles
from hyperpod.models.criteria import CriteriaFile
from hyperpod.models.fills import DirCode, FillRow, FillsFile
from hyperpod.models.manifest import (
    CandleFiles,
    CoinTrack,
    FillFiles,
    Manifest,
    ManifestFiles,
    PipelineStatus,
    StorageInfo,
)
from hyperpod.models.snapshot import (
    MarkResolution,
    PosChange,
    Snapshot,
    SnapshotAvSeries,
    SnapshotKeyframe,
    SnapshotSeries,
    StaleEntry,
    TrackedInterval,
)
from hyperpod.models.whitelist import Whitelist
from hyperpod.scoring.whitelist import WhitelistStateRecord
from hyperpod.storage.formats import (
    quantize_notional,
    quantize_price,
    quantize_szi,
    read_json_file,
    replay_snapshot_positions_at,
    write_json_file_atomic,
)
from hyperpod.storage.state_store import StateStore
from hyperpod.util.time import (
    HOUR_MS,
    ms_to_utc_date,
    ms_to_utc_month,
    now_utc_ms,
    recent_utc_dates,
)


def _default_schemas_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "schemas"


def _validate_with_schema(payload: dict[str, Any], schema_path: Path) -> None:
    if not schema_path.is_file():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(payload)


def _fill_key(row: FillRow) -> tuple[int, int, DirCode, int]:
    return (int(row[0]), int(row[1]), row[2], int(row[6]))


def _quantize_fill_row(row: FillRow, sz_decimals: int) -> FillRow | None:
    ts = int(row[0])
    addr_id = int(row[1])
    dir_code = row[2]
    px = quantize_price(float(row[3]))
    assert px is not None
    sz = quantize_szi(float(row[4]), sz_decimals)
    if sz <= 0.0:
        return None
    notional = quantize_notional(float(row[5]))
    liq = row[6]
    return (ts, addr_id, dir_code, px, sz, notional, liq)


def upsert_coin_fills(
    data_dir: Path,
    coin: str,
    rows: Iterable[FillRow],
    *,
    hot_days: set[str],
    sz_decimals: int = 5,
    schemas_dir: Path | None = None,
) -> list[Path]:
    """Upsert quantized whitelist fill rows into ``fills/{coin}/days|months`` with dedup."""
    effective_schemas = schemas_dir or _default_schemas_dir()
    fills_schema_path = effective_schemas / "fills.schema.json"
    coin_dir = data_dir / "fills" / coin
    days_dir = coin_dir / "days"
    months_dir = coin_dir / "months"

    # Inspect which dates already live inside monthly compacted archives for mutual exclusion.
    compacted_dates_by_month: dict[str, set[str]] = {}
    if months_dir.is_dir():
        for m_path in sorted(months_dir.glob("*.json.gz")):
            month_key = m_path.name.removesuffix(".json.gz")
            m_file = FillsFile.model_validate(read_json_file(m_path))
            compacted_dates_by_month[month_key] = {ms_to_utc_date(r[0]) for r in m_file.fills}

    day_buckets: dict[str, dict[tuple[int, int, DirCode, int], FillRow]] = defaultdict(dict)
    month_buckets: dict[str, dict[tuple[int, int, DirCode, int], FillRow]] = defaultdict(dict)

    for raw_row in rows:
        q_row = _quantize_fill_row(raw_row, sz_decimals)
        if q_row is None:
            continue
        date_str = ms_to_utc_date(q_row[0])
        month_str = ms_to_utc_month(q_row[0])
        compacted_dates = compacted_dates_by_month.get(month_str, set())
        if date_str in hot_days and date_str not in compacted_dates:
            day_buckets[date_str][_fill_key(q_row)] = q_row
        else:
            month_buckets[month_str][_fill_key(q_row)] = q_row

    written: list[Path] = []
    for date_str, new_map in sorted(day_buckets.items()):
        path = days_dir / f"{date_str}.json"
        merged: dict[tuple[int, int, DirCode, int], FillRow] = {}
        if path.is_file():
            existing = FillsFile.model_validate(read_json_file(path))
            for r in existing.fills:
                merged[_fill_key(r)] = r
        merged.update(new_map)
        ordered = [merged[k] for k in sorted(merged)]
        model = FillsFile(schemaVersion=1, coin=coin, fills=ordered)
        payload = model.model_dump(mode="json")
        _validate_with_schema(payload, fills_schema_path)
        write_json_file_atomic(path, payload, compress=False)
        written.append(path)

    for month_str, new_map in sorted(month_buckets.items()):
        path = months_dir / f"{month_str}.json.gz"
        merged = {}
        if path.is_file():
            existing = FillsFile.model_validate(read_json_file(path))
            for r in existing.fills:
                merged[_fill_key(r)] = r
        merged.update(new_map)
        ordered = [merged[k] for k in sorted(merged)]
        model = FillsFile(schemaVersion=1, coin=coin, fills=ordered)
        payload = model.model_dump(mode="json")
        _validate_with_schema(payload, fills_schema_path)
        write_json_file_atomic(path, payload, compress=True)
        written.append(path)

    return written


def _snapshot_window_end(snap: Snapshot) -> int:
    ts_candidates = [snap.keyframe.t, *snap.series.t, *(chg[0] for chg in snap.posChanges)]
    return max(ts_candidates)


def _coalesce_tracked_intervals(snapshots: Sequence[Snapshot]) -> list[TrackedInterval]:
    raw_by_addr: dict[int, list[tuple[int, int | None]]] = defaultdict(list)
    n_snaps = len(snapshots)

    for idx, snap in enumerate(snapshots):
        win_end = _snapshot_window_end(snap)
        next_snap = snapshots[idx + 1] if idx + 1 < n_snaps else None
        next_tracked_at_kf: set[int] = set()
        if next_snap is not None:
            next_kf_t = next_snap.keyframe.t
            next_tracked_at_kf = {
                aid
                for aid, f_ts, t_ts in next_snap.tracked
                if f_ts <= next_kf_t and (t_ts is None or next_kf_t <= t_ts)
            }

        for aid, from_ts, to_ts in snap.tracked:
            if to_ts is None and next_snap is not None and aid not in next_tracked_at_kf:
                raw_by_addr[aid].append((from_ts, max(from_ts, win_end)))
            else:
                raw_by_addr[aid].append((from_ts, to_ts))

    coalesced: list[TrackedInterval] = []
    for aid in sorted(raw_by_addr):
        intervals = sorted(raw_by_addr[aid], key=lambda item: (item[0], item[1] or 0))
        merged_addr: list[tuple[int, int | None]] = []
        for idx, (f_ts, t_ts) in enumerate(intervals):
            if not merged_addr:
                merged_addr.append((f_ts, t_ts))
                continue
            prev_f, prev_t = merged_addr[-1]
            if prev_t is None:
                # An open interval from an earlier snapshot continues into the next start.
                merged_addr[-1] = (prev_f, t_ts)
                continue
            # Adjacent daily boundaries have at most a 1-day/4h gap when continuous; only bridge
            # if the previous interval came from an open-ended interval capped or exact overlap.
            if f_ts <= prev_t:
                new_end: int | None = None if t_ts is None else max(prev_t, t_ts)
                merged_addr[-1] = (prev_f, new_end)
            else:
                merged_addr.append((f_ts, t_ts))
            _ = idx
        for f_ts, t_ts in merged_addr:
            coalesced.append((aid, f_ts, t_ts))
    return coalesced


def merge_snapshots_to_month(
    snapshots: Sequence[Snapshot],
    *,
    sparse_hourly: bool = True,
) -> Snapshot:
    """Merge chronological snapshots (monthly + daily) into one compacted monthly ``Snapshot``."""
    if not snapshots:
        raise ValueError("snapshots must not be empty")
    ordered = sorted(snapshots, key=lambda s: s.keyframe.t)

    merged_coins: list[str] = []
    coin_to_idx: dict[str, int] = {}
    for snap in ordered:
        for coin in snap.coins:
            if coin not in coin_to_idx:
                coin_to_idx[coin] = len(merged_coins)
                merged_coins.append(coin)

    tracked = _coalesce_tracked_intervals(ordered)

    first = ordered[0]
    curr_pos: dict[tuple[int, str], tuple[float, float | None]] = {
        (aid, first.coins[cid]): (float(szi), float(epx) if epx is not None else None)
        for aid, cid, szi, epx in first.keyframe.pos
    }
    kf_pos = sorted(
        ((aid, coin_to_idx[coin], szi, epx) for (aid, coin), (szi, epx) in curr_pos.items()),
        key=lambda item: (item[0], item[1]),
    )
    keyframe = SnapshotKeyframe(t=first.keyframe.t, pos=kf_pos)

    pos_changes: list[PosChange] = []
    for snap_idx, snap in enumerate(ordered):
        if snap_idx > 0:
            kf_t = snap.keyframe.t
            kf_map: dict[tuple[int, str], tuple[float, float | None]] = {
                (aid, snap.coins[cid]): (float(szi), float(epx) if epx is not None else None)
                for aid, cid, szi, epx in snap.keyframe.pos
            }
            for aid, coin in sorted(list(curr_pos.keys())):
                if (aid, coin) not in kf_map:
                    pos_changes.append((kf_t, aid, coin_to_idx[coin], 0.0, None))
                    curr_pos.pop((aid, coin), None)

            for (aid, coin), (szi, epx) in sorted(kf_map.items()):
                if curr_pos.get((aid, coin)) != (szi, epx):
                    pos_changes.append((kf_t, aid, coin_to_idx[coin], szi, epx))
                    curr_pos[(aid, coin)] = (szi, epx)

        for ev_t, aid, cid, szi, epx in snap.posChanges:
            coin = snap.coins[cid]
            epx_val = float(epx) if epx is not None else None
            szi_val = float(szi)
            pos_changes.append((ev_t, aid, coin_to_idx[coin], szi_val, epx_val))
            if szi_val == 0.0:
                curr_pos.pop((aid, coin), None)
            else:
                curr_pos[(aid, coin)] = (szi_val, epx_val)

    # Merge columnar series (t, mark, av), optionally keeping last point per UTC hour (§3.3).
    points_by_t: dict[int, tuple[dict[str, float | None], dict[int, float | None]]] = {}
    for snap in ordered:
        for t_idx, t_val in enumerate(snap.series.t):
            marks = {
                snap.coins[c_idx]: snap.series.mark[c_idx][t_idx]
                for c_idx in range(len(snap.coins))
            }
            avs = {
                snap.series.av.addrIds[a_idx]: snap.series.av.values[a_idx][t_idx]
                for a_idx in range(len(snap.series.av.addrIds))
            }
            points_by_t[t_val] = (marks, avs)

    if sparse_hourly:
        by_hour: dict[int, int] = {}
        for t_val in sorted(points_by_t):
            by_hour[t_val // HOUR_MS] = t_val
        kept_times = sorted(by_hour.values())
    else:
        kept_times = sorted(points_by_t)

    merged_addr_ids = sorted({aid for snap in ordered for aid in snap.series.av.addrIds})
    mark_rows: list[list[float | None]] = [
        [points_by_t[t_val][0].get(coin) for t_val in kept_times] for coin in merged_coins
    ]
    av_rows: list[list[float | None]] = [
        [points_by_t[t_val][1].get(aid) for t_val in kept_times] for aid in merged_addr_ids
    ]
    series = SnapshotSeries(
        t=kept_times,
        mark=mark_rows,
        av=SnapshotAvSeries(addrIds=merged_addr_ids, values=av_rows),
    )

    stale_by_t: dict[int, set[int]] = defaultdict(set)
    for snap in ordered:
        if snap.stale:
            for s_t, addr_list in snap.stale:
                stale_by_t[s_t].update(addr_list)
    stale: list[StaleEntry] | None = (
        [(t_val, sorted(stale_by_t[t_val])) for t_val in sorted(stale_by_t)] if stale_by_t else None
    )

    recon_flags = [s.reconstructed for s in ordered if s.reconstructed is not None]
    reconstructed: bool | None
    if any(flag is True for flag in recon_flags):
        reconstructed = True
    elif recon_flags:
        reconstructed = False
    else:
        reconstructed = None

    recon_befores = [s.reconstructedBefore for s in ordered if s.reconstructedBefore is not None]
    reconstructed_before = max(recon_befores) if (reconstructed and recon_befores) else None

    mark_resolutions = {s.markResolution for s in ordered if s.markResolution is not None}
    mark_resolution: MarkResolution | None = None
    if len(mark_resolutions) == 1:
        mark_resolution = next(iter(mark_resolutions))
    elif len(mark_resolutions) > 1:
        mark_resolution = "mixed"

    return Snapshot(
        schemaVersion=1,
        coins=merged_coins,
        reconstructed=reconstructed,
        reconstructedBefore=reconstructed_before,
        markResolution=mark_resolution,
        tracked=tracked,
        keyframe=keyframe,
        posChanges=pos_changes,
        series=series,
        stale=stale,
    )


def verify_snapshot_compaction_equivalence(
    daily_snapshots: Sequence[Snapshot],
    monthly_snapshot: Snapshot,
) -> None:
    """Verify exact position-replay equivalence at all ``posChanges``/boundary timestamps (§3.3)."""
    for day_snap in daily_snapshots:
        check_times: set[int] = {day_snap.keyframe.t}
        check_times.update(chg[0] for chg in day_snap.posChanges)
        for _, from_ts, to_ts in day_snap.tracked:
            check_times.add(from_ts)
            if to_ts is not None:
                check_times.add(to_ts)
        if day_snap.series.t:
            check_times.add(day_snap.series.t[-1])
        for t_ms in sorted(check_times):
            expected = replay_snapshot_positions_at(day_snap, t_ms)
            actual = replay_snapshot_positions_at(monthly_snapshot, t_ms)
            if actual != expected:
                raise ValueError(
                    f"Snapshot compaction replay mismatch at t={t_ms}: "
                    f"expected={expected!r}, actual={actual!r}"
                )


@dataclass
class CompactionSummary:
    """Counts of verified >7-day daily-to-monthly compactions."""

    snapshot_days_compacted: int = 0
    snapshot_months_updated: list[str] = field(default_factory=list)
    fill_days_compacted: int = 0
    fill_months_updated: list[str] = field(default_factory=list)


def compact_old_daily_files(
    data_dir: Path,
    cfg: PipelineConfig,
    *,
    now_ms: int,
    schemas_dir: Path | None = None,
) -> CompactionSummary:
    """Move >7d daily snapshots & fills into monthly ``.json.gz`` after verification (§3.3, #5)."""
    effective_schemas = schemas_dir or _default_schemas_dir()
    snap_schema_path = effective_schemas / "snapshot.schema.json"
    fills_schema_path = effective_schemas / "fills.schema.json"
    hot_days = recent_utc_dates(now_ms, cfg.snapshot.hotRetentionDays.value)
    summary = CompactionSummary()

    # 1. Compact old snapshot days -> snapshots/months/YYYY-MM.json.gz
    snap_days_dir = data_dir / "snapshots" / "days"
    snap_months_dir = data_dir / "snapshots" / "months"
    if snap_days_dir.is_dir():
        old_snap_by_month: dict[str, list[Path]] = defaultdict(list)
        for day_path in sorted(snap_days_dir.glob("*.json")):
            if day_path.stem not in hot_days:
                old_snap_by_month[day_path.stem[:7]].append(day_path)

        for month_str, day_paths in sorted(old_snap_by_month.items()):
            month_path = snap_months_dir / f"{month_str}.json.gz"
            daily_snaps = [Snapshot.model_validate(read_json_file(p)) for p in day_paths]
            inputs: list[Snapshot] = []
            if month_path.is_file():
                inputs.append(Snapshot.model_validate(read_json_file(month_path)))
            inputs.extend(daily_snaps)

            merged = merge_snapshots_to_month(inputs, sparse_hourly=True)
            payload = merged.model_dump(mode="json", exclude_none=True)
            # Keep explicitly null reconstructedBefore if reconstructed is True? No, exclude_none
            # omits optional fields cleanly unless needed; wait: keep optional stale/markRes only
            # when non-None, which exclude_none=True handles while keeping non-None required keys!
            # Wait: keyframe.pos/posChanges/tracked/series contain tuple/list Nones inside lists,
            # which exclude_none=True does NOT strip inside tuples/lists!
            _validate_with_schema(payload, snap_schema_path)
            verify_snapshot_compaction_equivalence(daily_snaps, merged)

            write_json_file_atomic(month_path, payload, compress=True)
            read_back_raw = read_json_file(month_path)
            _validate_with_schema(read_back_raw, snap_schema_path)
            read_back_snap = Snapshot.model_validate(read_back_raw)
            verify_snapshot_compaction_equivalence(daily_snaps, read_back_snap)

            for day_path in day_paths:
                day_path.unlink()
            summary.snapshot_days_compacted += len(day_paths)
            summary.snapshot_months_updated.append(month_str)

    # 2. Compact old fill days -> fills/{coin}/months/YYYY-MM.json.gz
    fills_root = data_dir / "fills"
    if fills_root.is_dir():
        for coin_dir in sorted(p for p in fills_root.iterdir() if p.is_dir()):
            coin = coin_dir.name
            days_dir = coin_dir / "days"
            months_dir = coin_dir / "months"
            if not days_dir.is_dir():
                continue
            old_fills_by_month: dict[str, list[Path]] = defaultdict(list)
            for day_path in sorted(days_dir.glob("*.json")):
                if day_path.stem not in hot_days:
                    old_fills_by_month[day_path.stem[:7]].append(day_path)

            for month_str, day_paths in sorted(old_fills_by_month.items()):
                month_path = months_dir / f"{month_str}.json.gz"
                merged_rows: dict[tuple[int, int, DirCode, int], FillRow] = {}
                if month_path.is_file():
                    existing_month = FillsFile.model_validate(read_json_file(month_path))
                    for r in existing_month.fills:
                        merged_rows[_fill_key(r)] = r
                expected_day_rows: dict[tuple[int, int, DirCode, int], FillRow] = {}
                for day_path in day_paths:
                    day_file = FillsFile.model_validate(read_json_file(day_path))
                    for r in day_file.fills:
                        key = _fill_key(r)
                        merged_rows[key] = r
                        expected_day_rows[key] = r

                ordered = [merged_rows[k] for k in sorted(merged_rows)]
                month_model = FillsFile(schemaVersion=1, coin=coin, fills=ordered)
                payload = month_model.model_dump(mode="json")
                _validate_with_schema(payload, fills_schema_path)

                write_json_file_atomic(month_path, payload, compress=True)
                read_back_raw = read_json_file(month_path)
                _validate_with_schema(read_back_raw, fills_schema_path)
                read_back_fills = FillsFile.model_validate(read_back_raw)
                actual_by_key = {_fill_key(r): r for r in read_back_fills.fills}
                for key, exp_row in expected_day_rows.items():
                    if actual_by_key.get(key) != exp_row:
                        raise ValueError(
                            f"Fill compaction verification failed for {coin} {month_str}: "
                            f"missing or altered key {key}"
                        )

                for day_path in day_paths:
                    day_path.unlink()
                summary.fill_days_compacted += len(day_paths)
                summary.fill_months_updated.append(f"{coin}/{month_str}")

    return summary


def _compute_data_bytes_on_disk(data_dir: Path) -> int:
    if not data_dir.exists():
        return 0
    return sum(p.stat().st_size for p in data_dir.rglob("*") if p.is_file())


def rebuild_manifest(
    data_dir: Path,
    cfg: PipelineConfig,
    *,
    whitelist_state: WhitelistStateRecord | None = None,
    now_ms: int | None = None,
    fast_last_ok: int | None = None,
    daily_last_ok: int | None = None,
    extra_warnings: Sequence[str] = (),
    schemas_dir: Path | None = None,
) -> Manifest:
    """Rebuild ``data/manifest.json`` purely from on-disk ``data/`` contents (§3.1)."""
    effective_schemas = schemas_dir or _default_schemas_dir()
    manifest_schema_path = effective_schemas / "manifest.schema.json"
    gen_ts = now_ms if now_ms is not None else now_utc_ms()

    prior_manifest: Manifest | None = None
    manifest_path = data_dir / "manifest.json"
    if manifest_path.is_file():
        prior_manifest = Manifest.model_validate(read_json_file(manifest_path))

    whitelist_path = data_dir / "whitelist.json"
    criteria_path = data_dir / "criteria.json"
    latest_path = data_dir / "snapshots" / "latest.json"

    wl_version = (
        Whitelist.model_validate(read_json_file(whitelist_path)).version
        if whitelist_path.is_file()
        else (prior_manifest.whitelistVersion if prior_manifest else ms_to_utc_date(gen_ts))
    )
    criteria_version = (
        CriteriaFile.model_validate(read_json_file(criteria_path)).current.version
        if criteria_path.is_file()
        else (prior_manifest.criteriaVersion if prior_manifest else 1)
    )
    latest_snapshot_at = (
        Snapshot.model_validate(read_json_file(latest_path)).keyframe.t
        if latest_path.is_file()
        else (prior_manifest.latestSnapshotAt if prior_manifest else gen_ts)
    )

    if whitelist_state is not None and whitelist_state.launchedAt is not None:
        launched_at = whitelist_state.launchedAt
    elif prior_manifest is not None:
        launched_at = prior_manifest.launchedAt
    else:
        launched_at = latest_snapshot_at

    coins: list[CoinTrack]
    if whitelist_state is not None and whitelist_state.coins:
        coins = [track.model_copy() for track in whitelist_state.coins]
    elif prior_manifest is not None and prior_manifest.coins:
        coins = [track.model_copy() for track in prior_manifest.coins]
    else:
        coins = [
            CoinTrack(coin=c, pinned=True, trackedFrom=launched_at, trackedTo=None)
            for c in cfg.coins.pinned.value
        ]

    snap_days = sorted(p.stem for p in (data_dir / "snapshots" / "days").glob("*.json"))
    snap_months = sorted(
        p.name.removesuffix(".json.gz")
        for p in (data_dir / "snapshots" / "months").glob("*.json.gz")
    )

    fills_map: dict[str, FillFiles] = {}
    fills_root = data_dir / "fills"
    if fills_root.is_dir():
        for coin_dir in sorted(p for p in fills_root.iterdir() if p.is_dir()):
            f_days = sorted(p.stem for p in (coin_dir / "days").glob("*.json"))
            f_months = sorted(
                p.name.removesuffix(".json.gz") for p in (coin_dir / "months").glob("*.json.gz")
            )
            fills_map[coin_dir.name] = FillFiles(days=f_days, months=f_months)

    candles_map: dict[str, CandleFiles] = {}
    candles_root = data_dir / "candles"
    if candles_root.is_dir():
        for coin_dir in sorted(p for p in candles_root.iterdir() if p.is_dir()):
            months5m = sorted(
                p.name.removesuffix(".json.gz") for p in (coin_dir / "5m").glob("*.json.gz")
            )
            has_backfill = (coin_dir / "backfill_15m_1h.json.gz").is_file()
            has_overview = (coin_dir / "overview_4h.json.gz").is_file()
            candles_map[coin_dir.name] = CandleFiles(
                months5m=months5m,
                hasBackfill=has_backfill,
                hasOverview4h=has_overview,
            )

    eff_fast_ok = (
        fast_last_ok
        if fast_last_ok is not None
        else (prior_manifest.pipeline.fastLastOk if prior_manifest else None)
    )
    eff_daily_ok = (
        daily_last_ok
        if daily_last_ok is not None
        else (prior_manifest.pipeline.dailyLastOk if prior_manifest else None)
    )

    soft_limit = cfg.snapshot.softLimitBytes.value
    warnings_list = list(dict.fromkeys(extra_warnings))

    # Converge manifest.storage.totalBytesOnDisk to exact byte size including manifest.json.
    total_bytes = _compute_data_bytes_on_disk(data_dir)
    manifest: Manifest | None = None
    for _ in range(5):
        iter_warnings = list(warnings_list)
        if total_bytes > soft_limit:
            size_mb = total_bytes / 1_000_000.0
            limit_mb = soft_limit / 1_000_000.0
            msg = (
                f"gh-pages data/ storage ({size_mb:.1f} MB) exceeds soft limit "
                f"({limit_mb:.1f} MB); consider cold-data offload ADR (§3.3, never auto-deleted)."
            )
            if msg not in iter_warnings:
                iter_warnings.append(msg)

        manifest = Manifest(
            schemaVersion=1,
            generatedAt=gen_ts,
            latestSnapshotAt=latest_snapshot_at,
            launchedAt=launched_at,
            whitelistVersion=wl_version,
            criteriaVersion=criteria_version,
            coins=coins,
            files=ManifestFiles(
                snapshotDays=snap_days,
                snapshotMonths=snap_months,
                fills=fills_map,
                candles=candles_map,
            ),
            storage=StorageInfo(
                totalBytesOnDisk=total_bytes,
                softLimitBytes=soft_limit,
            ),
            pipeline=PipelineStatus(
                fastLastOk=eff_fast_ok,
                dailyLastOk=eff_daily_ok,
                warnings=iter_warnings,
            ),
        )
        payload = manifest.model_dump(mode="json")
        _validate_with_schema(payload, manifest_schema_path)
        write_json_file_atomic(manifest_path, payload, compress=False)
        exact = _compute_data_bytes_on_disk(data_dir)
        if exact == total_bytes:
            break
        total_bytes = exact

    assert manifest is not None
    return manifest


def _copy_tree_overlay(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    for item in src.rglob("*"):
        if item.is_file():
            rel = item.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _merge_fill_file(
    src_path: Path,
    dst_path: Path,
    *,
    schemas_dir: Path | None = None,
) -> int:
    """Merge ``FillsFile`` rows from ``src_path`` into ``dst_path`` deduplicated by fill key."""
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if not dst_path.is_file():
        src_model = FillsFile.model_validate(read_json_file(src_path))
        if schemas_dir is not None:
            _validate_with_schema(
                src_model.model_dump(mode="json"), schemas_dir / "fills.schema.json"
            )
        write_json_file_atomic(
            dst_path,
            src_model.model_dump(mode="json"),
            compress=(dst_path.suffix == ".gz"),
        )
        return len(src_model.fills)

    dst_model = FillsFile.model_validate(read_json_file(dst_path))
    src_model = FillsFile.model_validate(read_json_file(src_path))
    merged = {_fill_key(row): row for row in dst_model.fills}
    for row in src_model.fills:
        merged[_fill_key(row)] = row
    ordered = [merged[k] for k in sorted(merged)]
    merged_model = FillsFile(schemaVersion=1, coin=src_model.coin, fills=ordered)
    payload = merged_model.model_dump(mode="json")
    if schemas_dir is not None:
        _validate_with_schema(payload, schemas_dir / "fills.schema.json")
    write_json_file_atomic(dst_path, payload, compress=(dst_path.suffix == ".gz"))
    return len(ordered)


def _merge_fills_tree(
    src_fills_root: Path,
    dst_fills_root: Path,
    *,
    schemas_dir: Path | None = None,
) -> None:
    """Merge ``fills/{coin}/days|months`` files from ``src_fills_root`` into ``dst_fills_root``."""
    if not src_fills_root.is_dir():
        return
    for coin_dir in sorted(p for p in src_fills_root.iterdir() if p.is_dir()):
        coin = coin_dir.name
        if (coin_dir / "days").is_dir():
            for day_path in sorted((coin_dir / "days").glob("*.json")):
                _merge_fill_file(
                    day_path,
                    dst_fills_root / coin / "days" / day_path.name,
                    schemas_dir=schemas_dir,
                )
        if (coin_dir / "months").is_dir():
            for m_path in sorted((coin_dir / "months").glob("*.json.gz")):
                _merge_fill_file(
                    m_path,
                    dst_fills_root / coin / "months" / m_path.name,
                    schemas_dir=schemas_dir,
                )


@dataclass
class PublishRunSummary:
    """Summary returned by ``hyperpod publish``."""

    manifest: Manifest
    compaction: CompactionSummary
    fills_rows_upserted: int
    fills_addresses_polled: int
    warnings: list[str]
    rate_limit_stats: dict[str, object]


async def run_daily_publish(
    cfg: PipelineConfig,
    store: StateStore,
    *,
    data_dir: Path,
    schemas_dir: Path | None = None,
    artifact_dir: Path | None = None,
    now_ms: int | None = None,
    fetch_missing_fills: bool = True,
    local_budget: bool = False,
    info_url: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    rate_limiter: WeightRateLimiter | None = None,
) -> PublishRunSummary:
    """Merge daily artifacts, backfill missing fills, compact >7d & rebuild manifest."""
    effective_schemas = schemas_dir or _default_schemas_dir()
    eval_ts = now_ms if now_ms is not None else now_utc_ms()
    store._require_write_owner("daily")

    if artifact_dir is not None:
        daily_src = (
            artifact_dir / "state" / "daily"
            if (artifact_dir / "state" / "daily").is_dir()
            else artifact_dir / "daily"
        )
        if daily_src.is_dir():
            _copy_tree_overlay(daily_src, store.daily_dir)
        candles_src = (
            artifact_dir / "data" / "candles"
            if (artifact_dir / "data" / "candles").is_dir()
            else artifact_dir / "candles"
        )
        if candles_src.is_dir():
            _copy_tree_overlay(candles_src, data_dir / "candles")
        fills_src = (
            artifact_dir / "data" / "fills"
            if (artifact_dir / "data" / "fills").is_dir()
            else artifact_dir / "fills"
        )
        if fills_src.is_dir():
            _merge_fills_tree(fills_src, data_dir / "fills", schemas_dir=effective_schemas)

    # Copy validated daily output JSONs (whitelist, criteria, candidates) into data_dir.
    data_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("whitelist.json", "criteria.json", "candidates.json"):
        src_path = store.latest_outputs_dir / filename
        dst_path = data_dir / filename
        if src_path.is_file():
            shutil.copy2(src_path, dst_path)
        elif not dst_path.is_file():
            raise FileNotFoundError(
                f"Missing {filename} in both {store.latest_outputs_dir} and {data_dir}"
            )

    whitelist_state = store.load_whitelist_state()
    warnings: list[str] = []
    fills_rows_upserted = 0
    fills_addresses_polled = 0

    budget_per_min = (
        cfg.ratelimit.localBudgetPerMin.value
        if local_budget
        else cfg.ratelimit.ciBudgetPerMin.value
    )
    limiter = rate_limiter or WeightRateLimiter(
        budget_per_min=budget_per_min,
        max_retries=cfg.ratelimit.maxRetries.value,
        halve_cooldown_seconds=float(cfg.ratelimit.halveCooldownSeconds.value),
    )

    if fetch_missing_fills and whitelist_state is not None:
        active_members = sorted(
            (m for m in whitelist_state.addresses.values() if m.is_active),
            key=lambda m: m.id,
        )
        if active_members:
            pub_cursors = store.load_daily_publish_fill_cursors()
            queued_coins = list(whitelist_state.coinBackfillQueue)
            queued_set = set(queued_coins)
            tracked_coins_set = {
                t.coin for t in whitelist_state.coins if t.trackedTo is None
            } | queued_set
            if not tracked_coins_set:
                tracked_coins_set = set(cfg.coins.pinned.value)

            overlap_ms = cfg.ratelimit.fillOverlapMs.value
            hot_days = recent_utc_dates(eval_ts, cfg.snapshot.hotRetentionDays.value)
            client_kwargs: dict[str, Any] = {
                "rate_limiter": limiter,
                "max_concurrency": cfg.ratelimit.maxConcurrency.value,
                "http_client": http_client,
            }
            if info_url is not None:
                client_kwargs["info_url"] = info_url

            async with InfoApiClient(**client_kwargs) as client:
                meta = await client.fetch_meta()
                main_perp_universe = {asset.name for asset in meta.universe}
                sz_decimals_by_coin = {asset.name: asset.szDecimals for asset in meta.universe}

                had_fill_error = False
                for member in active_members:
                    had_prior_cursor = member.id in pub_cursors
                    prior_cursor = pub_cursors.get(member.id, 0)
                    start_time = 0 if (queued_set or not had_prior_cursor) else prior_cursor
                    try:
                        fills_res = await client.fetch_user_fills_by_time(
                            member.address,
                            start_time=start_time,
                            overlap_ms=overlap_ms,
                            main_perp_universe=main_perp_universe,
                        )
                    except (InfoApiError, ValueError) as exc:
                        had_fill_error = True
                        warnings.append(
                            f"Daily publish fill fetch failed for {member.address}: {exc}"
                        )
                        continue
                    fills_addresses_polled += 1
                    warnings.extend(fills_res.warnings)

                    normalized, norm_warnings = normalize_fills(
                        fills_res.fills,
                        member.address,
                        main_perp_universe=main_perp_universe,
                    )
                    warnings.extend(norm_warnings)
                    if queued_set and had_prior_cursor:
                        min_existing_coin_ts = max(0, prior_cursor - overlap_ms)
                        tracked_fills = [
                            f
                            for f in normalized
                            if f.coin in queued_set
                            or (f.coin in tracked_coins_set and f.time >= min_existing_coin_ts)
                        ]
                    else:
                        tracked_fills = [f for f in normalized if f.coin in tracked_coins_set]
                    by_coin = aggregate_public_fill_rows(tracked_fills, member.id)
                    for coin, rows in by_coin.items():
                        upsert_coin_fills(
                            data_dir,
                            coin,
                            rows,
                            hot_days=hot_days,
                            sz_decimals=sz_decimals_by_coin.get(coin, 5),
                            schemas_dir=effective_schemas,
                        )
                        fills_rows_upserted += len(rows)

                    if fills_res.last_cursor_ms is not None:
                        pub_cursors[member.id] = max(
                            pub_cursors.get(member.id, 0), fills_res.last_cursor_ms
                        )
                    elif member.id not in pub_cursors:
                        pub_cursors[member.id] = eval_ts

            store.save_daily_publish_fill_cursors(pub_cursors)
            if queued_coins and not had_fill_error:
                whitelist_state.coinBackfillQueue = []
                store.save_whitelist_state(whitelist_state)

    compaction = compact_old_daily_files(
        data_dir,
        cfg,
        now_ms=eval_ts,
        schemas_dir=effective_schemas,
    )
    manifest = rebuild_manifest(
        data_dir,
        cfg,
        whitelist_state=whitelist_state,
        now_ms=eval_ts,
        daily_last_ok=eval_ts,
        extra_warnings=warnings,
        schemas_dir=effective_schemas,
    )
    rl_stats = limiter.stats_dict()
    store.append_run_log(
        "daily",
        {
            "job": "publish",
            "ts": eval_ts,
            "snapshotDaysCompacted": compaction.snapshot_days_compacted,
            "fillDaysCompacted": compaction.fill_days_compacted,
            "fillsRowsUpserted": fills_rows_upserted,
            "fillsAddressesPolled": fills_addresses_polled,
            "totalBytesOnDisk": manifest.storage.totalBytesOnDisk,
            "warnings": manifest.pipeline.warnings,
            "rateLimitStats": rl_stats,
        },
        max_entries=cfg.stateRetention.runsLogMaxEntries.value,
    )
    return PublishRunSummary(
        manifest=manifest,
        compaction=compaction,
        fills_rows_upserted=fills_rows_upserted,
        fills_addresses_polled=fills_addresses_polled,
        warnings=list(manifest.pipeline.warnings),
        rate_limit_stats=rl_stats,
    )


def _assert_unique_matching_names(label: str, expected: list[str], actual: set[str]) -> None:
    if len(expected) != len(set(expected)):
        raise ValueError(f"Duplicate entries in manifest for {label}: {expected}")
    if set(expected) != actual:
        raise ValueError(
            f"Manifest mismatch for {label}: "
            f"expected {sorted(expected)}, found {sorted(actual)} on disk"
        )


def verify_manifest_matches_disk(data_dir: Path, manifest: Manifest) -> None:
    """Verify manifest file indices and ``totalBytesOnDisk`` match on-disk files."""
    snap_days = {p.stem for p in (data_dir / "snapshots" / "days").glob("*.json")}
    snap_months = {
        p.name.removesuffix(".json.gz")
        for p in (data_dir / "snapshots" / "months").glob("*.json.gz")
    }
    _assert_unique_matching_names("files.snapshotDays", manifest.files.snapshotDays, snap_days)
    _assert_unique_matching_names(
        "files.snapshotMonths", manifest.files.snapshotMonths, snap_months
    )

    fills_root = data_dir / "fills"
    disk_fill_coins = (
        {p.name for p in fills_root.iterdir() if p.is_dir()} if fills_root.exists() else set()
    )
    if set(manifest.files.fills.keys()) != disk_fill_coins:
        raise ValueError(
            f"Manifest fills coins mismatch: manifest={sorted(manifest.files.fills)}, "
            f"disk={sorted(disk_fill_coins)}"
        )
    for coin, fill_files in manifest.files.fills.items():
        coin_dir = fills_root / coin
        actual_days = {p.stem for p in (coin_dir / "days").glob("*.json")}
        actual_months = {
            p.name.removesuffix(".json.gz") for p in (coin_dir / "months").glob("*.json.gz")
        }
        _assert_unique_matching_names(f"files.fills.{coin}.days", fill_files.days, actual_days)
        _assert_unique_matching_names(
            f"files.fills.{coin}.months", fill_files.months, actual_months
        )

    candles_root = data_dir / "candles"
    disk_candle_coins = (
        {p.name for p in candles_root.iterdir() if p.is_dir()} if candles_root.exists() else set()
    )
    if set(manifest.files.candles.keys()) != disk_candle_coins:
        raise ValueError(
            f"Manifest candles coins mismatch: manifest={sorted(manifest.files.candles)}, "
            f"disk={sorted(disk_candle_coins)}"
        )
    for coin, candle_files in manifest.files.candles.items():
        coin_dir = candles_root / coin
        actual_5m = {p.name.removesuffix(".json.gz") for p in (coin_dir / "5m").glob("*.json.gz")}
        _assert_unique_matching_names(
            f"files.candles.{coin}.months5m", candle_files.months5m, actual_5m
        )
        has_backfill = (coin_dir / "backfill_15m_1h.json.gz").is_file()
        if candle_files.hasBackfill != has_backfill:
            raise ValueError(
                f"Manifest candles.{coin}.hasBackfill={candle_files.hasBackfill} "
                f"does not match disk ({has_backfill})"
            )
        has_overview = (coin_dir / "overview_4h.json.gz").is_file()
        if candle_files.hasOverview4h != has_overview:
            raise ValueError(
                f"Manifest candles.{coin}.hasOverview4h={candle_files.hasOverview4h} "
                f"does not match disk ({has_overview})"
            )

    actual_bytes = _compute_data_bytes_on_disk(data_dir)
    if manifest.storage.totalBytesOnDisk != actual_bytes:
        raise ValueError(
            f"Manifest storage.totalBytesOnDisk ({manifest.storage.totalBytesOnDisk}) "
            f"!= actual disk bytes ({actual_bytes})"
        )


def validate_site_data_directory(
    data_dir: Path,
    *,
    schemas_dir: Path | None = None,
    validate_jsonschema: bool = True,
) -> Manifest:
    """Validate all contracts, schemas, disk indices, and day/month mutual exclusion."""
    effective_schemas = schemas_dir or _default_schemas_dir()
    validators: dict[str, Draft202012Validator] = {}
    if validate_jsonschema:
        for name in (
            "manifest",
            "whitelist",
            "criteria",
            "candidates",
            "snapshot",
            "fills",
            "candles",
        ):
            s_path = effective_schemas / f"{name}.schema.json"
            if not s_path.is_file():
                raise FileNotFoundError(f"Schema file not found: {s_path}")
            validators[name] = Draft202012Validator(json.loads(s_path.read_text("utf-8")))

    def _check(schema_name: str, raw: Any) -> Any:
        if validate_jsonschema:
            validators[schema_name].validate(raw)
        return raw

    manifest = Manifest.model_validate(
        _check("manifest", read_json_file(data_dir / "manifest.json"))
    )
    Whitelist.model_validate(_check("whitelist", read_json_file(data_dir / "whitelist.json")))
    CriteriaFile.model_validate(_check("criteria", read_json_file(data_dir / "criteria.json")))
    CandidatesFile.model_validate(
        _check("candidates", read_json_file(data_dir / "candidates.json"))
    )

    Snapshot.model_validate(
        _check("snapshot", read_json_file(data_dir / "snapshots" / "latest.json"))
    )
    snap_day_dates = set(manifest.files.snapshotDays)
    for path in sorted((data_dir / "snapshots" / "days").glob("*.json")):
        Snapshot.model_validate(_check("snapshot", read_json_file(path)))
    for path in sorted((data_dir / "snapshots" / "months").glob("*.json.gz")):
        m_snap = Snapshot.model_validate(_check("snapshot", read_json_file(path)))
        month_dates = {ms_to_utc_date(t) for t in m_snap.series.t}
        overlap = month_dates & snap_day_dates
        if overlap:
            raise ValueError(
                f"Snapshot day/month mutual exclusion violated for dates {sorted(overlap)}"
            )

    for path in sorted((data_dir / "fills").glob("*/days/*.json")):
        FillsFile.model_validate(_check("fills", read_json_file(path)))
    for path in sorted((data_dir / "fills").glob("*/months/*.json.gz")):
        m_fills = FillsFile.model_validate(_check("fills", read_json_file(path)))
        coin_day_dates = (
            set(manifest.files.fills[m_fills.coin].days)
            if m_fills.coin in manifest.files.fills
            else set()
        )
        m_dates = {ms_to_utc_date(row[0]) for row in m_fills.fills}
        overlap = m_dates & coin_day_dates
        if overlap:
            raise ValueError(
                f"Fill day/month mutual exclusion violated for {m_fills.coin}: {sorted(overlap)}"
            )

    for path in sorted((data_dir / "candles").glob("*/*/*.json.gz")):
        validate_candles(_check("candles", read_json_file(path)))
    for path in sorted((data_dir / "candles").glob("*/*.json.gz")):
        validate_candles(_check("candles", read_json_file(path)))

    verify_manifest_matches_disk(data_dir, manifest)
    return manifest

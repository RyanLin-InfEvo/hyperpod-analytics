# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""10-minute fast loop position snapshot and incremental whitelist fills collector (§6.3, §6.4)."""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from hyperpod.client.info_api import (
    InfoApiClient,
    InfoApiError,
    aggregate_public_fill_rows,
    compute_account_equity,
    normalize_fills,
)
from hyperpod.client.ratelimit import WeightRateLimiter
from hyperpod.config import PipelineConfig
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
from hyperpod.snapshot.candles import resolve_effective_tracked_coins
from hyperpod.storage.formats import (
    quantize_av,
    quantize_price,
    quantize_szi,
    read_json_file,
    write_json_file_atomic,
)
from hyperpod.storage.site_writer import rebuild_manifest, upsert_coin_fills
from hyperpod.storage.state_store import (
    LastSnapshotAddressState,
    LastSnapshotStateFile,
    StateStore,
)
from hyperpod.util.time import (
    HOUR_MS,
    ms_to_utc_date,
    now_utc_ms,
    recent_utc_dates,
    utc_day_floor_ms,
)


class SnapshotAbortError(RuntimeError):
    """Raised when fast-loop address failure ratio exceeds ``maxFailedAddressRatio`` (§6.4)."""


def _default_schemas_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "schemas"


def _validate_snapshot_schema(payload: dict[str, Any], schema_path: Path) -> None:
    if not schema_path.is_file():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(payload)


def _close_exited_intervals_in_previous_day(
    data_dir: Path,
    *,
    prev_date: str,
    t_last: int,
    active_ids: set[int],
    schemas_dir: Path,
) -> None:
    prev_path = data_dir / "snapshots" / "days" / f"{prev_date}.json"
    if not prev_path.is_file():
        return
    prev_snap = Snapshot.model_validate(read_json_file(prev_path))
    updated = False
    new_tracked: list[TrackedInterval] = []
    for aid, from_ts, to_ts in prev_snap.tracked:
        if to_ts is None and aid not in active_ids and t_last >= from_ts:
            new_tracked.append((aid, from_ts, t_last))
            updated = True
        else:
            new_tracked.append((aid, from_ts, to_ts))
    if not updated:
        return
    patched = prev_snap.model_copy(update={"tracked": new_tracked})
    payload = patched.model_dump(mode="json", exclude_none=True)
    _validate_snapshot_schema(payload, schemas_dir / "snapshot.schema.json")
    write_json_file_atomic(prev_path, payload, compress=False)


@dataclass
class SnapshotRunSummary:
    """Summary returned by ``hyperpod snapshot``."""

    t_now: int
    date_str: str
    active_count: int
    failed_count: int
    stale_ids: list[int]
    fills_polled_count: int
    fills_rows_upserted: int
    pos_changes_emitted: int
    coins: list[str]
    warnings: list[str] = field(default_factory=list)
    rate_limit_stats: dict[str, object] = field(default_factory=dict)


async def run_snapshot(
    cfg: PipelineConfig,
    store: StateStore,
    *,
    data_dir: Path,
    schemas_dir: Path | None = None,
    now_ms: int | None = None,
    local_budget: bool = False,
    info_url: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    rate_limiter: WeightRateLimiter | None = None,
) -> SnapshotRunSummary:
    """Capture 10-minute position snapshot and conditional incremental fills (§6.3, §6.4)."""
    store._require_write_owner("fast")
    effective_schemas = schemas_dir or _default_schemas_dir()
    snap_schema_path = effective_schemas / "snapshot.schema.json"
    t_now = now_ms if now_ms is not None else now_utc_ms()
    today_str = ms_to_utc_date(t_now)

    whitelist_state = store.load_whitelist_state()
    if whitelist_state is None:
        raise FileNotFoundError(
            f"Missing whitelist_state.json at {store.whitelist_state_path}; "
            "run score or bootstrap first."
        )

    last_state = store.load_last_snapshot_state() or LastSnapshotStateFile()
    fast_cursors = store.load_fast_fill_cursors()

    active_members = sorted(
        (m for m in whitelist_state.addresses.values() if m.is_active),
        key=lambda m: m.id,
    )
    active_ids = {m.id for m in active_members}

    day_snap_path = data_dir / "snapshots" / "days" / f"{today_str}.json"
    existing_day_snap: Snapshot | None = None
    if day_snap_path.is_file():
        existing_day_snap = Snapshot.model_validate(read_json_file(day_snap_path))
        today_coins = list(existing_day_snap.coins)
    else:
        today_coins = resolve_effective_tracked_coins(whitelist_state, cfg, t_now)
    today_coin_set = set(today_coins)
    coin_to_idx = {coin: idx for idx, coin in enumerate(today_coins)}

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

    client_kwargs: dict[str, Any] = {
        "rate_limiter": limiter,
        "max_concurrency": cfg.ratelimit.maxConcurrency.value,
        "http_client": http_client,
    }
    if info_url is not None:
        client_kwargs["info_url"] = info_url

    warnings: list[str] = []
    curr_pos_by_id: dict[int, dict[str, tuple[float, float | None]]] = {}
    av_by_id: dict[int, float | None] = {}
    stale_streak_by_id: dict[int, int] = {}
    failed_ids: list[int] = []
    fills_polled_count = 0
    fills_rows_upserted = 0

    async with InfoApiClient(**client_kwargs) as client:
        meta_ctx = await client.fetch_meta_and_asset_ctxs()
        main_perp_universe = {asset.name for asset in meta_ctx.meta.universe}
        sz_decimals_by_coin = {asset.name: asset.szDecimals for asset in meta_ctx.meta.universe}
        mark_by_coin: dict[str, float | None] = {}
        for asset, ctx in zip(meta_ctx.meta.universe, meta_ctx.asset_ctxs, strict=True):
            raw_mark = float(ctx.markPx)
            mark_by_coin[asset.name] = quantize_price(raw_mark) if raw_mark > 0.0 else None

        for member in active_members:
            prev_addr = last_state.addresses.get(str(member.id))
            try:
                ch = await client.fetch_clearinghouse_state(member.address)
                abstraction = member.abstraction
                if abstraction is None:
                    abstraction = await client.fetch_user_abstraction(member.address)
                spot = (
                    await client.fetch_spot_clearinghouse_state(member.address)
                    if abstraction not in {"disabled", "default"}
                    else None
                )
                av_raw, _, eq_warnings = compute_account_equity(ch, abstraction, spot)
                warnings.extend(eq_warnings)
                av_by_id[member.id] = quantize_av(av_raw)

                pos_map: dict[str, tuple[float, float | None]] = {}
                for ap in ch.assetPositions:
                    coin = ap.position.coin
                    if coin not in today_coin_set:
                        continue
                    sz_dec = sz_decimals_by_coin.get(coin, 5)
                    q_szi = quantize_szi(float(ap.position.szi), sz_dec)
                    if q_szi == 0.0:
                        continue
                    raw_epx = (
                        float(ap.position.entryPx) if ap.position.entryPx is not None else None
                    )
                    q_epx = (
                        quantize_price(raw_epx) if (raw_epx is not None and raw_epx > 0.0) else None
                    )
                    pos_map[coin] = (q_szi, q_epx)
                curr_pos_by_id[member.id] = pos_map
                stale_streak_by_id[member.id] = 0
            except (InfoApiError, ValueError) as exc:
                failed_ids.append(member.id)
                warnings.append(f"Snapshot fetch failed for {member.address}: {exc}")
                curr_pos_by_id[member.id] = (
                    {
                        c: (float(szi), float(epx) if epx is not None else None)
                        for c, (szi, epx) in prev_addr.positions.items()
                        if c in today_coin_set
                    }
                    if prev_addr is not None
                    else {}
                )
                av_by_id[member.id] = None
                new_streak = (prev_addr.staleStreak + 1) if prev_addr is not None else 1
                stale_streak_by_id[member.id] = new_streak
                if new_streak >= cfg.snapshot.staleWarnStreak.value:
                    warnings.append(
                        f"Address {member.address} (id={member.id}) stale for "
                        f"{new_streak} consecutive snapshots"
                    )

        # Abort without publishing if >20% active addresses failed (§6.4).
        if active_members:
            fail_ratio = len(failed_ids) / len(active_members)
            if fail_ratio > cfg.snapshot.maxFailedAddressRatio.value:
                rl_stats = limiter.stats_dict()
                store.append_run_log(
                    "fast",
                    {
                        "job": "snapshot",
                        "status": "aborted",
                        "ts": t_now,
                        "activeCount": len(active_members),
                        "failedCount": len(failed_ids),
                        "failRatio": round(fail_ratio, 4),
                        "warnings": warnings,
                        "rateLimitStats": rl_stats,
                    },
                    max_entries=cfg.stateRetention.runsLogMaxEntries.value,
                )
                raise SnapshotAbortError(
                    f"Aborted snapshot at t={t_now}: {len(failed_ids)}/{len(active_members)} "
                    f"({fail_ratio:.1%}) active addresses failed"
                )

        # Conditional incremental fills fetch only when szi or entryPx changed (§6.3 step 4).
        hot_days = recent_utc_dates(t_now, cfg.snapshot.hotRetentionDays.value)
        failed_set = set(failed_ids)
        for member in active_members:
            if member.id in failed_set:
                continue
            prev_addr = last_state.addresses.get(str(member.id))
            prev_pos = (
                {
                    c: (float(szi), float(epx) if epx is not None else None)
                    for c, (szi, epx) in prev_addr.positions.items()
                    if c in today_coin_set
                }
                if prev_addr is not None
                else {}
            )
            curr_pos = curr_pos_by_id[member.id]
            newly_tracked_non_zero = (prev_addr is None or not prev_addr.tracked) and bool(curr_pos)
            if not newly_tracked_non_zero and curr_pos == prev_pos:
                continue

            cursor_start = fast_cursors.get(member.id, 0)
            try:
                fills_res = await client.fetch_user_fills_by_time(
                    member.address,
                    start_time=cursor_start,
                    overlap_ms=cfg.ratelimit.fillOverlapMs.value,
                    main_perp_universe=main_perp_universe,
                )
            except (InfoApiError, ValueError) as exc:
                warnings.append(f"Fast fill fetch failed for {member.address}: {exc}")
                continue
            fills_polled_count += 1
            warnings.extend(fills_res.warnings)

            normalized, norm_warnings = normalize_fills(
                fills_res.fills,
                member.address,
                main_perp_universe=main_perp_universe,
            )
            warnings.extend(norm_warnings)
            tracked_fills = [f for f in normalized if f.coin in today_coin_set]
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
                fast_cursors[member.id] = max(cursor_start, fills_res.last_cursor_ms)
            elif member.id not in fast_cursors:
                fast_cursors[member.id] = t_now

    # Close exited intervals on the prior day file upon UTC day rollover.
    if (
        existing_day_snap is None
        and last_state.lastDate is not None
        and last_state.lastDate != today_str
        and last_state.tLast is not None
    ):
        _close_exited_intervals_in_previous_day(
            data_dir,
            prev_date=last_state.lastDate,
            t_last=last_state.tLast,
            active_ids=active_ids,
            schemas_dir=effective_schemas,
        )

    pos_changes_emitted = 0
    if existing_day_snap is None:
        tracked: list[TrackedInterval] = [(m.id, t_now, None) for m in active_members]
        kf_pos = [
            (m.id, coin_to_idx[coin], szi, epx)
            for m in active_members
            for coin, (szi, epx) in sorted(curr_pos_by_id[m.id].items())
        ]
        series = SnapshotSeries(
            t=[t_now],
            mark=[[mark_by_coin.get(coin)] for coin in today_coins],
            av=SnapshotAvSeries(
                addrIds=[m.id for m in active_members],
                values=[[av_by_id[m.id]] for m in active_members],
            ),
        )
        stale: list[StaleEntry] | None = [(t_now, sorted(failed_ids))] if failed_ids else None
        day_snap = Snapshot(
            schemaVersion=1,
            coins=today_coins,
            reconstructed=False,
            tracked=tracked,
            keyframe=SnapshotKeyframe(t=t_now, pos=kf_pos),
            posChanges=[],
            series=series,
            stale=stale,
        )
    else:
        if existing_day_snap.series.t and t_now <= existing_day_snap.series.t[-1]:
            raise ValueError(
                f"Snapshot timestamp t_now={t_now} must be strictly greater than "
                f"last series timestamp {existing_day_snap.series.t[-1]}"
            )
        prev_t = (
            existing_day_snap.series.t[-1]
            if existing_day_snap.series.t
            else existing_day_snap.keyframe.t
        )
        raw_prev_by_id: dict[int, dict[str, tuple[float, float | None]]] = defaultdict(dict)
        for aid, cid, szi, epx in existing_day_snap.keyframe.pos:
            if float(szi) != 0.0:
                raw_prev_by_id[aid][existing_day_snap.coins[cid]] = (
                    float(szi),
                    float(epx) if epx is not None else None,
                )
        for _, aid, cid, szi, epx in sorted(existing_day_snap.posChanges, key=lambda c: c[0]):
            c_name = existing_day_snap.coins[cid]
            if float(szi) == 0.0:
                raw_prev_by_id[aid].pop(c_name, None)
            else:
                raw_prev_by_id[aid][c_name] = (
                    float(szi),
                    float(epx) if epx is not None else None,
                )

        tracked = []
        open_tracked_ids: set[int] = set()
        for aid, from_ts, to_ts in existing_day_snap.tracked:
            if to_ts is None:
                if aid in active_ids:
                    tracked.append((aid, from_ts, None))
                    open_tracked_ids.add(aid)
                else:
                    close_ts = max(from_ts, last_state.tLast or prev_t)
                    tracked.append((aid, from_ts, close_ts))
            elif (
                existing_day_snap.reconstructed is True
                and aid in active_ids
                and to_ts >= max(utc_day_floor_ms(t_now), t_now - HOUR_MS)
                and aid not in open_tracked_ids
            ):
                # Seamlessly bridge bootstrap reconstructed day into live tracking on launch day.
                tracked.append((aid, from_ts, None))
                open_tracked_ids.add(aid)
            else:
                tracked.append((aid, from_ts, to_ts))

        for m in active_members:
            if m.id not in open_tracked_ids:
                tracked.append((m.id, t_now, None))
                open_tracked_ids.add(m.id)

        new_pos_changes: list[PosChange] = list(existing_day_snap.posChanges)
        for m in active_members:
            curr_coins = curr_pos_by_id[m.id]
            prev_coins = raw_prev_by_id.get(m.id, {})
            for coin in sorted(set(prev_coins) | set(curr_coins)):
                if curr_coins.get(coin) != prev_coins.get(coin):
                    if coin not in curr_coins:
                        new_pos_changes.append((t_now, m.id, coin_to_idx[coin], 0.0, None))
                    else:
                        szi, epx = curr_coins[coin]
                        new_pos_changes.append((t_now, m.id, coin_to_idx[coin], szi, epx))
                    pos_changes_emitted += 1

        new_t = [*existing_day_snap.series.t, t_now]
        new_mark = [
            [*existing_day_snap.series.mark[c_idx], mark_by_coin.get(coin)]
            for c_idx, coin in enumerate(today_coins)
        ]

        n_prev = len(existing_day_snap.series.t)
        prev_av_rows = {
            aid: list(existing_day_snap.series.av.values[idx])
            for idx, aid in enumerate(existing_day_snap.series.av.addrIds)
        }
        all_av_ids = sorted(set(prev_av_rows) | active_ids)
        new_av_values = [
            [
                *prev_av_rows.get(aid, [None] * n_prev),
                av_by_id[aid] if aid in active_ids else None,
            ]
            for aid in all_av_ids
        ]
        new_series = SnapshotSeries(
            t=new_t,
            mark=new_mark,
            av=SnapshotAvSeries(addrIds=all_av_ids, values=new_av_values),
        )

        new_stale: list[StaleEntry] | None = (
            list(existing_day_snap.stale) if existing_day_snap.stale else None
        )
        if failed_ids:
            new_stale = [*(new_stale or []), (t_now, sorted(failed_ids))]

        reconstructed_flag = existing_day_snap.reconstructed
        reconstructed_before = existing_day_snap.reconstructedBefore
        mark_res: MarkResolution | None = existing_day_snap.markResolution
        if reconstructed_flag is True and mark_res in {"1h", "4h"}:
            mark_res = "mixed"

        day_snap = Snapshot(
            schemaVersion=1,
            coins=today_coins,
            reconstructed=reconstructed_flag,
            reconstructedBefore=reconstructed_before,
            markResolution=mark_res,
            tracked=tracked,
            keyframe=existing_day_snap.keyframe,
            posChanges=new_pos_changes,
            series=new_series,
            stale=new_stale,
        )

    day_payload = day_snap.model_dump(mode="json", exclude_none=True)
    _validate_snapshot_schema(day_payload, snap_schema_path)
    write_json_file_atomic(day_snap_path, day_payload, compress=False)

    # Build snapshots/latest.json (< 30 KB, §3.3).
    open_from_by_id = {
        aid: from_ts
        for aid, from_ts, to_ts in day_snap.tracked
        if to_ts is None and aid in active_ids
    }
    latest_tracked: list[TrackedInterval] = [
        (m.id, open_from_by_id.get(m.id, t_now), None) for m in active_members
    ]
    latest_kf_pos = [
        (m.id, coin_to_idx[coin], szi, epx)
        for m in active_members
        for coin, (szi, epx) in sorted(curr_pos_by_id[m.id].items())
    ]
    latest_snap = Snapshot(
        schemaVersion=1,
        coins=today_coins,
        reconstructed=False,
        tracked=latest_tracked,
        keyframe=SnapshotKeyframe(t=t_now, pos=latest_kf_pos),
        posChanges=[],
        series=SnapshotSeries(
            t=[t_now],
            mark=[[mark_by_coin.get(coin)] for coin in today_coins],
            av=SnapshotAvSeries(
                addrIds=[m.id for m in active_members],
                values=[[av_by_id[m.id]] for m in active_members],
            ),
        ),
        stale=[(t_now, sorted(failed_ids))] if failed_ids else None,
    )
    latest_payload = latest_snap.model_dump(mode="json", exclude_none=True)
    _validate_snapshot_schema(latest_payload, snap_schema_path)
    write_json_file_atomic(data_dir / "snapshots" / "latest.json", latest_payload, compress=False)

    # Ensure daily metadata JSONs exist in data_dir if run before first publish.
    for filename in ("whitelist.json", "criteria.json", "candidates.json"):
        dst_file = data_dir / filename
        src_file = store.latest_outputs_dir / filename
        if not dst_file.is_file() and src_file.is_file():
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)

    rebuild_manifest(
        data_dir,
        cfg,
        whitelist_state=whitelist_state,
        now_ms=t_now,
        fast_last_ok=t_now,
        extra_warnings=warnings,
        schemas_dir=effective_schemas,
    )

    new_last_state = LastSnapshotStateFile(
        tLast=t_now,
        lastDate=today_str,
        coins=today_coins,
        addresses={
            str(m.id): LastSnapshotAddressState(
                addrId=m.id,
                address=m.address,
                tracked=True,
                av=av_by_id[m.id],
                staleStreak=stale_streak_by_id[m.id],
                positions={c: (szi, epx) for c, (szi, epx) in curr_pos_by_id[m.id].items()},
            )
            for m in active_members
        },
    )
    store.save_last_snapshot_state(new_last_state)
    store.save_fast_fill_cursors(fast_cursors)

    rl_stats = limiter.stats_dict()
    store.append_run_log(
        "fast",
        {
            "job": "snapshot",
            "status": "ok",
            "ts": t_now,
            "activeCount": len(active_members),
            "failedCount": len(failed_ids),
            "fillsPolledCount": fills_polled_count,
            "fillsRowsUpserted": fills_rows_upserted,
            "posChangesEmitted": pos_changes_emitted,
            "warnings": warnings,
            "rateLimitStats": rl_stats,
        },
        max_entries=cfg.stateRetention.runsLogMaxEntries.value,
    )
    return SnapshotRunSummary(
        t_now=t_now,
        date_str=today_str,
        active_count=len(active_members),
        failed_count=len(failed_ids),
        stale_ids=sorted(failed_ids),
        fills_polled_count=fills_polled_count,
        fills_rows_upserted=fills_rows_upserted,
        pos_changes_emitted=pos_changes_emitted,
        coins=today_coins,
        warnings=warnings,
        rate_limit_stats=rl_stats,
    )

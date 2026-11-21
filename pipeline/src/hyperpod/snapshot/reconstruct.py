# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pre-launch historical snapshot reconstruction from fills, portfolio & candles (§6.5)."""

from __future__ import annotations

import json
import shutil
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from hyperpod.client.info_api import (
    InfoApiClient,
    NormalizedFill,
    aggregate_public_fill_rows,
    compute_account_equity,
    normalize_fills,
)
from hyperpod.client.ratelimit import WeightRateLimiter
from hyperpod.config import PipelineConfig
from hyperpod.models.api import (
    ClearinghouseStateResponse,
    PortfolioResponse,
    PortfolioWindowName,
    SpotClearinghouseStateResponse,
)
from hyperpod.models.candles import BackfillCandles, SingleIntervalCandles
from hyperpod.models.snapshot import (
    KeyframePos,
    MarkResolution,
    PosChange,
    Snapshot,
    SnapshotAvSeries,
    SnapshotKeyframe,
    SnapshotSeries,
    TrackedInterval,
)
from hyperpod.models.whitelist import MembershipInterval, Whitelist
from hyperpod.scoring.whitelist import WhitelistMemberRecord
from hyperpod.snapshot.candles import FOUR_HOUR_MS, HOUR_MS, resolve_effective_tracked_coins
from hyperpod.storage.formats import (
    quantize_av,
    quantize_price,
    quantize_szi,
    read_json_file,
    write_json_file_atomic,
)
from hyperpod.storage.site_writer import (
    merge_snapshots_to_month,
    rebuild_manifest,
    upsert_coin_fills,
    verify_snapshot_compaction_equivalence,
)
from hyperpod.storage.state_store import StateStore
from hyperpod.util.time import (
    DAY_MS,
    ms_to_utc_date,
    now_utc_ms,
    recent_utc_dates,
    utc_day_floor_ms,
)


def _default_schemas_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "schemas"


def _validate_with_schema(payload: dict[str, Any], schema_path: Path) -> None:
    if not schema_path.is_file():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(payload)


@dataclass
class ReconstructAddressInputs:
    """Live or mocked per-address inputs required for pre-launch historical reconstruction."""

    clearinghouse: ClearinghouseStateResponse
    abstraction: str
    spot: SpotClearinghouseStateResponse | None
    portfolio: PortfolioResponse
    normalized_fills: list[NormalizedFill]
    last_fill_cursor_ms: int | None = None


@dataclass
class CoinTimelineResult:
    """Reconstructed position + entryPx trajectory for one ``(address, coin)``."""

    init_pos: tuple[float, float | None]
    events: list[tuple[int, float, float | None]]
    final_szi: float
    expected_szi: float
    mismatch_warning: str | None = None


def reconstruct_address_coin_timeline(
    coin_fills: Sequence[NormalizedFill],
    *,
    address: str,
    coin: str,
    eff_start_ms: int,
    launched_at_ms: int,
    current_szi: float,
    current_entry_px: float | None,
    sz_decimals: int,
) -> CoinTimelineResult:
    """Reconstruct ``szi(t)`` & ``entryPx(t)`` for one coin, verifying end ``szi`` (§6.5)."""
    expected_szi_q = quantize_szi(current_szi, sz_decimals)
    expected_epx_q = (
        quantize_price(current_entry_px)
        if (current_entry_px is not None and current_entry_px > 0.0)
        else None
    )

    if not coin_fills:
        # Zero-fill open position constant backfill across [eff_start_ms, launched_at_ms) (§6.5).
        init_pos = (expected_szi_q, expected_epx_q) if expected_szi_q != 0.0 else (0.0, None)
        return CoinTimelineResult(
            init_pos=init_pos,
            events=[],
            final_szi=expected_szi_q,
            expected_szi=expected_szi_q,
            mismatch_warning=None,
        )

    post_launch_delta = sum(
        (f.sz if f.dir_code in ("OL", "CS") else -f.sz)
        for f in coin_fills
        if f.time >= launched_at_ms
    )
    expected_szi_q = quantize_szi(current_szi - post_launch_delta, sz_decimals)

    pre_launch = [f for f in coin_fills if f.time < launched_at_ms]
    if not pre_launch:
        init_pos = (expected_szi_q, expected_epx_q) if expected_szi_q != 0.0 else (0.0, None)
        return CoinTimelineResult(
            init_pos=init_pos,
            events=[],
            final_szi=expected_szi_q,
            expected_szi=expected_szi_q,
            mismatch_warning=None,
        )

    ordered = sorted(pre_launch, key=lambda f: (f.time, f.api_seq_index, f.leg_idx))
    curr_szi = quantize_szi(ordered[0].start_position, sz_decimals)
    curr_epx: float | None = None  # Pre-existing position before first zero-crossing -> null

    init_pos = (curr_szi, curr_epx)
    captured_init = ordered[0].time > eff_start_ms
    events: list[tuple[int, float, float | None]] = []

    idx = 0
    n_fills = len(ordered)
    while idx < n_fills:
        batch_t = ordered[idx].time
        if not captured_init and batch_t > eff_start_ms:
            init_pos = (curr_szi, curr_epx)
            captured_init = True

        prev_state = (curr_szi, curr_epx)
        while idx < n_fills and ordered[idx].time == batch_t:
            fill = ordered[idx]
            idx += 1
            delta = fill.sz if fill.dir_code in ("OL", "CS") else -fill.sz
            new_szi = quantize_szi(curr_szi + delta, sz_decimals)
            if new_szi == 0.0:
                new_epx: float | None = None
            elif curr_szi == 0.0:
                new_epx = quantize_price(fill.px)
            elif curr_szi * new_szi > 0.0:
                if abs(new_szi) > abs(curr_szi) and curr_epx is not None:
                    added = abs(new_szi) - abs(curr_szi)
                    weighted = (abs(curr_szi) * curr_epx + added * fill.px) / abs(new_szi)
                    new_epx = quantize_price(weighted)
                else:
                    new_epx = curr_epx
            else:
                new_epx = quantize_price(fill.px)
            curr_szi = new_szi
            curr_epx = new_epx

        if eff_start_ms < batch_t < launched_at_ms and (curr_szi, curr_epx) != prev_state:
            events.append((batch_t, curr_szi, curr_epx))

    if not captured_init:
        init_pos = (curr_szi, curr_epx)

    tol = 10.0 ** (-(sz_decimals + 1))
    warning: str | None = None
    if abs(curr_szi - expected_szi_q) > tol:
        warning = (
            f"Reconstruct szi mismatch for {address} {coin}: "
            f"reconstructed={curr_szi}, clearinghouse={expected_szi_q}"
        )

    return CoinTimelineResult(
        init_pos=init_pos,
        events=events,
        final_szi=curr_szi,
        expected_szi=expected_szi_q,
        mismatch_warning=warning,
    )


def build_av_curve(
    portfolio: PortfolioResponse,
    *,
    current_equity: float | None = None,
    abstraction: str = "disabled",
) -> list[tuple[int, float]]:
    """Build timestamp-sorted ``accountValue(t)`` curve from portfolio + unified spot offset."""
    by_t: dict[int, float] = {}
    av_windows: tuple[PortfolioWindowName, ...] = (
        "perpAllTime",
        "perpMonth",
        "perpWeek",
        "perpDay",
    )
    for win_name in av_windows:
        win = portfolio.window(win_name)
        for ts, av_str in win.accountValueHistory:
            by_t[int(ts)] = float(av_str)

    curve = sorted(by_t.items(), key=lambda item: item[0])
    if abstraction not in {"disabled", "default"} and current_equity is not None and curve:
        spot_offset = max(0.0, current_equity - curve[-1][1])
        if spot_offset > 0.0:
            curve = [(ts, av + spot_offset) for ts, av in curve]
    return curve


def interpolate_account_value(
    curve: Sequence[tuple[int, float]],
    t_ms: int,
    *,
    eff_start_ms: int,
    fallback_av: float | None = None,
) -> float | None:
    """Linearly interpolate ``accountValue(t)`` for ``t >= eff_start_ms``, else ``None``."""
    if t_ms < eff_start_ms:
        return None
    if not curve:
        return quantize_av(max(0.0, fallback_av)) if fallback_av is not None else None
    if t_ms <= curve[0][0]:
        return quantize_av(max(0.0, curve[0][1]))
    if t_ms >= curve[-1][0]:
        return quantize_av(max(0.0, curve[-1][1]))

    times = [pt[0] for pt in curve]
    idx = bisect_right(times, t_ms)
    t0, av0 = curve[idx - 1]
    t1, av1 = curve[idx]
    val = av1 if t1 <= t0 else av0 + ((t_ms - t0) / (t1 - t0)) * (av1 - av0)
    return quantize_av(max(0.0, val))


@dataclass
class CoinCandleLookup:
    """Binary-searchable archived candle close lookup for 4h (>208d) and 1h/fine (<=208d)."""

    times_4h: list[int]
    closes_4h: list[float]
    times_fine: list[int]
    closes_fine: list[float]

    def mark_at(self, t_ms: int, *, use_4h: bool) -> float | None:
        """Return latest candle close price at or before ``t_ms``, or ``None`` if unavailable."""
        times = self.times_4h if use_4h else self.times_fine
        closes = self.closes_4h if use_4h else self.closes_fine
        idx = bisect_right(times, t_ms) - 1
        if idx < 0:
            return None
        return quantize_price(closes[idx])


def load_coin_candle_lookup(data_dir: Path, coin: str) -> CoinCandleLookup:
    """Load archived 4h, 1h, 15m, and 5m candle closes for ``coin``."""
    coin_dir = data_dir / "candles" / coin
    map_4h: dict[int, float] = {}
    map_fine: dict[int, float] = {}

    ov_path = coin_dir / "overview_4h.json.gz"
    if ov_path.is_file():
        ov = SingleIntervalCandles.model_validate(read_json_file(ov_path))
        for row in ov.c:
            map_4h[int(row[0])] = float(row[4])
            map_fine[int(row[0])] = float(row[4])

    bf_path = coin_dir / "backfill_15m_1h.json.gz"
    if bf_path.is_file():
        bf = BackfillCandles.model_validate(read_json_file(bf_path))
        for row in bf.c1h:
            map_fine[int(row[0])] = float(row[4])
        for row in bf.c15m:
            map_fine[int(row[0])] = float(row[4])

    dir_5m = coin_dir / "5m"
    if dir_5m.is_dir():
        for path_5m in sorted(dir_5m.glob("*.json.gz")):
            c5m = SingleIntervalCandles.model_validate(read_json_file(path_5m))
            for row in c5m.c:
                map_fine[int(row[0])] = float(row[4])

    times_4h = sorted(map_4h)
    closes_4h = [map_4h[t] for t in times_4h]
    times_fine = sorted(map_fine)
    closes_fine = [map_fine[t] for t in times_fine]
    return CoinCandleLookup(
        times_4h=times_4h,
        closes_4h=closes_4h,
        times_fine=times_fine,
        closes_fine=closes_fine,
    )


def _apply_retrospective_membership(
    member: WhitelistMemberRecord,
    *,
    eff_start_ms: int,
    launched_at_ms: int,
) -> None:
    live_ver = 1
    for _, _, ver in reversed(member.membership):
        if ver > 0:
            live_ver = ver
            break

    intervals: list[MembershipInterval] = []
    if eff_start_ms < launched_at_ms:
        intervals.append((eff_start_ms, launched_at_ms, 0))
    intervals.append((launched_at_ms, None, live_ver))
    member.membership = intervals


@dataclass
class ReconstructRunSummary:
    """Summary returned by ``hyperpod reconstruct``."""

    launched_at: int
    reconstruct_start: int
    cutoff_1h: int
    days_reconstructed: int
    hot_days_written: int
    months_written: list[str]
    active_addresses: int
    szi_mismatches: int
    fill_cursors_by_id: dict[int, int]
    fills_rows_upserted: int
    warnings: list[str] = field(default_factory=list)
    rate_limit_stats: dict[str, object] = field(default_factory=dict)


async def run_reconstruct(
    cfg: PipelineConfig,
    store: StateStore,
    *,
    data_dir: Path,
    schemas_dir: Path | None = None,
    launched_at_ms: int | None = None,
    write_fills: bool = True,
    local_budget: bool = True,
    info_url: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    rate_limiter: WeightRateLimiter | None = None,
    prefetched_address_data: Mapping[str, ReconstructAddressInputs] | None = None,
    sz_decimals_override: Mapping[str, int] | None = None,
) -> ReconstructRunSummary:
    """Reconstruct pre-launch snapshots, verify ``szi``, and add ``criteriaVersion=0`` (§6.5)."""
    store._require_write_owner("daily")
    effective_schemas = schemas_dir or _default_schemas_dir()
    snap_schema_path = effective_schemas / "snapshot.schema.json"
    wl_schema_path = effective_schemas / "whitelist.schema.json"

    whitelist_state = store.load_whitelist_state()
    if whitelist_state is None:
        raise FileNotFoundError(
            f"Missing whitelist_state.json at {store.whitelist_state_path}; "
            "run score or backfill first."
        )

    active_members = sorted(
        (m for m in whitelist_state.addresses.values() if m.is_active),
        key=lambda m: m.id,
    )

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

    warnings: list[str] = []
    sz_decimals_by_coin: dict[str, int] = dict(sz_decimals_override or {})
    addr_inputs: dict[str, ReconstructAddressInputs] = {}

    if prefetched_address_data is not None:
        addr_inputs = {k.lower(): v for k, v in prefetched_address_data.items()}
    else:
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
            if sz_decimals_override:
                sz_decimals_by_coin.update(sz_decimals_override)

            for member in active_members:
                addr = member.address.lower()
                ch = await client.fetch_clearinghouse_state(addr)
                abstraction = member.abstraction or await client.fetch_user_abstraction(addr)
                spot = (
                    await client.fetch_spot_clearinghouse_state(addr)
                    if abstraction not in {"disabled", "default"}
                    else None
                )
                portfolio = await client.fetch_portfolio(addr)
                fills_res = await client.fetch_user_fills_by_time(
                    addr,
                    start_time=0,
                    overlap_ms=cfg.ratelimit.fillOverlapMs.value,
                    main_perp_universe=main_perp_universe,
                )
                warnings.extend(fills_res.warnings)
                normalized, norm_warnings = normalize_fills(
                    fills_res.fills,
                    addr,
                    main_perp_universe=main_perp_universe,
                )
                warnings.extend(norm_warnings)
                addr_inputs[addr] = ReconstructAddressInputs(
                    clearinghouse=ch,
                    abstraction=abstraction,
                    spot=spot,
                    portfolio=portfolio,
                    normalized_fills=normalized,
                    last_fill_cursor_ms=fills_res.last_cursor_ms,
                )

    t_launch = launched_at_ms if launched_at_ms is not None else now_utc_ms()
    reconstruct_start_ms = utc_day_floor_ms(t_launch - cfg.snapshot.reconstructDays.value * DAY_MS)
    cutoff_1h_ms = utc_day_floor_ms(t_launch - cfg.snapshot.reconstruct1hDays.value * DAY_MS)
    tracked_coins = resolve_effective_tracked_coins(whitelist_state, cfg, t_launch)
    tracked_coin_set = set(tracked_coins)
    coin_to_idx = {coin: idx for idx, coin in enumerate(tracked_coins)}

    candle_lookups = {coin: load_coin_candle_lookup(data_dir, coin) for coin in tracked_coins}

    hot_days = recent_utc_dates(t_launch, cfg.snapshot.hotRetentionDays.value)
    fill_cursors_by_id: dict[int, int] = {}
    fills_rows_upserted = 0
    szi_mismatches = 0

    eff_start_by_id: dict[int, int] = {}
    av_curve_by_id: dict[int, list[tuple[int, float]]] = {}
    curr_equity_by_id: dict[int, float] = {}

    # Initial positions at eff_start_ms and subsequent events inside [eff_start_ms, t_launch).
    init_pos_by_id_coin: dict[tuple[int, str], tuple[float, float | None]] = {}
    events_by_day: dict[str, list[PosChange]] = defaultdict(list)

    for member in active_members:
        addr = member.address.lower()
        inp = addr_inputs.get(addr)
        if inp is None:
            raise KeyError(f"Missing reconstruction inputs for active whitelist address {addr}")

        eff_start = max(member.coverageStart, reconstruct_start_ms)
        eff_start_by_id[member.id] = eff_start

        equity, _, eq_warnings = compute_account_equity(
            inp.clearinghouse, inp.abstraction, inp.spot
        )
        warnings.extend(eq_warnings)
        curr_equity_by_id[member.id] = max(0.0, equity)
        av_curve_by_id[member.id] = build_av_curve(
            inp.portfolio,
            current_equity=equity,
            abstraction=inp.abstraction,
        )

        # Upsert historical whitelist fills if enabled (§3.4).
        if write_fills:
            tracked_fills = [
                f for f in inp.normalized_fills if f.coin in tracked_coin_set and f.time <= t_launch
            ]
            by_coin_rows = aggregate_public_fill_rows(tracked_fills, member.id)
            for coin, rows in by_coin_rows.items():
                upsert_coin_fills(
                    data_dir,
                    coin,
                    rows,
                    hot_days=hot_days,
                    sz_decimals=sz_decimals_by_coin.get(coin, 5),
                    schemas_dir=effective_schemas,
                )
                fills_rows_upserted += len(rows)

        pre_launch_times = [f.time for f in inp.normalized_fills if f.time <= t_launch]
        has_post_launch = any(f.time > t_launch for f in inp.normalized_fills)
        if has_post_launch:
            fill_cursors_by_id[member.id] = max(pre_launch_times) if pre_launch_times else 0
        elif inp.last_fill_cursor_ms is not None:
            fill_cursors_by_id[member.id] = min(inp.last_fill_cursor_ms, t_launch)
        elif pre_launch_times:
            fill_cursors_by_id[member.id] = max(pre_launch_times)
        else:
            fill_cursors_by_id[member.id] = t_launch

        ch_pos_by_coin: dict[str, tuple[float, float | None]] = {}
        for ap in inp.clearinghouse.assetPositions:
            c_name = ap.position.coin
            if c_name not in tracked_coin_set:
                continue
            c_szi = float(ap.position.szi)
            c_epx = float(ap.position.entryPx) if ap.position.entryPx is not None else None
            ch_pos_by_coin[c_name] = (c_szi, c_epx)

        fills_by_coin: dict[str, list[NormalizedFill]] = defaultdict(list)
        for f in inp.normalized_fills:
            if f.coin in tracked_coin_set:
                fills_by_coin[f.coin].append(f)

        for coin in tracked_coins:
            c_szi, c_epx = ch_pos_by_coin.get(coin, (0.0, None))
            timeline = reconstruct_address_coin_timeline(
                fills_by_coin.get(coin, []),
                address=addr,
                coin=coin,
                eff_start_ms=eff_start,
                launched_at_ms=t_launch,
                current_szi=c_szi,
                current_entry_px=c_epx,
                sz_decimals=sz_decimals_by_coin.get(coin, 5),
            )
            if timeline.mismatch_warning is not None:
                szi_mismatches += 1
                warnings.append(timeline.mismatch_warning)
            if timeline.init_pos[0] != 0.0:
                init_pos_by_id_coin[(member.id, coin)] = timeline.init_pos
            for ev_t, new_szi, new_epx in timeline.events:
                ev_day = ms_to_utc_date(ev_t)
                events_by_day[ev_day].append((ev_t, member.id, coin_to_idx[coin], new_szi, new_epx))

        _apply_retrospective_membership(
            member,
            eff_start_ms=eff_start,
            launched_at_ms=t_launch,
        )

    # Walk chronologically day by day from reconstruct_start_ms up to t_launch.
    running_pos: dict[tuple[int, str], tuple[float, float | None]] = {}
    mid_day_initial_emitted: set[int] = set()
    daily_snapshots: list[tuple[str, Snapshot]] = []

    day_start = reconstruct_start_ms
    while day_start < t_launch:
        day_end = min(day_start + DAY_MS, t_launch)
        is_4h = day_start < cutoff_1h_ms
        step = FOUR_HOUR_MS if is_4h else HOUR_MS
        mark_res: MarkResolution = "4h" if is_4h else "1h"
        t_grid = list(range(day_start, day_end, step))
        if t_grid:
            day_str = ms_to_utc_date(day_start)

            # Activate members whose eff_start < day_end into running_pos before keyframe.
            tracked_rows: list[TrackedInterval] = []
            day_addr_ids: list[int] = []
            day_changes: list[PosChange] = []

            for member in active_members:
                eff_start = eff_start_by_id[member.id]
                if eff_start >= day_end:
                    continue
                day_addr_ids.append(member.id)
                if eff_start <= day_start:
                    if member.id not in mid_day_initial_emitted:
                        for coin in tracked_coins:
                            init_val = init_pos_by_id_coin.get((member.id, coin))
                            if init_val is not None:
                                running_pos[(member.id, coin)] = init_val
                        mid_day_initial_emitted.add(member.id)
                    to_val = t_launch if day_end == t_launch else None
                    tracked_rows.append((member.id, day_start, to_val))
                else:
                    # Mid-day coverageStart: emit non-zero initial positions at eff_start.
                    if member.id not in mid_day_initial_emitted:
                        for coin in tracked_coins:
                            init_val = init_pos_by_id_coin.get((member.id, coin))
                            if init_val is not None:
                                running_pos[(member.id, coin)] = init_val
                                day_changes.append(
                                    (
                                        eff_start,
                                        member.id,
                                        coin_to_idx[coin],
                                        init_val[0],
                                        init_val[1],
                                    )
                                )
                        mid_day_initial_emitted.add(member.id)
                    to_val = t_launch if day_end == t_launch else None
                    tracked_rows.append((member.id, eff_start, to_val))

            kf_pos: list[KeyframePos] = sorted(
                (
                    (aid, coin_to_idx[coin], szi, epx)
                    for (aid, coin), (szi, epx) in running_pos.items()
                    if szi != 0.0 and eff_start_by_id[aid] <= day_start
                ),
                key=lambda item: (item[0], item[1]),
            )

            # Append intra-day fill transitions in chronological order and advance running_pos.
            for chg in sorted(events_by_day.get(day_str, []), key=lambda c: c[0]):
                ev_t, aid, cid, new_szi, new_epx = chg
                if day_start <= ev_t < day_end:
                    day_changes.append(chg)
                    coin_name = tracked_coins[cid]
                    if new_szi == 0.0:
                        running_pos.pop((aid, coin_name), None)
                    else:
                        running_pos[(aid, coin_name)] = (new_szi, new_epx)

            day_changes.sort(key=lambda c: (c[0], c[1], c[2]))

            mark_series = [
                [candle_lookups[coin].mark_at(t_val, use_4h=is_4h) for t_val in t_grid]
                for coin in tracked_coins
            ]
            av_series = [
                [
                    interpolate_account_value(
                        av_curve_by_id[aid],
                        t_val,
                        eff_start_ms=eff_start_by_id[aid],
                        fallback_av=curr_equity_by_id[aid],
                    )
                    for t_val in t_grid
                ]
                for aid in day_addr_ids
            ]

            day_snap = Snapshot(
                schemaVersion=1,
                coins=tracked_coins,
                reconstructed=True,
                reconstructedBefore=t_launch,
                markResolution=mark_res,
                tracked=tracked_rows,
                keyframe=SnapshotKeyframe(t=day_start, pos=kf_pos),
                posChanges=day_changes,
                series=SnapshotSeries(
                    t=t_grid,
                    mark=mark_series,
                    av=SnapshotAvSeries(addrIds=day_addr_ids, values=av_series),
                ),
            )
            daily_snapshots.append((day_str, day_snap))
        day_start += DAY_MS

    # Write hot_days as days/*.json and older days compacted into months/*.json.gz (§3.3, §6.5).
    hot_written = 0
    old_by_month: dict[str, list[Snapshot]] = defaultdict(list)
    for day_str, day_snap in daily_snapshots:
        if day_str in hot_days:
            payload = day_snap.model_dump(mode="json", exclude_none=True)
            _validate_with_schema(payload, snap_schema_path)
            write_json_file_atomic(
                data_dir / "snapshots" / "days" / f"{day_str}.json",
                payload,
                compress=False,
            )
            hot_written += 1
        else:
            old_by_month[day_str[:7]].append(day_snap)

    months_written: list[str] = []
    for month_str, month_days in sorted(old_by_month.items()):
        month_snap = merge_snapshots_to_month(month_days, sparse_hourly=True)
        month_payload = month_snap.model_dump(mode="json", exclude_none=True)
        _validate_with_schema(month_payload, snap_schema_path)
        verify_snapshot_compaction_equivalence(month_days, month_snap)

        month_path = data_dir / "snapshots" / "months" / f"{month_str}.json.gz"
        write_json_file_atomic(month_path, month_payload, compress=True)
        read_back = Snapshot.model_validate(read_json_file(month_path))
        verify_snapshot_compaction_equivalence(month_days, read_back)
        months_written.append(month_str)

    # Persist updated whitelist.json with retrospective criteriaVersion=0 intervals & coin tracks.
    for track in whitelist_state.coins:
        if track.coin in tracked_coin_set:
            track.trackedFrom = min(track.trackedFrom, reconstruct_start_ms)
    if write_fills:
        whitelist_state.coinBackfillQueue = []
    whitelist_state.launchedAt = t_launch
    store.save_whitelist_state(whitelist_state)

    latest_path = data_dir / "snapshots" / "latest.json"
    if daily_snapshots and not latest_path.is_file():
        latest_snap = Snapshot(
            schemaVersion=1,
            coins=tracked_coins,
            reconstructed=False,
            tracked=[(m.id, eff_start_by_id[m.id], None) for m in active_members],
            keyframe=SnapshotKeyframe(
                t=t_launch,
                pos=sorted(
                    (
                        (aid, coin_to_idx[coin], szi, epx)
                        for (aid, coin), (szi, epx) in running_pos.items()
                        if szi != 0.0
                    ),
                    key=lambda item: (item[0], item[1]),
                ),
            ),
            posChanges=[],
            series=SnapshotSeries(
                t=[t_launch],
                mark=[
                    [candle_lookups[coin].mark_at(t_launch, use_4h=False)] for coin in tracked_coins
                ],
                av=SnapshotAvSeries(
                    addrIds=[m.id for m in active_members],
                    values=[[quantize_av(curr_equity_by_id[m.id])] for m in active_members],
                ),
            ),
        )
        latest_payload = latest_snap.model_dump(mode="json", exclude_none=True)
        _validate_with_schema(latest_payload, snap_schema_path)
        write_json_file_atomic(latest_path, latest_payload, compress=False)

    public_whitelist = Whitelist(
        schemaVersion=1,
        version=ms_to_utc_date(t_launch),
        addresses=[
            m.to_public()
            for m in sorted(whitelist_state.addresses.values(), key=lambda rec: rec.id)
        ],
    )
    wl_payload = public_whitelist.model_dump(mode="json", by_alias=True)
    _validate_with_schema(wl_payload, wl_schema_path)
    write_json_file_atomic(data_dir / "whitelist.json", wl_payload, compress=False)
    write_json_file_atomic(store.latest_outputs_dir / "whitelist.json", wl_payload, compress=False)

    for filename in ("criteria.json", "candidates.json"):
        src_file = store.latest_outputs_dir / filename
        dst_file = data_dir / filename
        if src_file.is_file() and not dst_file.is_file():
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)

    rebuild_manifest(
        data_dir,
        cfg,
        whitelist_state=whitelist_state,
        now_ms=t_launch,
        extra_warnings=warnings,
        schemas_dir=effective_schemas,
    )

    rl_stats = limiter.stats_dict()
    store.append_run_log(
        "daily",
        {
            "job": "reconstruct",
            "ts": t_launch,
            "reconstructStart": reconstruct_start_ms,
            "cutoff1h": cutoff_1h_ms,
            "daysReconstructed": len(daily_snapshots),
            "hotDaysWritten": hot_written,
            "monthsWritten": months_written,
            "activeAddresses": len(active_members),
            "sziMismatches": szi_mismatches,
            "fillsRowsUpserted": fills_rows_upserted,
            "warnings": warnings,
            "rateLimitStats": rl_stats,
        },
        max_entries=cfg.stateRetention.runsLogMaxEntries.value,
    )
    return ReconstructRunSummary(
        launched_at=t_launch,
        reconstruct_start=reconstruct_start_ms,
        cutoff_1h=cutoff_1h_ms,
        days_reconstructed=len(daily_snapshots),
        hot_days_written=hot_written,
        months_written=months_written,
        active_addresses=len(active_members),
        szi_mismatches=szi_mismatches,
        fill_cursors_by_id=fill_cursors_by_id,
        fills_rows_upserted=fills_rows_upserted,
        warnings=warnings,
        rate_limit_stats=rl_stats,
    )

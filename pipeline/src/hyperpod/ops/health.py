# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pipeline health check and live snapshot verification utilities (§9 Phase 4, §10)."""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from hyperpod.client.info_api import InfoApiClient, normalize_fills
from hyperpod.client.ratelimit import WeightRateLimiter
from hyperpod.config import PipelineConfig
from hyperpod.models.manifest import Manifest
from hyperpod.models.snapshot import Snapshot
from hyperpod.models.whitelist import Whitelist
from hyperpod.storage.formats import (
    is_addr_tracked_at,
    quantize_szi,
    read_json_file,
    replay_snapshot_positions_at,
)
from hyperpod.util.time import HOUR_MS, MIN_MS, now_utc_ms


@dataclass
class GapWindow:
    """Detected snapshot gap exceeding ``max_gap_minutes``."""

    start_ms: int
    end_ms: int
    gap_minutes: float


@dataclass
class HealthReport:
    """Health evaluation summary across ``fast`` / ``daily`` runs and snapshot cadence."""

    healthy: bool
    evaluated_at: int
    window_hours: float
    window_start: int
    fast_runs_total: int
    fast_runs_succeeded: int
    fast_success_rate: float
    avg_fast_weight: float
    daily_runs_total: int
    daily_runs_failed: int
    max_gap_minutes_observed: float
    gaps: list[GapWindow] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize report to camelCase-friendly JSON dictionary."""
        return {
            "healthy": self.healthy,
            "evaluatedAt": self.evaluated_at,
            "windowHours": self.window_hours,
            "windowStart": self.window_start,
            "fastRunsTotal": self.fast_runs_total,
            "fastRunsSucceeded": self.fast_runs_succeeded,
            "fastSuccessRate": round(self.fast_success_rate, 6),
            "avgFastWeight": round(self.avg_fast_weight, 2),
            "dailyRunsTotal": self.daily_runs_total,
            "dailyRunsFailed": self.daily_runs_failed,
            "maxGapMinutesObserved": round(self.max_gap_minutes_observed, 2),
            "gaps": [asdict(g) for g in self.gaps],
            "reasons": list(self.reasons),
        }


def _read_jsonl_entries(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def _extract_run_weight(entry: dict[str, Any]) -> float:
    rl = entry.get("rateLimitStats")
    if isinstance(rl, dict):
        for key in ("totalWeight", "total_weight_consumed", "total_weight"):
            val = rl.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val)
    for key in ("weightUsed", "totalWeight"):
        val = entry.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
    return 0.0


def evaluate_health(
    state_dir: Path,
    data_dir: Path,
    *,
    hours: float = 24.0,
    now_ms: int | None = None,
    max_gap_minutes: float = 30.0,
    min_success_rate: float = 0.95,
    max_avg_fast_weight: float = 1500.0,
) -> HealthReport:
    """Evaluate fast/daily run success rate, weight budget, and snapshot continuity (§9)."""
    if hours <= 0.0:
        raise ValueError(f"hours must be > 0, got {hours}")
    eval_ts = now_ms if now_ms is not None else now_utc_ms()
    window_start = max(0, eval_ts - int(round(hours * HOUR_MS)))

    fast_entries = [
        e
        for e in _read_jsonl_entries(state_dir / "fast" / "runs.jsonl")
        if isinstance(e.get("ts"), int) and window_start <= int(e["ts"]) <= eval_ts
    ]
    daily_entries = [
        e
        for e in _read_jsonl_entries(state_dir / "daily" / "runs.jsonl")
        if isinstance(e.get("ts"), int) and window_start <= int(e["ts"]) <= eval_ts
    ]

    fast_total = len(fast_entries)
    fast_ok = 0
    fast_weight_sum = 0.0
    for entry in fast_entries:
        status = str(entry.get("status", "ok")).lower()
        if status not in {"aborted", "error", "failed"}:
            fast_ok += 1
        fast_weight_sum += _extract_run_weight(entry)

    fast_success_rate = (fast_ok / fast_total) if fast_total > 0 else 0.0
    avg_fast_weight = (fast_weight_sum / fast_total) if fast_total > 0 else 0.0

    daily_total = len(daily_entries)
    daily_failed = sum(
        1
        for entry in daily_entries
        if str(entry.get("status", "ok")).lower() in {"aborted", "error", "failed"}
    )

    reasons: list[str] = []
    if fast_total == 0:
        reasons.append("No fast snapshot runs found in evaluation window")
    else:
        if fast_success_rate < min_success_rate:
            reasons.append(
                f"Fast success rate {fast_success_rate:.2%} < minimum {min_success_rate:.2%}"
            )
        if avg_fast_weight >= max_avg_fast_weight:
            reasons.append(
                f"Average fast weight {avg_fast_weight:.1f} >= limit {max_avg_fast_weight:.1f}"
            )

    if daily_failed > 0:
        reasons.append(f"{daily_failed} daily pipeline run(s) failed in window")

    # Inspect live snapshot timestamp continuity.
    manifest_path = data_dir / "manifest.json"
    launched_at = 0
    if manifest_path.is_file():
        launched_at = Manifest.model_validate(read_json_file(manifest_path)).launchedAt

    check_start = max(launched_at, window_start)
    max_gap_ms = int(round(max_gap_minutes * MIN_MS))

    live_day_timestamps: list[int] = []
    days_dir = data_dir / "snapshots" / "days"
    if days_dir.is_dir():
        for day_path in sorted(days_dir.glob("*.json")):
            snap = Snapshot.model_validate(read_json_file(day_path))
            for t_val in snap.series.t:
                if check_start <= t_val <= eval_ts:
                    live_day_timestamps.append(t_val)
    live_day_timestamps = sorted(set(live_day_timestamps))

    month_hourly_timestamps: list[int] = []
    months_dir = data_dir / "snapshots" / "months"
    if months_dir.is_dir():
        for m_path in sorted(months_dir.glob("*.json.gz")):
            snap = Snapshot.model_validate(read_json_file(m_path))
            for t_val in snap.series.t:
                if check_start <= t_val <= eval_ts:
                    month_hourly_timestamps.append(t_val)
    month_hourly_timestamps = sorted(set(month_hourly_timestamps))

    gaps: list[GapWindow] = []
    max_gap_obs = 0.0

    def _record_gap(start_t: int, end_t: int, limit_ms: int, *, track_max_obs: bool = True) -> None:
        nonlocal max_gap_obs
        delta = max(0, end_t - start_t)
        mins = delta / MIN_MS
        if (track_max_obs or delta > limit_ms) and mins > max_gap_obs:
            max_gap_obs = mins
        if delta > limit_ms:
            gaps.append(GapWindow(start_ms=start_t, end_ms=end_t, gap_minutes=round(mins, 2)))

    if not live_day_timestamps and not month_hourly_timestamps:
        if eval_ts - check_start > max_gap_ms:
            _record_gap(check_start, eval_ts, max_gap_ms)
    else:
        # Check compacted month section (hourly sparsified -> 65m tolerance) if present before days.
        if month_hourly_timestamps:
            hourly_limit_ms = max(max_gap_ms, 65 * MIN_MS)
            if (
                check_start < month_hourly_timestamps[0]
                and month_hourly_timestamps[0] - check_start > hourly_limit_ms
            ):
                _record_gap(
                    check_start,
                    month_hourly_timestamps[0],
                    hourly_limit_ms,
                    track_max_obs=False,
                )
            for idx in range(len(month_hourly_timestamps) - 1):
                _record_gap(
                    month_hourly_timestamps[idx],
                    month_hourly_timestamps[idx + 1],
                    hourly_limit_ms,
                    track_max_obs=False,
                )
            if live_day_timestamps:
                _record_gap(
                    month_hourly_timestamps[-1],
                    live_day_timestamps[0],
                    hourly_limit_ms,
                    track_max_obs=False,
                )
            else:
                _record_gap(month_hourly_timestamps[-1], eval_ts, max_gap_ms)

        if live_day_timestamps:
            if not month_hourly_timestamps and check_start < live_day_timestamps[0]:
                _record_gap(check_start, live_day_timestamps[0], max_gap_ms)
            for idx in range(len(live_day_timestamps) - 1):
                _record_gap(live_day_timestamps[idx], live_day_timestamps[idx + 1], max_gap_ms)
            _record_gap(live_day_timestamps[-1], eval_ts, max_gap_ms)

    if gaps:
        reasons.append(
            f"Found {len(gaps)} snapshot gap(s) > {max_gap_minutes:.0f}m (max {max_gap_obs:.1f}m)"
        )

    return HealthReport(
        healthy=len(reasons) == 0,
        evaluated_at=eval_ts,
        window_hours=hours,
        window_start=window_start,
        fast_runs_total=fast_total,
        fast_runs_succeeded=fast_ok,
        fast_success_rate=fast_success_rate,
        avg_fast_weight=avg_fast_weight,
        daily_runs_total=daily_total,
        daily_runs_failed=daily_failed,
        max_gap_minutes_observed=max_gap_obs,
        gaps=gaps,
        reasons=reasons,
    )


@dataclass
class CoinCheckDetail:
    """Per-coin reconciliation between published ``latest.json`` + post fills and live API."""

    address: str
    addr_id: int
    coin: str
    published_szi: float
    post_fills_delta: float
    expected_szi: float
    live_szi: float
    diff: float
    matched: bool


@dataclass
class VerifyLiveReport:
    """Summary returned by ``hyperpod verify-live``."""

    passed: bool
    t_snap: int
    sampled_count: int
    sampled_addresses: list[str]
    mismatches: list[CoinCheckDetail] = field(default_factory=list)
    checks: list[CoinCheckDetail] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rate_limit_stats: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize report to JSON-compatible dict."""
        return {
            "passed": self.passed,
            "tSnap": self.t_snap,
            "sampledCount": self.sampled_count,
            "sampledAddresses": self.sampled_addresses,
            "mismatches": [asdict(m) for m in self.mismatches],
            "checksCount": len(self.checks),
            "warnings": self.warnings,
            "rateLimitStats": self.rate_limit_stats,
        }


async def verify_live_positions(
    data_dir: Path,
    cfg: PipelineConfig,
    *,
    sample: int = 3,
    seed: int | None = None,
    info_url: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    rate_limiter: WeightRateLimiter | None = None,
) -> VerifyLiveReport:
    """Verify sampled active whitelist positions against live Hyperliquid API (§9, §10)."""
    if sample < 1:
        raise ValueError(f"sample must be >= 1, got {sample}")

    whitelist = Whitelist.model_validate(read_json_file(data_dir / "whitelist.json"))
    latest = Snapshot.model_validate(read_json_file(data_dir / "snapshots" / "latest.json"))

    t_snap = latest.keyframe.t
    replayed = replay_snapshot_positions_at(latest, t_snap)

    stale_ids = {aid for _, ids in (latest.stale or []) for aid in ids}
    tracked_addrs = [
        addr
        for addr in whitelist.addresses
        if addr.membership[-1][1] is None and is_addr_tracked_at(latest.tracked, addr.id, t_snap)
    ]
    non_stale = [addr for addr in tracked_addrs if addr.id not in stale_ids]
    eligible = non_stale if non_stale else tracked_addrs
    with_pos = [addr for addr in eligible if any(aid == addr.id for aid, _ in replayed)]
    without_pos = [addr for addr in eligible if not any(aid == addr.id for aid, _ in replayed)]

    rng = random.Random(seed)
    rng.shuffle(with_pos)
    rng.shuffle(without_pos)
    chosen = (with_pos + without_pos)[:sample]

    limiter = rate_limiter or WeightRateLimiter(
        budget_per_min=cfg.ratelimit.ciBudgetPerMin.value,
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

    latest_coins = list(latest.coins)
    latest_coin_set = set(latest_coins)
    checks: list[CoinCheckDetail] = []
    mismatches: list[CoinCheckDetail] = []
    warnings: list[str] = []

    async with InfoApiClient(**client_kwargs) as client:
        meta = await client.fetch_meta()
        main_perp_universe = {asset.name for asset in meta.universe}
        sz_decimals_by_coin = {asset.name: asset.szDecimals for asset in meta.universe}

        for member in chosen:
            ch = await client.fetch_clearinghouse_state(member.address)
            fills_res = await client.fetch_user_fills_by_time(
                member.address,
                start_time=t_snap,
                overlap_ms=cfg.ratelimit.fillOverlapMs.value,
                main_perp_universe=main_perp_universe,
            )
            warnings.extend(fills_res.warnings)
            normalized, norm_warnings = normalize_fills(
                fills_res.fills,
                member.address,
                main_perp_universe=main_perp_universe,
            )
            warnings.extend(norm_warnings)

            post_fills = [f for f in normalized if f.time > t_snap and f.coin in latest_coin_set]
            live_by_coin: dict[str, float] = {}
            for ap in ch.assetPositions:
                c = ap.position.coin
                if c in latest_coin_set:
                    live_by_coin[c] = float(ap.position.szi)

            for coin in latest_coins:
                sz_dec = sz_decimals_by_coin.get(coin, 5)
                pub_szi = replayed.get((member.id, coin), (0.0, None))[0]
                live_szi = quantize_szi(live_by_coin.get(coin, 0.0), sz_dec)
                coin_post_fills = sorted(
                    (f for f in post_fills if f.coin == coin),
                    key=lambda f: (f.time, f.api_seq_index, f.leg_idx),
                )
                if not coin_post_fills:
                    candidate_deltas = [0.0]
                else:
                    candidate_deltas = [
                        sum(
                            (f.sz if f.dir_code in ("OL", "CS") else -f.sz)
                            for f in coin_post_fills[k:]
                        )
                        for k in range(len(coin_post_fills) + 1)
                    ]

                best_delta = candidate_deltas[0]
                best_exp_szi = quantize_szi(pub_szi + best_delta, sz_dec)
                best_diff = abs(best_exp_szi - live_szi)
                for cand_delta in candidate_deltas[1:]:
                    cand_exp = quantize_szi(pub_szi + cand_delta, sz_dec)
                    cand_diff = abs(cand_exp - live_szi)
                    if cand_diff < best_diff:
                        best_delta = cand_delta
                        best_exp_szi = cand_exp
                        best_diff = cand_diff

                post_delta = best_delta
                exp_szi = best_exp_szi
                diff = best_diff
                tol = 10.0 ** (-(sz_dec + 1))
                matched = diff <= tol
                detail = CoinCheckDetail(
                    address=member.address,
                    addr_id=member.id,
                    coin=coin,
                    published_szi=pub_szi,
                    post_fills_delta=post_delta,
                    expected_szi=exp_szi,
                    live_szi=live_szi,
                    diff=diff,
                    matched=matched,
                )
                checks.append(detail)
                if not matched:
                    mismatches.append(detail)

    return VerifyLiveReport(
        passed=len(mismatches) == 0,
        t_snap=t_snap,
        sampled_count=len(chosen),
        sampled_addresses=[m.address for m in chosen],
        mismatches=mismatches,
        checks=checks,
        warnings=warnings,
        rate_limit_stats=limiter.stats_dict(),
    )

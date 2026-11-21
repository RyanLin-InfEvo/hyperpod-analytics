# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Permanent candle archiving (5m monthly, 15m/1h backfill, 4h overview) (§3.5, §3.1.1)."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from hyperpod.client.info_api import InfoApiClient, InfoApiError
from hyperpod.client.ratelimit import WeightRateLimiter
from hyperpod.config import PipelineConfig
from hyperpod.models.api import RawCandle
from hyperpod.models.candles import (
    BackfillCandles,
    CandleRow,
    SingleIntervalCandles,
)
from hyperpod.scoring.whitelist import WhitelistStateRecord
from hyperpod.storage.formats import (
    quantize_notional,
    quantize_price,
    read_json_file,
    write_json_file_atomic,
)
from hyperpod.storage.state_store import StateStore
from hyperpod.util.time import (
    DAY_MS,
    ms_to_utc_month,
    now_utc_ms,
    utc_day_floor_ms,
)

FIVE_MIN_MS = 300_000
FIFTEEN_MIN_MS = 900_000
HOUR_MS = 3_600_000
FOUR_HOUR_MS = 14_400_000

_LOOKBACK_5M_DAYS = 17
_LOOKBACK_15M_DAYS = 52
_LOOKBACK_1H_DAYS = 208


def _default_schemas_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "schemas"


def _validate_candles_schema(payload: dict[str, Any], schema_path: Path) -> None:
    if not schema_path.is_file():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(payload)


def quantize_candle_row(raw: RawCandle | CandleRow) -> CandleRow:
    """Convert and quantize a candle row to 6-sig-fig OHLC and volume."""
    if isinstance(raw, RawCandle):
        t, o, h, l_val, c, v = raw.to_candle_row()
    else:
        t, o, h, l_val, c, v = raw
    qo = quantize_price(float(o))
    qc = quantize_price(float(c))
    qh = quantize_price(float(h))
    ql = quantize_price(float(l_val))
    assert qo is not None and qc is not None and qh is not None and ql is not None
    qh = max(qh, qo, qc)
    ql = min(ql, qo, qc)
    qv = quantize_notional(float(v))
    return (int(t), qo, qh, ql, qc, qv)


def upsert_candle_rows(
    existing: Sequence[CandleRow],
    new_rows: Iterable[CandleRow],
) -> list[CandleRow]:
    """Upsert candle tuples keyed by open timestamp ``t``, returning sorted ascending."""
    by_t: dict[int, CandleRow] = {int(row[0]): row for row in existing}
    for row in new_rows:
        by_t[int(row[0])] = row
    return [by_t[t] for t in sorted(by_t)]


def resolve_effective_tracked_coins(
    whitelist_state: WhitelistStateRecord | None,
    cfg: PipelineConfig,
    ts_ms: int,
) -> list[str]:
    """Return coins active on the UTC day containing ``ts_ms`` (pinned first, §3.1.1)."""
    pinned = list(dict.fromkeys(cfg.coins.pinned.value))
    if whitelist_state is None or not whitelist_state.coins:
        return pinned

    day_start = utc_day_floor_ms(ts_ms)
    active_set: set[str] = set()
    for track in whitelist_state.coins:
        if track.trackedFrom <= max(ts_ms, day_start) and (
            track.trackedTo is None or track.trackedTo > day_start
        ):
            active_set.add(track.coin)

    if not active_set:
        return pinned

    ordered = [c for c in pinned if c in active_set]
    dynamic = sorted(c for c in active_set if c not in ordered)
    return [*ordered, *dynamic]


def _find_latest_5m_timestamp(coin_dir: Path) -> int | None:
    dir_5m = coin_dir / "5m"
    if not dir_5m.is_dir():
        return None
    latest_t: int | None = None
    for path in sorted(dir_5m.glob("*.json.gz")):
        model = SingleIntervalCandles.model_validate(read_json_file(path))
        if model.c:
            max_t = model.c[-1][0]
            if latest_t is None or max_t > latest_t:
                latest_t = max_t
    return latest_t


async def archive_coin_candles(
    coin: str,
    *,
    client: InfoApiClient,
    data_dir: Path,
    schemas_dir: Path,
    now_ms: int,
    full_backfill: bool = False,
    include_today_5m: bool = False,
) -> dict[str, int]:
    """Archive 5m monthly candles, 4h overview, and (when needed) 15m/1h backfill (§3.5)."""
    schema_path = schemas_dir / "candles.schema.json"
    coin_dir = data_dir / "candles" / coin
    stats = {"rows5m": 0, "rows15m": 0, "rows1h": 0, "rows4h": 0}

    # 1. 5m monthly archives
    last_5m_t = _find_latest_5m_timestamp(coin_dir)
    start_5m = (
        max(0, now_ms - _LOOKBACK_5M_DAYS * DAY_MS)
        if (full_backfill or last_5m_t is None)
        else last_5m_t
    )
    end_5m = now_ms if include_today_5m else (utc_day_floor_ms(now_ms) - 1)
    if end_5m >= start_5m:
        raw_5m = await client.fetch_candle_snapshot(coin, "5m", start_5m, end_5m)
        rows_5m = [quantize_candle_row(rc) for rc in raw_5m if start_5m <= rc.t <= end_5m]
        by_month: dict[str, list[CandleRow]] = defaultdict(list)
        for row in rows_5m:
            by_month[ms_to_utc_month(row[0])].append(row)
        for month_str, month_new_rows in sorted(by_month.items()):
            month_path = coin_dir / "5m" / f"{month_str}.json.gz"
            existing_rows: list[CandleRow] = []
            if month_path.is_file():
                existing_model = SingleIntervalCandles.model_validate(read_json_file(month_path))
                existing_rows = list(existing_model.c)
            merged_rows = upsert_candle_rows(existing_rows, month_new_rows)
            model = SingleIntervalCandles(
                schemaVersion=1,
                coin=coin,
                interval="5m",
                c=merged_rows,
            )
            payload = model.model_dump(mode="json")
            _validate_candles_schema(payload, schema_path)
            write_json_file_atomic(month_path, payload, compress=True)
        stats["rows5m"] = len(rows_5m)

    # 2. One-time backfill_15m_1h (days 18-52 15m, days 53-208 1h)
    backfill_path = coin_dir / "backfill_15m_1h.json.gz"
    if full_backfill or not backfill_path.is_file():
        cutoff_17d = max(0, now_ms - _LOOKBACK_5M_DAYS * DAY_MS)
        cutoff_52d = max(0, now_ms - _LOOKBACK_15M_DAYS * DAY_MS)
        cutoff_208d = max(0, now_ms - _LOOKBACK_1H_DAYS * DAY_MS)

        raw_15m = await client.fetch_candle_snapshot(coin, "15m", cutoff_52d, cutoff_17d)
        raw_1h = await client.fetch_candle_snapshot(coin, "1h", cutoff_208d, cutoff_52d)

        c15m_new = [quantize_candle_row(rc) for rc in raw_15m if cutoff_52d <= rc.t < cutoff_17d]
        c1h_new = [quantize_candle_row(rc) for rc in raw_1h if cutoff_208d <= rc.t < cutoff_52d]

        existing_15m: list[CandleRow] = []
        existing_1h: list[CandleRow] = []
        if backfill_path.is_file():
            existing_bf = BackfillCandles.model_validate(read_json_file(backfill_path))
            existing_15m = list(existing_bf.c15m)
            existing_1h = list(existing_bf.c1h)

        merged_15m = upsert_candle_rows(existing_15m, c15m_new)
        merged_1h = upsert_candle_rows(existing_1h, c1h_new)
        bf_model = BackfillCandles(
            schemaVersion=1,
            coin=coin,
            interval="15m_1h",
            c15m=merged_15m,
            c1h=merged_1h,
        )
        bf_payload = bf_model.model_dump(mode="json")
        _validate_candles_schema(bf_payload, schema_path)
        write_json_file_atomic(backfill_path, bf_payload, compress=True)
        stats["rows15m"] = len(merged_15m)
        stats["rows1h"] = len(merged_1h)

    # 3. Full-history overview_4h.json.gz
    overview_path = coin_dir / "overview_4h.json.gz"
    existing_4h: list[CandleRow] = []
    start_4h = 0
    if overview_path.is_file():
        existing_ov = SingleIntervalCandles.model_validate(read_json_file(overview_path))
        existing_4h = list(existing_ov.c)
        if not full_backfill and existing_4h:
            start_4h = existing_4h[-1][0]

    raw_4h = await client.fetch_candle_snapshot(coin, "4h", start_4h, now_ms)
    rows_4h = [quantize_candle_row(rc) for rc in raw_4h if rc.t <= now_ms]
    merged_4h = upsert_candle_rows(existing_4h, rows_4h)
    ov_model = SingleIntervalCandles(
        schemaVersion=1,
        coin=coin,
        interval="4h",
        c=merged_4h,
    )
    ov_payload = ov_model.model_dump(mode="json")
    _validate_candles_schema(ov_payload, schema_path)
    write_json_file_atomic(overview_path, ov_payload, compress=True)
    stats["rows4h"] = len(rows_4h)

    return stats


@dataclass
class CandlesRunSummary:
    """Summary returned by ``hyperpod candles``."""

    coins_processed: list[str]
    coin_stats: dict[str, dict[str, int]]
    errors: int = 0
    warnings: list[str] = field(default_factory=list)
    rate_limit_stats: dict[str, object] = field(default_factory=dict)


async def run_candles(
    cfg: PipelineConfig,
    store: StateStore,
    *,
    data_dir: Path,
    schemas_dir: Path | None = None,
    full_backfill: bool = False,
    include_today_5m: bool = False,
    local_budget: bool = False,
    now_ms: int | None = None,
    info_url: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    rate_limiter: WeightRateLimiter | None = None,
) -> CandlesRunSummary:
    """Archive 5m, 4h, and queued 15m/1h backfill candles for tracked coins (§3.5)."""
    effective_schemas = schemas_dir or _default_schemas_dir()
    eval_ts = now_ms if now_ms is not None else now_utc_ms()
    whitelist_state = store.load_whitelist_state()

    tracked_coins = resolve_effective_tracked_coins(whitelist_state, cfg, eval_ts)
    queued_coins: set[str] = set()
    if whitelist_state is not None:
        queued_coins.update(whitelist_state.coinBackfillQueue)
        for track in whitelist_state.coins:
            if track.trackedTo is None and track.coin not in tracked_coins:
                tracked_coins.append(track.coin)
    for q_coin in sorted(queued_coins):
        if q_coin not in tracked_coins:
            tracked_coins.append(q_coin)

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

    coin_stats: dict[str, dict[str, int]] = {}
    warnings: list[str] = []
    errors = 0

    async with InfoApiClient(**client_kwargs) as client:
        for coin in tracked_coins:
            needs_backfill = (
                full_backfill
                or coin in queued_coins
                or not (data_dir / "candles" / coin / "backfill_15m_1h.json.gz").is_file()
            )
            try:
                coin_stats[coin] = await archive_coin_candles(
                    coin,
                    client=client,
                    data_dir=data_dir,
                    schemas_dir=effective_schemas,
                    now_ms=eval_ts,
                    full_backfill=needs_backfill,
                    include_today_5m=include_today_5m,
                )
            except (InfoApiError, ValueError) as exc:
                errors += 1
                warnings.append(f"Candle archive failed for {coin}: {exc}")

    rl_stats = limiter.stats_dict()
    store.append_run_log(
        "daily",
        {
            "job": "candles",
            "ts": eval_ts,
            "coins": list(coin_stats),
            "errors": errors,
            "warnings": warnings,
            "rateLimitStats": rl_stats,
        },
        max_entries=cfg.stateRetention.runsLogMaxEntries.value,
    )
    return CandlesRunSummary(
        coins_processed=list(coin_stats),
        coin_stats=coin_stats,
        errors=errors,
        warnings=warnings,
        rate_limit_stats=rl_stats,
    )

# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate a deterministic, schema-valid synthetic dataset in fixtures/site-data/."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hyperpod.config import PipelineConfig

ROOT = Path(__file__).resolve().parent.parent
SITE_DATA = ROOT / "fixtures" / "site-data"

START_DAY = datetime(2026, 10, 27, 0, 0, tzinfo=UTC)
LAUNCHED_DT = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
V2_DT = datetime(2026, 11, 3, 0, 30, tzinfo=UTC)
EXIT_DT = datetime(2026, 11, 4, 1, 0, tzinfo=UTC)
END_DT = datetime(2026, 11, 5, 23, 50, tzinfo=UTC)

COINS = ["BTC", "ETH"]
SZ_DECIMALS = {"BTC": 5, "ETH": 4}
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
TEN_MIN_MS = 600_000
FIVE_MIN_MS = 300_000
FIFTEEN_MIN_MS = 900_000
FOUR_HOUR_MS = 14_400_000


def ts_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def q_sig(val: float, sig: int) -> float:
    if val == 0.0:
        return 0.0
    return float(f"{val:.{sig}g}")


def q_sz(val: float, coin: str) -> float:
    dec = SZ_DECIMALS[coin]
    rounded = round(val, dec)
    if abs(rounded) < 10.0 ** (-(dec + 2)):
        return 0.0
    return rounded


def btc_price(t_ms: int) -> float:
    hours = (t_ms - ts_ms(datetime(2026, 9, 1, 0, 0, tzinfo=UTC))) / HOUR_MS
    return q_sig(92000.0 + 3500.0 * math.sin(hours / 36.0) + 1200.0 * math.cos(hours / 11.0), 6)


def eth_price(t_ms: int) -> float:
    hours = (t_ms - ts_ms(datetime(2026, 9, 1, 0, 0, tzinfo=UTC))) / HOUR_MS
    return q_sig(3100.0 + 220.0 * math.sin(hours / 28.0) + 80.0 * math.cos(hours / 9.0), 6)


def mark_price(coin: str, t_ms: int) -> float:
    return btc_price(t_ms) if coin == "BTC" else eth_price(t_ms)


def make_candle_bar(coin: str, t_ms: int, step_ms: int) -> list[int | float]:
    o = mark_price(coin, t_ms)
    c = mark_price(coin, t_ms + step_ms)
    mid_a = mark_price(coin, t_ms + step_ms // 3)
    mid_b = mark_price(coin, t_ms + (2 * step_ms) // 3)
    hi = q_sig(max(o, c, mid_a, mid_b) * 1.0012, 6)
    lo = q_sig(min(o, c, mid_a, mid_b) * 0.9988, 6)
    vol = q_sig(80.0 + 40.0 * abs(math.sin(t_ms / 7_200_000.0)), 4)
    return [t_ms, o, hi, lo, c, vol]


def write_json(path: Path, payload: Any, *, compress: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if compress:
        # Deterministic gzip mtime=0 so fixture regeneration is byte-identical.
        path.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
    else:
        path.write_bytes(raw + b"\n")


def build_candles() -> None:
    sep1 = ts_ms(datetime(2026, 9, 1, 0, 0, tzinfo=UTC))
    oct1 = ts_ms(datetime(2026, 10, 1, 0, 0, tzinfo=UTC))
    oct20 = ts_ms(datetime(2026, 10, 20, 0, 0, tzinfo=UTC))
    nov1 = ts_ms(datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
    end_plus = ts_ms(datetime(2026, 11, 6, 0, 0, tzinfo=UTC))

    for coin in COINS:
        overview_rows = [
            make_candle_bar(coin, t, FOUR_HOUR_MS) for t in range(sep1, end_plus, FOUR_HOUR_MS)
        ]
        write_json(
            SITE_DATA / "candles" / coin / "overview_4h.json.gz",
            {"schemaVersion": 1, "coin": coin, "interval": "4h", "c": overview_rows},
            compress=True,
        )

        c1h_rows = [make_candle_bar(coin, t, HOUR_MS) for t in range(sep1, oct1, HOUR_MS)]
        c15m_rows = [
            make_candle_bar(coin, t, FIFTEEN_MIN_MS) for t in range(oct1, oct20, FIFTEEN_MIN_MS)
        ]
        write_json(
            SITE_DATA / "candles" / coin / "backfill_15m_1h.json.gz",
            {
                "schemaVersion": 1,
                "coin": coin,
                "interval": "15m_1h",
                "c15m": c15m_rows,
                "c1h": c1h_rows,
            },
            compress=True,
        )

        c5m_oct = [make_candle_bar(coin, t, FIVE_MIN_MS) for t in range(oct20, nov1, FIVE_MIN_MS)]
        write_json(
            SITE_DATA / "candles" / coin / "5m" / "2026-10.json.gz",
            {"schemaVersion": 1, "coin": coin, "interval": "5m", "c": c5m_oct},
            compress=True,
        )

        c5m_nov = [
            make_candle_bar(coin, t, FIVE_MIN_MS) for t in range(nov1, end_plus, FIVE_MIN_MS)
        ]
        write_json(
            SITE_DATA / "candles" / coin / "5m" / "2026-11.json.gz",
            {"schemaVersion": 1, "coin": coin, "interval": "5m", "c": c5m_nov},
            compress=True,
        )


def _ensure_pipeline_env() -> PipelineConfig:
    pipeline_src = ROOT / "pipeline" / "src"
    if str(pipeline_src) not in sys.path:
        sys.path.insert(0, str(pipeline_src))
    try:
        from hyperpod.config import load_config
    except ModuleNotFoundError:
        venv_dir = ROOT / "pipeline" / ".venv"
        venv_py = venv_dir / "bin" / "python"
        if venv_py.exists() and Path(sys.prefix).resolve() != venv_dir.resolve():
            os.execv(str(venv_py), [str(venv_py), str(Path(__file__).resolve()), *sys.argv[1:]])
        raise
    return load_config(ROOT / "config" / "pipeline.yaml")


def _percentile_ranks(
    values_by_id: dict[int, float | None], *, none_is_top: bool = False
) -> dict[int, float]:
    n = len(values_by_id)
    denom = max(n - 1, 1)
    present = sorted((val, aid) for aid, val in values_by_id.items() if val is not None)
    out: dict[int, float] = {}
    i = 0
    while i < len(present):
        j = i + 1
        while j < len(present) and present[j][0] == present[i][0]:
            j += 1
        avg_rank = (i + j - 1) / 2.0
        pct = 1.0 if n == 1 else avg_rank / denom
        for k in range(i, j):
            out[present[k][1]] = pct
        i = j
    if none_is_top:
        for aid, val in values_by_id.items():
            if val is None:
                out[aid] = 1.0
    return out


def _apply_whitelist_scores(addresses: list[dict[str, Any]], cfg: PipelineConfig) -> None:
    active = [addr for addr in addresses if addr["membership"][-1][1] is None]
    if not active:
        return
    pf_pct = _percentile_ranks(
        {a["id"]: a["metrics"]["profitFactorRobust"] for a in active}, none_is_top=True
    )
    roi_pct = _percentile_ranks({a["id"]: a["metrics"]["roiAllTime"] for a in active})
    mdd_pct = _percentile_ranks({a["id"]: 1.0 - a["metrics"]["maxDrawdown"] for a in active})
    mret_pct = _percentile_ranks(
        {a["id"]: a["metrics"]["pnlMonth"] / a["metrics"]["accountValue"] for a in active}
    )
    wr_pct = _percentile_ranks({a["id"]: a["metrics"]["winRate"] for a in active})
    av_pct = _percentile_ranks(
        {a["id"]: math.log(max(a["metrics"]["accountValue"], 1.0)) for a in active}
    )
    w = cfg.scoring.weights
    for addr in active:
        aid = addr["id"]
        score = (
            w.profitFactorRobust.value * pf_pct[aid]
            + w.roiAllTime.value * roi_pct[aid]
            + w.invMaxDrawdown.value * mdd_pct[aid]
            + w.monthlyReturn.value * mret_pct[aid]
            + w.winRate.value * wr_pct[aid]
            + w.logAccountValue.value * av_pct[aid]
        )
        addr["score"] = round(score, 4)


def build_whitelist(cfg: PipelineConfig) -> dict[str, Any]:
    start_ms = ts_ms(START_DAY)
    cov_1_ms = ts_ms(datetime(2026, 10, 27, 12, 0, tzinfo=UTC))
    cov_4_ms = ts_ms(datetime(2026, 10, 30, 0, 0, tzinfo=UTC))
    launched_ms = ts_ms(LAUNCHED_DT)
    v2_ms = ts_ms(V2_DT)
    exit_ms = ts_ms(EXIT_DT)

    addr0 = "0x1111111111111111111111111111111111111111"
    addr1 = "0x2222222222222222222222222222222222222222"
    addr2 = "0x3333333333333333333333333333333333333333"
    addr3 = "0x4444444444444444444444444444444444444444"
    addr4 = "0x5555555555555555555555555555555555555555"

    addresses = [
        {
            "id": 0,
            "address": addr0,
            "label": "Alpha Master",
            "master": None,
            "subAccounts": [addr1],
            "coverageStart": start_ms,
            "score": 0.91,
            "metrics": {
                "accountValue": 12_500_000.0,
                "equitySource": "perp+spot",
                "pnlAllTime": 4_200_000.0,
                "roiAllTime": 0.61,
                "pnlMonth": 310_000.0,
                "maxDrawdown": 0.18,
                "profitFactor": 2.45,
                "profitFactorRobust": 1.95,
                "winRate": 0.59,
                "nTrades": 143,
                "historyDays": 212.0,
                "fillsPerDay": 24.5,
                "medianHoldMinutes": 540.0,
                "makerRatio": 0.34,
                "turnoverMonth": 6.1,
                "fundingShare": 0.04,
                "topWinShare": 0.19,
                "hedgeRatio": 0.0,
            },
            "membership": [
                [start_ms, launched_ms, 0],
                [launched_ms, v2_ms, 1],
                [v2_ms, None, 2],
            ],
        },
        {
            "id": 1,
            "address": addr1,
            "label": "Alpha Subaccount",
            "master": addr0,
            "subAccounts": [],
            "coverageStart": cov_1_ms,
            "score": 0.78,
            "metrics": {
                "accountValue": 3_200_000.0,
                "equitySource": "perp",
                "pnlAllTime": 980_000.0,
                "roiAllTime": 0.44,
                "pnlMonth": 145_000.0,
                "maxDrawdown": 0.21,
                "profitFactor": 2.05,
                "profitFactorRobust": 1.72,
                "winRate": 0.55,
                "nTrades": 88,
                "historyDays": 120.0,
                "fillsPerDay": 15.0,
                "medianHoldMinutes": 420.0,
                "makerRatio": 0.41,
                "turnoverMonth": 4.8,
                "fundingShare": 0.03,
                "topWinShare": 0.22,
                "hedgeRatio": 0.1,
            },
            "membership": [
                [cov_1_ms, launched_ms, 0],
                [launched_ms, v2_ms, 1],
                [v2_ms, None, 2],
            ],
        },
        {
            "id": 2,
            "address": addr2,
            "label": None,
            "master": None,
            "subAccounts": [],
            "coverageStart": start_ms,
            "score": 0.72,
            "metrics": {
                "accountValue": 5_800_000.0,
                "equitySource": "perp+spot",
                "pnlAllTime": 1_850_000.0,
                "roiAllTime": 0.47,
                "pnlMonth": 92_000.0,
                "maxDrawdown": 0.29,
                "profitFactor": 1.92,
                "profitFactorRobust": 1.61,
                "winRate": 0.52,
                "nTrades": 76,
                "historyDays": 165.0,
                "fillsPerDay": 19.2,
                "medianHoldMinutes": 310.0,
                "makerRatio": 0.22,
                "turnoverMonth": 8.9,
                "fundingShare": 0.07,
                "topWinShare": 0.25,
                "hedgeRatio": 0.0,
            },
            "membership": [
                [start_ms, launched_ms, 0],
                [launched_ms, v2_ms, 1],
                [v2_ms, None, 2],
            ],
        },
        {
            "id": 3,
            "address": addr3,
            "label": "Exited Whale",
            "master": None,
            "subAccounts": [],
            "coverageStart": start_ms,
            "score": 0.54,
            "metrics": {
                "accountValue": 2_100_000.0,
                "equitySource": "perp",
                "pnlAllTime": 640_000.0,
                "roiAllTime": 0.31,
                "pnlMonth": -45_000.0,
                "maxDrawdown": 0.42,
                "profitFactor": 1.65,
                "profitFactorRobust": 1.38,
                "winRate": 0.48,
                "nTrades": 54,
                "historyDays": 95.0,
                "fillsPerDay": 11.4,
                "medianHoldMinutes": 290.0,
                "makerRatio": 0.18,
                "turnoverMonth": 5.3,
                "fundingShare": 0.05,
                "topWinShare": 0.28,
                "hedgeRatio": 0.0,
            },
            "membership": [
                [start_ms, launched_ms, 0],
                [launched_ms, v2_ms, 1],
                [v2_ms, exit_ms, 2],
            ],
        },
        {
            "id": 4,
            "address": addr4,
            "label": "Zero-Loss Newcomer",
            "master": None,
            "subAccounts": [],
            "coverageStart": cov_4_ms,
            "score": 0.96,
            "metrics": {
                "accountValue": 4_400_000.0,
                "equitySource": "perp+spot",
                "pnlAllTime": 1_120_000.0,
                "roiAllTime": 0.34,
                "pnlMonth": 280_000.0,
                "maxDrawdown": 0.06,
                "profitFactor": None,
                "profitFactorRobust": None,
                "winRate": 1.0,
                "nTrades": 28,
                "historyDays": 48.0,
                "fillsPerDay": 9.8,
                "medianHoldMinutes": 720.0,
                "makerRatio": 0.62,
                "turnoverMonth": 3.4,
                "fundingShare": 0.02,
                "topWinShare": 0.16,
                "hedgeRatio": 0.0,
            },
            "membership": [
                [v2_ms, None, 2],
            ],
        },
    ]
    _apply_whitelist_scores(addresses, cfg)
    return {
        "schemaVersion": 1,
        "version": "2026-11-05",
        "addresses": addresses,
    }


def build_criteria(candidates_payload: dict[str, Any], cfg: PipelineConfig) -> dict[str, Any]:
    v1_from = ts_ms(LAUNCHED_DT)
    v2_from = ts_ms(V2_DT)
    h1 = "sha256:" + hashlib.sha256(b"criteria-v1").hexdigest()
    h2 = "sha256:" + hashlib.sha256(b"criteria-v2").hexdigest()

    candidate_rows = candidates_payload["candidates"]
    passed_count = sum(1 for row in candidate_rows if row["passed"])
    rejected_counts: Counter[str] = Counter()
    for row in candidate_rows:
        for reason in row["rejectReasons"]:
            rejected_counts[reason] += 1

    return {
        "schemaVersion": 1,
        "current": {
            "version": 2,
            "effectiveFrom": v2_from,
            "hash": h2,
            "stage1": cfg.export_stage1_rules(),
            "gates": cfg.export_gate_rules(),
            "membership": {
                "exitAfterFailedDays": cfg.membership.exitAfterFailedDays.value,
                "subaccounts": cfg.membership.subaccounts.value,
                "excludeVaults": cfg.membership.excludeVaults.value,
            },
            "scope": {
                "perpDex": cfg.scope.perpDex.value,
                "spot": cfg.scope.spot.value,
                "hip3": cfg.scope.hip3.value,
            },
            "stats": {
                "candidates": len(candidate_rows),
                "passed": passed_count,
                "rejectedBy": dict(sorted(rejected_counts.items())),
            },
        },
        "history": [
            {
                "version": 1,
                "effectiveFrom": v1_from,
                "effectiveTo": v2_from,
                "hash": h1,
                "changes": [
                    {
                        "key": "pf.minRobust",
                        "from": 1.3,
                        "to": 1.5,
                    }
                ],
            }
        ],
    }


def build_candidates() -> dict[str, Any]:
    rows = []
    reject_cases = [
        [],
        [],
        [],
        [],
        ["hft"],
        ["hft", "mm"],
        ["mdd"],
        ["pf"],
        ["role"],
        ["funding"],
        ["hedge"],
        ["passive"],
        ["sample"],
        ["lucky"],
        ["hft", "pf"],
        ["mdd", "pf"],
        ["mm"],
        ["sample", "pf"],
        ["hft", "mdd"],
        ["lucky", "mdd"],
    ]
    for idx, reasons in enumerate(reject_cases):
        passed = len(reasons) == 0
        rows.append(
            {
                "idx": idx,
                "passed": passed,
                "rejectReasons": reasons,
                "fillsPerDay": None if reasons == ["role"] else round(10.0 + idx * 35.0, 2),
                "makerRatio": None
                if reasons == ["role"]
                else round(min(0.95, 0.2 + idx * 0.03), 3),
                "medianHoldMinutes": None
                if reasons == ["role"]
                else (2.5 if "hft" in reasons else 360.0 + idx * 15.0),
                "maxDrawdown": None
                if reasons == ["role"]
                else (0.55 if "mdd" in reasons else 0.18),
                "profitFactorRobust": None
                if reasons == ["role"]
                else (1.1 if "pf" in reasons else 1.85),
                "turnoverMonth": None if reasons == ["role"] else round(4.0 + idx * 2.1, 2),
                "nTrades": None if reasons == ["role"] else (12 if "sample" in reasons else 80),
                "historyDays": None
                if reasons == ["role"]
                else (15.0 if "sample" in reasons else 140.0),
                "fundingShare": None
                if reasons == ["role"]
                else (0.65 if "funding" in reasons else 0.04),
                "hedgeRatio": None
                if reasons == ["role"]
                else (0.9 if "hedge" in reasons else 0.05),
                "topWinShare": None
                if reasons == ["role"]
                else (0.62 if "lucky" in reasons else 0.21),
            }
        )
    return {
        "schemaVersion": 1,
        "generatedAt": ts_ms(END_DT),
        "criteriaVersion": 2,
        "candidates": rows,
    }


def simulate_timeline() -> tuple[
    list[tuple[int, int, int, float, float | None]],
    dict[str, list[list[Any]]],
]:
    """Return chronological position changes and per-coin fills across 10 UTC days."""
    # Events: (t_ms, addrId, coinIdx, new_szi, new_entryPx, fills_for_event)
    # fills_for_event rows: (coin, [t_ms, addrId, dirCode, px, sz, notional, liq])
    events: list[tuple[int, int, int, float, float | None]] = []
    fills: dict[str, list[list[Any]]] = {"BTC": [], "ETH": []}

    def add_fill(
        t_dt: datetime, addr_id: int, coin: str, dir_code: str, sz: float, liq: int = 0
    ) -> float:
        t = ts_ms(t_dt)
        px = mark_price(coin, t)
        sz_q = q_sz(sz, coin)
        notional = q_sig(px * sz_q, 6)
        fills[coin].append([t, addr_id, dir_code, px, sz_q, notional, liq])
        return px

    # 2026-10-27 12:00: addr 1 coverage starts with ETH short -120.0 (pre-existing -> entryPx=None)
    t_cov1 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    events.append((ts_ms(t_cov1), 1, 1, -120.0, None))

    # 2026-10-28 12:00: addr 0 closes pre-existing BTC +4.0 (entryPx was None -> now 0, None)
    t1 = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    add_fill(t1, 0, "BTC", "CL", 4.0)
    events.append((ts_ms(t1), 0, 0, 0.0, None))

    # 2026-10-29 06:00: addr 0 opens fresh BTC +6.5 (now has exact entryPx)
    t2 = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)
    px2 = add_fill(t2, 0, "BTC", "OL", 6.5)
    events.append((ts_ms(t2), 0, 0, 6.5, px2))

    # 2026-10-29 18:00: addr 2 reduces ETH long from +200 to +120
    t3 = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)
    add_fill(t3, 2, "ETH", "CL", 80.0)
    events.append((ts_ms(t3), 2, 1, 120.0, 3050.0))

    # 2026-10-30 10:00: addr 1 opens BTC +2.0 (opposite subaccount hedge / offset example)
    t4 = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
    px4 = add_fill(t4, 1, "BTC", "OL", 2.0)
    events.append((ts_ms(t4), 1, 0, 2.0, px4))

    # 2026-10-31 15:00: addr 3 trims BTC short -5.0 -> -3.0
    t5 = datetime(2026, 10, 31, 15, 0, tzinfo=UTC)
    add_fill(t5, 3, "BTC", "CS", 2.0)
    events.append((ts_ms(t5), 3, 0, -3.0, 95500.0))

    # 2026-11-01 09:20: addr 0 adds BTC +1.5 -> +8.0
    t6 = datetime(2026, 11, 1, 9, 20, tzinfo=UTC)
    px6 = add_fill(t6, 0, "BTC", "OL", 1.5)
    avg6 = q_sig((6.5 * px2 + 1.5 * px6) / 8.0, 6)
    events.append((ts_ms(t6), 0, 0, 8.0, avg6))

    # 2026-11-02 10:20: addr 2 flips BTC +3.0 -> -2.0 (split into CL 3.0 + OS 2.0 at same ms)
    t7 = datetime(2026, 11, 2, 10, 20, tzinfo=UTC)
    add_fill(t7, 2, "BTC", "CL", 3.0)
    px7 = add_fill(t7, 2, "BTC", "OS", 2.0)
    events.append((ts_ms(t7), 2, 0, -2.0, px7))

    # 2026-11-03 00:30: addr 4 joins whitelist with initial ETH +85.0 and opens BTC +3.5
    t8 = V2_DT
    px8_eth = mark_price("ETH", ts_ms(t8))
    events.append((ts_ms(t8), 4, 1, 85.0, px8_eth))
    px8_btc = add_fill(t8, 4, "BTC", "OL", 3.5)
    events.append((ts_ms(t8), 4, 0, 3.5, px8_btc))

    # 2026-11-03 14:30: addr 2 is liquidated on ETH +120.0 -> 0 (CL with liq=1)
    t9 = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)
    add_fill(t9, 2, "ETH", "CL", 120.0, liq=1)
    events.append((ts_ms(t9), 2, 1, 0.0, None))

    # 2026-11-04 16:40: addr 1 flips ETH short -120.0 -> +60.0 (CS + OL at same ms)
    t10 = datetime(2026, 11, 4, 16, 40, tzinfo=UTC)
    add_fill(t10, 1, "ETH", "CS", 120.0)
    px10 = add_fill(t10, 1, "ETH", "OL", 60.0)
    events.append((ts_ms(t10), 1, 1, 60.0, px10))

    # 2026-11-05 11:10: addr 4 adds BTC +1.5 -> +5.0
    t11 = datetime(2026, 11, 5, 11, 10, tzinfo=UTC)
    px11 = add_fill(t11, 4, "BTC", "OL", 1.5)
    avg11 = q_sig((3.5 * px8_btc + 1.5 * px11) / 5.0, 6)
    events.append((ts_ms(t11), 4, 0, 5.0, avg11))

    # 2026-11-05 20:00: addr 0 trims ETH +150.0 -> +100.0 (opened below at t=0)
    t12 = datetime(2026, 11, 5, 20, 0, tzinfo=UTC)
    add_fill(t12, 0, "ETH", "CL", 50.0)
    events.append((ts_ms(t12), 0, 1, 100.0, 3080.0))

    for coin in COINS:
        fills[coin].sort(key=lambda row: (row[0], row[1], row[2], row[6]))
    events.sort(key=lambda row: (row[0], row[1], row[2]))
    return events, fills


def addr_tracked_intervals(addr_id: int) -> tuple[int, int | None]:
    start_ms = ts_ms(START_DAY)
    cov_1_ms = ts_ms(datetime(2026, 10, 27, 12, 0, tzinfo=UTC))
    v2_ms = ts_ms(V2_DT)
    exit_ms = ts_ms(EXIT_DT)
    if addr_id == 1:
        return (cov_1_ms, None)
    if addr_id == 3:
        return (start_ms, exit_ms)
    if addr_id == 4:
        return (v2_ms, None)
    return (start_ms, None)


def base_av(addr_id: int, t_ms: int) -> float:
    base = {0: 12_200_000.0, 1: 3_150_000.0, 2: 5_750_000.0, 3: 2_150_000.0, 4: 4_300_000.0}[
        addr_id
    ]
    hours = (t_ms - ts_ms(START_DAY)) / HOUR_MS
    return q_sig(base + 80_000.0 * math.sin((hours + addr_id * 5) / 14.0), 4)


def build_snapshots_and_fills() -> tuple[dict[str, Any], list[str], list[str]]:
    events, fills = simulate_timeline()

    # Initial positions at 2026-10-27 00:00 UTC (addr 0, 2, 3 active; addr 1 starts at 12:00; addr 4 joins Nov 3)
    state_pos: dict[tuple[int, int], tuple[float, float | None]] = {
        (0, 0): (4.0, None),  # entryPx=None (pre-existing before first zero crossing)
        (0, 1): (150.0, 3080.0),
        (2, 0): (3.0, 94500.0),
        (2, 1): (200.0, 3050.0),
        (3, 0): (-5.0, 95500.0),
    }

    launched_ms = ts_ms(LAUNCHED_DT)
    stale_t = ts_ms(datetime(2026, 11, 4, 0, 40, tzinfo=UTC))

    def is_tracked_at(aid: int, t_ms: int) -> bool:
        a_from, a_to = addr_tracked_intervals(aid)
        return t_ms >= a_from and (a_to is None or t_ms <= a_to)

    def snapshot_window(
        win_start_ms: int,
        win_end_ms: int,
        step_ms: int,
        *,
        reconstructed: bool,
        mark_res: str,
        is_month: bool = False,
    ) -> dict[str, Any]:
        nonlocal state_pos
        kf_pos = [
            [aid, cid, szi, epx]
            for (aid, cid), (szi, epx) in sorted(state_pos.items())
            if szi != 0.0 and is_tracked_at(aid, win_start_ms)
        ]

        # Advance state_pos through events in [win_start_ms, win_end_ms]
        # Note: keyframe captures state at win_start_ms BEFORE events strictly > win_start_ms.
        # Events at == win_start_ms are applied after keyframe if addr just started tracking at win_start_ms?
        # Keep all simulated events off 00:00 boundaries except t_cov1 (12:00) and V2_DT (00:30),
        # so keyframe at 00:00 is strictly unambiguous!
        win_changes: list[list[Any]] = []
        for ev_t, aid, cid, new_szi, new_epx in events:
            if win_start_ms <= ev_t <= win_end_ms:
                state_pos[(aid, cid)] = (new_szi, new_epx)
                if new_szi == 0.0:
                    state_pos.pop((aid, cid), None)
                win_changes.append([ev_t, aid, cid, new_szi, new_epx])

        t_grid = list(range(win_start_ms, win_end_ms + 1, step_ms))
        if is_month:
            # Sparse hourly series: last point of each UTC hour inside [win_start_ms, win_end_ms]
            t_grid = list(range(win_start_ms, win_end_ms + 1, HOUR_MS))

        tracked_rows: list[list[int | None]] = []
        active_addr_ids: list[int] = []
        for aid in range(5):
            a_from, a_to = addr_tracked_intervals(aid)
            if a_to is not None and a_to < win_start_ms:
                continue
            if a_from > win_end_ms:
                continue
            clipped_from = max(a_from, win_start_ms)
            clipped_to = a_to if (a_to is not None and a_to <= win_end_ms) else None
            tracked_rows.append([aid, clipped_from, clipped_to])
            active_addr_ids.append(aid)

        mark_series = [[mark_price(coin, t) for t in t_grid] for coin in COINS]
        av_values: list[list[float | None]] = []
        for aid in active_addr_ids:
            a_from, a_to = addr_tracked_intervals(aid)
            row: list[float | None] = []
            for t in t_grid:
                in_interval = t >= a_from and (a_to is None or t <= a_to)
                if not in_interval or (aid == 3 and t == stale_t):
                    row.append(None)
                else:
                    row.append(base_av(aid, t))
            av_values.append(row)

        payload: dict[str, Any] = {
            "schemaVersion": 1,
            "coins": list(COINS),
            "reconstructed": reconstructed,
            "tracked": tracked_rows,
            "keyframe": {"t": win_start_ms, "pos": kf_pos},
            "posChanges": win_changes,
            "series": {
                "t": t_grid,
                "mark": mark_series,
                "av": {"addrIds": active_addr_ids, "values": av_values},
            },
        }
        if reconstructed:
            payload["reconstructedBefore"] = launched_ms
            payload["markResolution"] = mark_res
        if win_start_ms <= stale_t <= win_end_ms:
            payload["stale"] = [[stale_t, [3]]]
        return payload

    # 1. Monthly compacted snapshot: 2026-10-27 00:00 .. 2026-10-29 23:00
    oct_m_start = ts_ms(datetime(2026, 10, 27, 0, 0, tzinfo=UTC))
    oct_m_end = ts_ms(datetime(2026, 10, 29, 23, 0, tzinfo=UTC))
    oct_month_snap = snapshot_window(
        oct_m_start,
        oct_m_end,
        HOUR_MS,
        reconstructed=True,
        mark_res="1h",
        is_month=True,
    )
    write_json(
        SITE_DATA / "snapshots" / "months" / "2026-10.json.gz", oct_month_snap, compress=True
    )

    # 2. Recent 7 daily snapshots: 2026-10-30 .. 2026-11-05
    snapshot_days: list[str] = []
    for day_offset in range(3, 10):
        day_dt = START_DAY + timedelta(days=day_offset)
        day_str = day_dt.strftime("%Y-%m-%d")
        snapshot_days.append(day_str)
        is_recon = day_dt < LAUNCHED_DT
        step = HOUR_MS if is_recon else TEN_MIN_MS
        win_start = ts_ms(day_dt)
        win_end = win_start + DAY_MS - step
        day_snap = snapshot_window(
            win_start,
            win_end,
            step,
            reconstructed=is_recon,
            mark_res="1h" if is_recon else "10m",
            is_month=False,
        )
        write_json(SITE_DATA / "snapshots" / "days" / f"{day_str}.json", day_snap)

    # 3. snapshots/latest.json at 2026-11-05 23:50 UTC
    end_ms = ts_ms(END_DT)
    latest_addr_ids = [0, 1, 2, 4]
    latest_snap = {
        "schemaVersion": 1,
        "coins": list(COINS),
        "reconstructed": False,
        "tracked": [[aid, *addr_tracked_intervals(aid)] for aid in latest_addr_ids],
        "keyframe": {
            "t": end_ms,
            "pos": [
                [aid, cid, szi, epx]
                for (aid, cid), (szi, epx) in sorted(state_pos.items())
                if szi != 0.0 and is_tracked_at(aid, end_ms)
            ],
        },
        "posChanges": [],
        "series": {
            "t": [end_ms],
            "mark": [[mark_price(coin, end_ms)] for coin in COINS],
            "av": {
                "addrIds": latest_addr_ids,
                "values": [[base_av(aid, end_ms)] for aid in latest_addr_ids],
            },
        },
    }
    write_json(SITE_DATA / "snapshots" / "latest.json", latest_snap)

    # 4. Partition fills into 2026-10.json.gz (<= 2026-10-29) and days/2026-10-30..2026-11-05.json
    cutoff_ms = ts_ms(datetime(2026, 10, 30, 0, 0, tzinfo=UTC))
    for coin in COINS:
        month_rows = [row for row in fills[coin] if row[0] < cutoff_ms]
        write_json(
            SITE_DATA / "fills" / coin / "months" / "2026-10.json.gz",
            {"schemaVersion": 1, "coin": coin, "fills": month_rows},
            compress=True,
        )
        for day_str in snapshot_days:
            d0 = ts_ms(datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=UTC))
            d1 = d0 + DAY_MS
            day_rows = [row for row in fills[coin] if d0 <= row[0] < d1]
            write_json(
                SITE_DATA / "fills" / coin / "days" / f"{day_str}.json",
                {"schemaVersion": 1, "coin": coin, "fills": day_rows},
            )

    return latest_snap, snapshot_days, ["2026-10"]


def total_bytes_on_disk() -> int:
    return sum(p.stat().st_size for p in SITE_DATA.rglob("*") if p.is_file())


def main() -> None:
    cfg = _ensure_pipeline_env()
    if SITE_DATA.exists():
        for p in sorted(SITE_DATA.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
    SITE_DATA.mkdir(parents=True, exist_ok=True)

    build_candles()
    whitelist = build_whitelist(cfg)
    candidates = build_candidates()
    criteria = build_criteria(candidates, cfg)
    _, snapshot_days, snapshot_months = build_snapshots_and_fills()

    write_json(SITE_DATA / "whitelist.json", whitelist)
    write_json(SITE_DATA / "criteria.json", criteria)
    write_json(SITE_DATA / "candidates.json", candidates)

    end_ms = ts_ms(END_DT)
    launched_ms = ts_ms(LAUNCHED_DT)
    daily_ok_ms = ts_ms(datetime(2026, 11, 5, 0, 30, tzinfo=UTC))
    start_ms = ts_ms(START_DAY)

    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "generatedAt": end_ms,
        "latestSnapshotAt": end_ms,
        "launchedAt": launched_ms,
        "whitelistVersion": "2026-11-05",
        "criteriaVersion": 2,
        "coins": [
            {"coin": "BTC", "pinned": True, "trackedFrom": start_ms, "trackedTo": None},
            {"coin": "ETH", "pinned": True, "trackedFrom": start_ms, "trackedTo": None},
        ],
        "files": {
            "snapshotDays": snapshot_days,
            "snapshotMonths": snapshot_months,
            "fills": {coin: {"days": snapshot_days, "months": snapshot_months} for coin in COINS},
            "candles": {
                coin: {
                    "months5m": ["2026-10", "2026-11"],
                    "hasBackfill": True,
                    "hasOverview4h": True,
                }
                for coin in COINS
            },
        },
        "storage": {
            "totalBytesOnDisk": 0,
            "softLimitBytes": 800_000_000,
        },
        "pipeline": {
            "fastLastOk": end_ms,
            "dailyLastOk": daily_ok_ms,
            "warnings": [],
        },
    }

    for _ in range(4):
        write_json(SITE_DATA / "manifest.json", manifest)
        exact = total_bytes_on_disk()
        if manifest["storage"]["totalBytesOnDisk"] == exact:
            break
        manifest["storage"]["totalBytesOnDisk"] = exact

    print(
        f"Generated fixtures/site-data/ ({len(list(SITE_DATA.rglob('*')))} entries, "
        f"totalBytesOnDisk={manifest['storage']['totalBytesOnDisk']} B)"
    )


if __name__ == "__main__":
    main()

# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline contract, normalization, pagination, equity, and concurrency tests for InfoApiClient."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError

from hyperpod.client.info_api import (
    DEFAULT_INFO_URL,
    ApiHttpError,
    ApiTransportError,
    InfoApiClient,
    InfoApiError,
    aggregate_public_fill_rows,
    compute_account_equity,
    is_main_perp_coin,
    normalize_fills,
)
from hyperpod.client.ratelimit import WeightRateLimiter
from hyperpod.models.api import (
    ClearinghouseStateResponse,
    MetaAndAssetCtxsResponse,
    PerpMeta,
    PortfolioResponse,
    RawCandle,
    RawFill,
    SpotClearinghouseStateResponse,
    SubAccountItem,
    UserFundingEvent,
    UserRoleResponse,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
API_FIXTURES_DIR = REPO_ROOT / "fixtures" / "api"


def _load_fixture(name: str) -> Any:
    return json.loads((API_FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _fast_limiter(budget: int = 800, max_retries: int = 2) -> WeightRateLimiter:
    now = 0.0

    async def fast_sleep(dt: float) -> None:
        nonlocal now
        now += dt

    return WeightRateLimiter(
        budget_per_min=budget,
        max_retries=max_retries,
        clock=lambda: now,
        sleep=fast_sleep,
        rng=lambda: 0.0,
    )


def _sample_fill(
    *,
    coin: str = "BTC",
    px: str = "95000.0",
    sz: str = "1.0",
    side: str = "B",
    time: int = 1794000000000,
    start_pos: str = "0.0",
    dir_str: str = "Open Long",
    closed_pnl: str = "0.0",
    fee: str = "1.5",
    tid: int = 1,
    oid: int = 10,
    crossed: bool = True,
    tx_hash: str = "0x" + "1" * 64,
    liquidation: dict[str, str] | None = None,
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "coin": coin,
        "px": px,
        "sz": sz,
        "side": side,
        "time": time,
        "startPosition": start_pos,
        "dir": dir_str,
        "closedPnl": closed_pnl,
        "hash": tx_hash,
        "oid": oid,
        "crossed": crossed,
        "fee": fee,
        "tid": tid,
        "feeToken": "USDC",
        "twapId": None,
    }
    if liquidation is not None:
        obj["liquidation"] = liquidation
    return obj


def _sample_funding(
    *,
    time: int,
    coin: str,
    usdc: str,
    n_samples: int | None = None,
    tx_hash: str = "0x" + "0" * 64,
) -> dict[str, Any]:
    return {
        "time": time,
        "hash": tx_hash,
        "delta": {
            "type": "funding",
            "coin": coin,
            "usdc": usdc,
            "szi": "10.0",
            "fundingRate": "0.0000125",
            "nSamples": n_samples,
        },
    }


def test_all_88_api_fixtures_validate_against_strict_pydantic_models() -> None:
    index = _load_fixture("index.json")
    assert len(index) == 11

    validated_files = 0
    for entry in index:
        files = entry["files"]
        ClearinghouseStateResponse.model_validate(_load_fixture(files["clearinghouseState"]))
        SpotClearinghouseStateResponse.model_validate(
            _load_fixture(files["spotClearinghouseState"])
        )
        PortfolioResponse.model_validate(_load_fixture(files["portfolio"]))
        UserRoleResponse.model_validate(_load_fixture(files["userRole"]))

        abs_val = _load_fixture(files["userAbstraction"])
        assert isinstance(abs_val, str) and abs_val

        subs_raw = _load_fixture(files["subAccounts"])
        if subs_raw is not None:
            for item in subs_raw:
                SubAccountItem.model_validate(item)

        for raw_fill in _load_fixture(files["userFillsByTime"]):
            RawFill.model_validate(raw_fill)

        for raw_fund in _load_fixture(files["userFunding"]):
            UserFundingEvent.model_validate(raw_fund)

        validated_files += 8

    assert validated_files == 88


def test_compute_account_equity_across_all_abstraction_modes_and_fallbacks() -> None:
    # 1. disabled: ignores spot USDC (0x023a3d05 has $1.65M isolated spot USDC)
    ch_dis = ClearinghouseStateResponse.model_validate(
        _load_fixture("clearinghouseState_0x023a3d05.json")
    )
    sp_dis = SpotClearinghouseStateResponse.model_validate(
        _load_fixture("spotClearinghouseState_0x023a3d05.json")
    )
    eq, src, warns = compute_account_equity(ch_dis, "disabled", sp_dis)
    assert eq == pytest.approx(float(ch_dis.marginSummary.accountValue))
    assert src == "perp"
    assert warns == []

    # 2. default
    ch_def = ClearinghouseStateResponse.model_validate(
        _load_fixture("clearinghouseState_0x87f9cd15.json")
    )
    eq, src, warns = compute_account_equity(ch_def, "default", None)
    assert eq == pytest.approx(float(ch_def.marginSummary.accountValue))
    assert src == "perp"
    assert warns == []

    # 3. unifiedAccount pure perp (0x1f250df5): no double counting
    ch_u2 = ClearinghouseStateResponse.model_validate(
        _load_fixture("clearinghouseState_0x1f250df5.json")
    )
    sp_u2 = SpotClearinghouseStateResponse.model_validate(
        _load_fixture("spotClearinghouseState_0x1f250df5.json")
    )
    eq, src, warns = compute_account_equity(ch_u2, "unifiedAccount", sp_u2)
    assert eq == pytest.approx(6_709_328.02523276, abs=1e-6)
    assert src == "perp+spot"
    assert warns == []

    # 4. unifiedAccount mixed perp + idle spot (0xdf9ea6ec) & ch_av == 0 case
    ch_u1 = ClearinghouseStateResponse.model_validate(
        _load_fixture("clearinghouseState_0xdf9ea6ec.json")
    )
    sp_u1 = SpotClearinghouseStateResponse.model_validate(
        _load_fixture("spotClearinghouseState_0xdf9ea6ec.json")
    )
    eq, src, _ = compute_account_equity(ch_u1, "unifiedAccount", sp_u1)
    assert eq == pytest.approx(1_342_369.52459167, abs=1e-6)
    assert src == "perp+spot"

    ch_zero_dict = copy.deepcopy(_load_fixture("clearinghouseState_0xdf9ea6ec.json"))
    ch_zero_dict["marginSummary"]["accountValue"] = "0.0"
    ch_zero_dict["crossMaintenanceMarginUsed"] = "0.0"
    eq_zero, src_zero, _ = compute_account_equity(
        ClearinghouseStateResponse.model_validate(ch_zero_dict), "unifiedAccount", sp_u1
    )
    assert eq_zero == pytest.approx(1_342_369.52459167, abs=1e-6)
    assert src_zero == "perp+spot"

    # 5. portfolioMargin supplied USDC (0xbf732ea0)
    ch_pm1 = ClearinghouseStateResponse.model_validate(
        _load_fixture("clearinghouseState_0xbf732ea0.json")
    )
    sp_pm1 = SpotClearinghouseStateResponse.model_validate(
        _load_fixture("spotClearinghouseState_0xbf732ea0.json")
    )
    eq, src, _ = compute_account_equity(ch_pm1, "portfolioMargin", sp_pm1)
    assert eq == pytest.approx(1_687_466.6593072, abs=1e-6)
    assert src == "perp+spot"

    # 6. portfolioMargin negative borrowed USDC against HYPE (0xb1ec7feb)
    ch_pm2 = ClearinghouseStateResponse.model_validate(
        _load_fixture("clearinghouseState_0xb1ec7feb.json")
    )
    sp_pm2 = SpotClearinghouseStateResponse.model_validate(
        _load_fixture("spotClearinghouseState_0xb1ec7feb.json")
    )
    eq, src, _ = compute_account_equity(ch_pm2, "portfolioMargin", sp_pm2)
    assert eq == pytest.approx(3_894_587.6513679898 + 841_845.654374, abs=1e-6)
    assert src == "perp+spot"

    # 7. Missing token 0 in tokenToAvailableAfterMaintenance falls back to max(ch_av, spot_usdc)
    sp_no_tok0_raw = copy.deepcopy(_load_fixture("spotClearinghouseState_0xb1ec7feb.json"))
    sp_no_tok0_raw["tokenToAvailableAfterMaintenance"] = [[150, "71571.47"]]
    eq_no_tok0, src_no_tok0, _ = compute_account_equity(
        ch_pm2, "portfolioMargin", SpotClearinghouseStateResponse.model_validate(sp_no_tok0_raw)
    )
    assert eq_no_tok0 == pytest.approx(float(ch_pm2.marginSummary.accountValue))
    assert src_no_tok0 == "perp+spot"

    # 8. Missing spot & unknown abstraction emit warnings
    eq_missing_sp, src_ms, warns_ms = compute_account_equity(ch_u1, "unifiedAccount", None)
    assert eq_missing_sp == pytest.approx(float(ch_u1.marginSummary.accountValue))
    assert src_ms == "perp+spot"
    assert any("Missing spotClearinghouseState" in w for w in warns_ms)

    eq_unk, src_unk, warns_unk = compute_account_equity(ch_u1, "futureSharedMode", sp_u1)
    assert eq_unk == pytest.approx(1_342_369.52459167, abs=1e-6)
    assert src_unk == "perp+spot"
    assert any("Unrecognized userAbstraction" in w for w in warns_unk)


def test_main_perp_filter_normalization_intra_ms_and_liquidation_rules() -> None:
    assert is_main_perp_coin("BTC", {"BTC", "MATIC"})
    assert is_main_perp_coin("MATIC", {"BTC", "MATIC"})  # delisted main perp retained
    assert not is_main_perp_coin("#12270", {"BTC"})
    assert not is_main_perp_coin("#12270", None)
    assert not is_main_perp_coin("xyz:CL", None)
    assert not is_main_perp_coin("@107", None)
    assert not is_main_perp_coin("PURR/USDC", None)
    assert not is_main_perp_coin("UNKNOWN", {"BTC"})

    # Fixture test 1: 0x6ecbb68c FARTCOIN intra-ms Short > Long chain at t=1793861593746
    raw_6ec = [RawFill.model_validate(x) for x in _load_fixture("userFillsByTime_0x6ecbb68c.json")]
    norm_6ec, warns_6ec = normalize_fills(
        raw_6ec,
        "0x6ecbb68c07bd4be7adb7f0365e9263e382aeca2d",
    )
    assert warns_6ec == []
    fart_ms = [f for f in norm_6ec if f.coin == "FARTCOIN" and f.time == 1793861593746]
    # seq 70: CS 4553.0, seq 71: CS 1601.0, seq 72 Short > Long split into CS 1447.0 + OL 514.0
    assert [(f.dir_code, f.sz, f.api_seq_index, f.leg_idx) for f in fart_ms] == [
        ("CS", 4553.0, 70, 0),
        ("CS", 1601.0, 71, 0),
        ("CS", 1447.0, 72, 0),
        ("OL", 514.0, 72, 1),
    ]
    # Reconstructed position matches final +514.0 despite non-monotonic tid!
    szi = fart_ms[0].start_position
    for leg in fart_ms:
        signed_delta = leg.sz if leg.dir_code in ("OL", "CS") else -leg.sz
        szi += signed_delta
    assert szi == pytest.approx(514.0)

    # Verify proportional fee split & closedPnl allocation on flip
    flip_raw = raw_6ec[72]
    total_fee = float(flip_raw.fee)
    total_pnl = float(flip_raw.closedPnl)
    assert fart_ms[2].closed_pnl == pytest.approx(total_pnl)
    assert fart_ms[3].closed_pnl == 0.0
    assert fart_ms[2].fee + fart_ms[3].fee == pytest.approx(total_fee)
    assert fart_ms[2].fee == pytest.approx(total_fee * (1447.0 / 1961.0))

    # Fixture test 2: 0xbf732ea0 BTC intra-ms open chain & counterparty liquidation -> liq = 0
    raw_bf7 = [RawFill.model_validate(x) for x in _load_fixture("userFillsByTime_0xbf732ea0.json")]
    bf7_addr = "0xbf732ea04197942783e34730ed6e0f6099575d58"
    norm_bf7, _ = normalize_fills(raw_bf7, bf7_addr)
    btc_ms = [f for f in norm_bf7 if f.coin == "BTC" and f.time == 1793976031201]
    assert [f.start_position for f in btc_ms] == [10.0, 20.0, 30.0]
    assert [f.api_seq_index for f in btc_ms] == [0, 1, 2]
    # Row 19 has liquidation.liquidatedUser != bf7_addr -> must have liq == 0!
    assert norm_bf7[19].liq == 0

    # Synthetic normalization edge cases: self-liq, Long > Short, non-standard liq/unknown dir, VWAP
    user = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    synthetic = [
        RawFill.model_validate(
            _sample_fill(
                coin="BTC",
                px="100000.0",
                sz="5.0",
                side="A",
                time=1000,
                start_pos="2.0",
                dir_str="Long > Short",
                closed_pnl="500.0",
                fee="10.0",
                tid=1,
            )
        ),
        RawFill.model_validate(
            _sample_fill(
                coin="BTC",
                px="98000.0",
                sz="1.0",
                side="A",
                time=2000,
                start_pos="1.0",
                dir_str="Close Long",
                tid=2,
                liquidation={
                    "liquidatedUser": "0x" + user[2:].upper(),
                    "markPx": "98000.0",
                    "method": "market",
                },
            )
        ),
        RawFill.model_validate(
            _sample_fill(
                coin="BTC",
                px="99000.0",
                sz="2.0",
                side="B",
                time=3000,
                start_pos="-2.0",
                dir_str="Liquidated Cross",
                tid=3,
            )
        ),
        RawFill.model_validate(
            _sample_fill(
                coin="ETH",
                px="3000.0",
                sz="1.0",
                side="B",
                time=3000,
                start_pos="0.0",
                dir_str="MysteryDir",
                tid=4,
            )
        ),
        RawFill.model_validate(
            _sample_fill(
                coin="#12270",
                px="0.0",
                sz="10.0",
                side="A",
                time=3000,
                start_pos="10.0",
                dir_str="Settlement",
                tid=5,
            )
        ),
        # Degenerate Long > Short edge cases
        RawFill.model_validate(
            _sample_fill(
                coin="SOL",
                px="100.0",
                sz="2.0",
                side="A",
                time=4000,
                start_pos="0.0",
                dir_str="Long > Short",
                tid=6,
            )
        ),
        RawFill.model_validate(
            _sample_fill(
                coin="SOL",
                px="100.0",
                sz="2.0",
                side="A",
                time=5000,
                start_pos="3.0",
                dir_str="Long > Short",
                tid=7,
            )
        ),
    ]
    norm_syn, warns_syn = normalize_fills(synthetic, user, main_perp_universe={"BTC", "ETH", "SOL"})
    assert [f.dir_code for f in norm_syn[:4]] == ["CL", "OS", "CL", "CS"]
    assert norm_syn[0].sz == 2.0 and norm_syn[1].sz == 3.0
    assert norm_syn[0].fee == 4.0 and norm_syn[1].fee == 6.0
    assert norm_syn[2].liq == 1  # self liquidation (case-insensitive)
    assert norm_syn[3].liq == 1  # fallback liquidation dir
    assert [f.dir_code for f in norm_syn[4:]] == ["OS", "CL"]
    assert any("MysteryDir" in w for w in warns_syn)
    assert any("Fallback liquidation" in w for w in warns_syn)

    # Public fill aggregation: merge identical (time, addrId, dirCode, liq) with VWAP
    agg_fills = [
        *norm_syn[:2],
        *normalize_fills(
            [
                RawFill.model_validate(
                    _sample_fill(
                        coin="BTC", px="102000.0", sz="2.0", time=1000, dir_str="Close Long", tid=8
                    )
                )
            ],
            user,
        )[0],
    ]
    grouped = aggregate_public_fill_rows(agg_fills, addr_id=7)
    btc_rows = grouped["BTC"]
    # (1000, 7, "CL", 0) merged: sz = 2 + 2 = 4, notional = 200k + 204k = 404k -> vwap = 101k
    assert btc_rows[0][:3] == (1000, 7, "CL")
    assert btc_rows[0][3] == pytest.approx(101000.0)
    assert btc_rows[0][4] == pytest.approx(4.0)
    assert btc_rows[0][5] == pytest.approx(404000.0)
    assert btc_rows[0][6] == 0

    assert btc_rows[1][:3] == (1000, 7, "OS")
    assert btc_rows[1][3] == pytest.approx(100000.0)
    assert btc_rows[1][4] == pytest.approx(3.0)
    assert btc_rows[1][5] == pytest.approx(300000.0)
    assert btc_rows[1][6] == 0


def test_user_fills_pagination_overlap_dedup_and_livelock_guard() -> None:
    async def scenario() -> None:
        user = "0x1111111111111111111111111111111111111111"
        # Build 2,000 fills for page 1 (t=10_000..200_000) and overlap fills in page 2.
        page1 = [
            _sample_fill(time=10_000 + i * 100, tid=1000 + i, tx_hash=f"0x{i:064x}")
            for i in range(2000)
        ]
        # Last ts on page 1 is 10_000 + 1999 * 100 = 209_900; overlap start = 149_900.
        overlap_fills = [f for f in page1 if f["time"] >= 149_900]
        new_fills = [
            _sample_fill(time=210_000 + i * 100, tid=5000 + i, tx_hash=f"0x{5000 + i:064x}")
            for i in range(50)
        ]
        page2 = overlap_fills + new_fills

        seen_start_times: list[int] = []

        with respx.mock(assert_all_called=True) as router:

            def handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content)
                assert body["type"] == "userFillsByTime"
                assert body["aggregateByTime"] is True
                st = int(body["startTime"])
                seen_start_times.append(st)
                if st == 0:
                    return httpx.Response(200, json=page1)
                assert st == 209_900 - 60_000
                return httpx.Response(200, json=page2)

            router.post(DEFAULT_INFO_URL).mock(side_effect=handler)
            limiter = _fast_limiter()
            async with InfoApiClient(rate_limiter=limiter) as client:
                res = await client.fetch_user_fills_by_time(user, overlap_ms=60_000)

        assert seen_start_times == [0, 149_900]
        assert res.pages_fetched == 2
        assert len(res.fills) == 2050
        assert not res.has_more
        assert not res.truncated_10k
        assert limiter.stats.extra_weight_total == 100 + 33  # ceil(2000/20) + ceil(650/20)

        # Test dense single-millisecond livelock guard: 2,000 fills at t=500_000 + 1 at 500_001
        dense_ms = [_sample_fill(time=500_000, tid=i, tx_hash=f"0x{i:064x}") for i in range(2000)]
        tail_fill = [_sample_fill(time=500_001, tid=9999, tx_hash="0x" + "9" * 64)]
        guard_starts: list[int] = []

        with respx.mock(assert_all_called=True) as router:

            def guard_handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content)
                st = int(body["startTime"])
                guard_starts.append(st)
                if st <= 500_000:
                    return httpx.Response(200, json=dense_ms)
                return httpx.Response(200, json=tail_fill)

            router.post(DEFAULT_INFO_URL).mock(side_effect=guard_handler)
            async with InfoApiClient(rate_limiter=_fast_limiter()) as client:
                res_guard = await client.fetch_user_fills_by_time(
                    user, start_time=500_000, overlap_ms=60_000
                )

        assert guard_starts == [440_000, 500_000, 500_001]
        assert len(res_guard.fills) == 2001
        assert len(res_guard.warnings) == 1
        assert "dense boundary guard" in res_guard.warnings[0]

    asyncio.run(scenario())


def test_user_funding_mid_timestamp_500_split_and_cross_run_idempotence() -> None:
    async def scenario() -> None:
        user = "0x2222222222222222222222222222222222222222"
        # Page 0: 492 prior events + 8 events at t=1_780_963_200_000 (split mid-timestamp!).
        page0 = [
            _sample_funding(
                time=1_780_000_000_000 + (i // 4) * 3_600_000,
                coin=["BTC", "ETH", "SOL", "xyz:CL"][i % 4],
                usdc="-1.0" if i % 4 != 3 else "-10.0",
                n_samples=24,
            )
            for i in range(492)
        ]
        split_t = 1_780_963_200_000
        page0.extend(
            [
                _sample_funding(time=split_t, coin=c, usdc="2.5", n_samples=24)
                for c in ["BTC", "ETH", "SOL", "ADA", "AVAX", "DOGE", "LINK", "xyz:NVDA"]
            ]
        )
        assert len(page0) == 500

        # Page 1 starts inclusively at split_t: overlaps 8 + adds 2 remaining + 1 null nSamples
        page1 = [
            *page0[-8:],
            _sample_funding(time=split_t, coin="HYPE", usdc="-5.0", n_samples=24),
            _sample_funding(time=split_t, coin="ARB", usdc="1.0", n_samples=24),
            _sample_funding(time=split_t + 3_600_000, coin="BTC", usdc="10.0", n_samples=None),
        ]

        cursors: list[int] = []
        with respx.mock(assert_all_called=True) as router:

            def handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content)
                st = int(body["startTime"])
                cursors.append(st)
                if st == 0:
                    return httpx.Response(200, json=page0)
                assert st == split_t
                return httpx.Response(200, json=page1)

            router.post(DEFAULT_INFO_URL).mock(side_effect=handler)
            limiter = _fast_limiter()
            async with InfoApiClient(rate_limiter=limiter) as client:
                full = await client.fetch_user_funding(
                    user,
                    main_perp_universe={
                        "BTC",
                        "ETH",
                        "SOL",
                        "ADA",
                        "AVAX",
                        "DOGE",
                        "LINK",
                        "HYPE",
                        "ARB",
                    },
                )

        assert cursors == [0, split_t]
        assert full.pages_fetched == 2
        assert len(full.events) == 503
        # xyz:CL (123 events) + xyz:NVDA (1 event) = 124 excluded -> 379 main perp events
        assert len(full.main_perp_events) == 379
        assert full.last_funding_time == split_t + 3_600_000
        assert full.last_funding_coins == ["BTC"]

        # Expected main perp sum:
        # 492/4 * 3 = 369 events @ -1.0 = -369.0
        # 7 main perp at split_t from page0 @ +2.5 = +17.5
        # HYPE (-5.0) + ARB (+1.0) + BTC (+10.0) = +6.0
        assert full.cum_funding_net == pytest.approx(-369.0 + 17.5 + 6.0)
        assert full.cum_funding_abs == pytest.approx(369.0 + 17.5 + 16.0)

        # Cross-run incremental resumption equivalence (Day d -> Day d+1)
        day_d_resp = [
            _sample_funding(time=1000, coin="BTC", usdc="-4.0"),
            _sample_funding(time=1000, coin="ETH", usdc="1.0"),
        ]
        day_d1_resp = [
            *day_d_resp,
            _sample_funding(time=1000, coin="SOL", usdc="3.0"),
            _sample_funding(time=2000, coin="BTC", usdc="2.0", n_samples=None),
        ]
        with respx.mock(assert_all_called=True) as router:

            def inc_handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content)
                if int(body["startTime"]) == 0:
                    return httpx.Response(200, json=day_d_resp)
                return httpx.Response(200, json=day_d1_resp)

            router.post(DEFAULT_INFO_URL).mock(side_effect=inc_handler)
            async with InfoApiClient(rate_limiter=_fast_limiter()) as client:
                r1 = await client.fetch_user_funding(user, start_time=0)
                r2 = await client.fetch_user_funding(
                    user,
                    start_time=r1.last_funding_time or 0,
                    seed_last_funding_coins=r1.last_funding_coins,
                )

        assert r1.cum_funding_net + r2.cum_funding_net == pytest.approx(2.0)
        assert r1.cum_funding_abs + r2.cum_funding_abs == pytest.approx(10.0)
        assert r2.last_funding_time == 2000
        assert r2.last_funding_coins == ["BTC"]

    asyncio.run(scenario())


def test_concurrency_limit_and_all_endpoints_plus_retries_and_errors() -> None:
    async def scenario() -> None:
        ch_fixture = _load_fixture("clearinghouseState_0x1f250df5.json")
        in_flight = 0
        max_seen = 0

        with respx.mock(assert_all_called=True) as router:

            async def ch_handler(request: httpx.Request) -> httpx.Response:
                nonlocal in_flight, max_seen
                in_flight += 1
                max_seen = max(max_seen, in_flight)
                await asyncio.sleep(0.01)
                in_flight -= 1
                return httpx.Response(200, json=ch_fixture)

            router.post(DEFAULT_INFO_URL).mock(side_effect=ch_handler)
            async with InfoApiClient(rate_limiter=_fast_limiter(), max_concurrency=4) as client:
                results = await asyncio.gather(
                    *[
                        client.fetch_clearinghouse_state(
                            "0x1f250df59a777d61cb8bd043c12970f3afe4f925"
                        )
                        for _ in range(16)
                    ]
                )
        assert len(results) == 16
        assert 2 <= max_seen <= 4

        # Endpoint coverage: meta, metaAndAssetCtxs, candleSnapshot, subAccounts=null, retries
        meta_payload = {
            "universe": [{"szDecimals": 5, "name": "BTC", "maxLeverage": 40, "marginTableId": 56}],
            "marginTables": [
                [56, {"description": "", "marginTiers": [{"lowerBound": "0.0", "maxLeverage": 40}]}]
            ],
            "collateralToken": 0,
        }
        ctx_payload = {
            "funding": "0.0000125",
            "openInterest": "100.0",
            "prevDayPx": "95000.0",
            "dayNtlVlm": "1000000.0",
            "premium": "0.0001",
            "oraclePx": "96000.0",
            "markPx": "96010.0",
            "midPx": "96005.0",
            "impactPxs": ["96000.0", "96010.0"],
            "dayBaseVlm": "10.0",
        }
        candle_payload = [
            {
                "t": 1794720000000,
                "T": 1794720299999,
                "s": "BTC",
                "i": "5m",
                "o": "96000.0",
                "c": "96100.0",
                "h": "96200.0",
                "l": "95900.0",
                "v": "12.5",
                "n": 100,
            }
        ]

        attempts = 0
        with respx.mock(assert_all_called=True) as router:

            def multi_handler(request: httpx.Request) -> httpx.Response:
                nonlocal attempts
                body = json.loads(request.content)
                kind = body["type"]
                if kind == "meta":
                    attempts += 1
                    if attempts == 1:
                        return httpx.Response(429, text="rate limited")
                    if attempts == 2:
                        return httpx.Response(502, text="bad gateway")
                    return httpx.Response(200, json=meta_payload)
                if kind == "metaAndAssetCtxs":
                    return httpx.Response(200, json=[meta_payload, [ctx_payload]])
                if kind == "candleSnapshot":
                    return httpx.Response(200, json=candle_payload)
                if kind == "subAccounts":
                    return httpx.Response(
                        200,
                        content=b"null",
                        headers={"Content-Type": "application/json"},
                    )
                if kind == "userRole":
                    return httpx.Response(200, json={"role": "user"})
                if kind == "portfolio":
                    return httpx.Response(200, json=_load_fixture("portfolio_0x1f250df5.json"))
                if kind == "userAbstraction":
                    return httpx.Response(200, json="unifiedAccount")
                raise AssertionError(f"unexpected endpoint {kind}")

            router.post(DEFAULT_INFO_URL).mock(side_effect=multi_handler)
            limiter = _fast_limiter(max_retries=2)
            async with InfoApiClient(rate_limiter=limiter) as client:
                meta = await client.fetch_meta()
                assert isinstance(meta, PerpMeta)
                assert meta.universe[0].name == "BTC"

                mac = await client.fetch_meta_and_asset_ctxs()
                assert isinstance(mac, MetaAndAssetCtxsResponse)
                assert mac.asset_ctxs[0].markPx == "96010.0"

                candles = await client.fetch_candle_snapshot("BTC", "5m", 0, 1794720300000)
                assert len(candles) == 1
                assert isinstance(candles[0], RawCandle)
                assert candles[0].to_candle_row() == (
                    1794720000000,
                    96000.0,
                    96200.0,
                    95900.0,
                    96100.0,
                    12.5,
                )
                assert (
                    await client.fetch_sub_accounts("0x1f250df59a777d61cb8bd043c12970f3afe4f925")
                    == []
                )
                assert (
                    await client.fetch_user_abstraction(
                        "0x1f250df59a777d61cb8bd043c12970f3afe4f925"
                    )
                    == "unifiedAccount"
                )
                role_obj = await client.fetch_user_role(
                    "0x1f250df59a777d61cb8bd043c12970f3afe4f925"
                )
                assert role_obj.role == "user"
                pf_obj = await client.fetch_portfolio("0x1f250df59a777d61cb8bd043c12970f3afe4f925")
                assert len(pf_obj.window("perpAllTime").accountValueHistory) > 0

            assert limiter.stats.retries_total == 2
            assert limiter.stats.http_429_count == 1
            assert limiter.stats.http_5xx_count == 1
            assert limiter.stats.extra_weight_total == 1

        # Negative error cases: HTTP 400 (no retry), HTTP 500 exhaustion, transport error, bad JSON
        with respx.mock(assert_all_called=True) as router:
            router.post(DEFAULT_INFO_URL).mock(return_value=httpx.Response(400, text="bad req"))
            async with InfoApiClient(rate_limiter=_fast_limiter(max_retries=2)) as client:
                with pytest.raises(ApiHttpError, match="HTTP 400"):
                    await client.fetch_meta()

        with respx.mock(assert_all_called=True) as router:
            router.post(DEFAULT_INFO_URL).mock(return_value=httpx.Response(503, text="down"))
            async with InfoApiClient(rate_limiter=_fast_limiter(max_retries=1)) as client:
                with pytest.raises(ApiHttpError, match="HTTP 503"):
                    await client.fetch_meta()

        with respx.mock(assert_all_called=True) as router:
            router.post(DEFAULT_INFO_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))
            async with InfoApiClient(rate_limiter=_fast_limiter(max_retries=1)) as client:
                with pytest.raises(ApiTransportError, match="transport error"):
                    await client.fetch_meta()

        with respx.mock(assert_all_called=True) as router:
            router.post(DEFAULT_INFO_URL).mock(return_value=httpx.Response(200, text="not-json"))
            async with InfoApiClient(rate_limiter=_fast_limiter()) as client:
                with pytest.raises(InfoApiError, match="invalid JSON"):
                    await client.fetch_meta()

        with pytest.raises(ValidationError):
            PortfolioResponse.model_validate(
                [["day", {"accountValueHistory": [], "pnlHistory": [], "vlm": "0"}]]
            )
        with pytest.raises(ValidationError):
            UserRoleResponse.model_validate({"role": "subAccount"})
        with pytest.raises(ValidationError):
            MetaAndAssetCtxsResponse.model_validate([meta_payload, []])
        with pytest.raises(ValidationError):
            RawCandle.model_validate(
                {
                    "t": 200,
                    "T": 100,
                    "s": "BTC",
                    "i": "5m",
                    "o": "10",
                    "c": "10",
                    "h": "10",
                    "l": "10",
                    "v": "0",
                    "n": 1,
                }
            )

        # Cross-run seed_keys dedup on fetch_user_fills_by_time
        seeded_fill = _sample_fill(time=590_000, tid=11, tx_hash="0x" + "a" * 64)
        fresh_fill = _sample_fill(time=610_000, tid=12, tx_hash="0x" + "b" * 64)
        with respx.mock(assert_all_called=True) as router:
            router.post(DEFAULT_INFO_URL).mock(
                return_value=httpx.Response(200, json=[seeded_fill, fresh_fill])
            )
            async with InfoApiClient(rate_limiter=_fast_limiter()) as client:
                seeded_res = await client.fetch_user_fills_by_time(
                    "0x1111111111111111111111111111111111111111",
                    start_time=600_000,
                    overlap_ms=60_000,
                    seed_keys=[(590_000, 11, "0x" + "a" * 64, "BTC", "Open Long")],
                )
        assert [f.tid for _, f in seeded_res.fills] == [12]

    asyncio.run(scenario())

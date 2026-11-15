<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Task 0.4 — Fill `dir` Values, Pagination, Dedup Key, Intra-ms Ordering & Aggregate Ratio

## 1. Survey Scope & Methodology
Evaluated `POST https://api.hyperliquid.xyz/info` (`type: "userFillsByTime"`) across:
1. **Top 50 Stage 1 candidates** (`startTime: 0`, `aggregateByTime: true`): `50 / 50` addresses returned fills, `47` hit the `2,000`-fill first-page cap, yielding **`94,131` total fills scanned**.
2. **Extended liquidation probe**: `stage1[50:110]` plus the 10-address Phase 0.2 fixture pool (`fixtures/api/userFillsByTime_*.json`).
3. **Pagination boundary overlap tests**: `3` active addresses queried across consecutive 2,000-fill pages using `overlapMs = 60_000`.
4. **`aggregateByTime` compression benchmark**: `20` addresses comparing `aggregateByTime: true` vs `aggregateByTime: false` over matched time intervals.

---

## 2. Observed `dir` Values & Main-Perp Filter Pitfall

### 2.1 Distribution Across `94,131` Scanned Fills
| `dir` | All Markets | HIP-3 (`:` in `coin`) | Spot (`@` / `/`) | Naive Main-Perp Filter | True Main Perp DEX (`meta.universe`) | Notes |
|---|---:|---:|---:|---:|---:|---|
| `Close Short` | 31,977 | 11,738 | 0 | 20,239 | 20,239 | Standard main/HIP-3 short close (`CS`) |
| `Open Short` | 23,623 | 11,566 | 0 | 12,057 | 12,057 | Standard main/HIP-3 short open (`OS`) |
| `Open Long` | 17,434 | 5,591 | 0 | 11,843 | 11,843 | Standard main/HIP-3 long open (`OL`) |
| `Close Long` | 16,953 | 6,633 | 0 | 10,320 | 10,320 | Standard main/HIP-3 long close (`CL`) |
| `Long > Short` | 438 | 335 | 0 | 103 | 103 | Position reversal (`CL` + `OS`, §5.2) |
| `Short > Long` | 428 | 324 | 0 | 104 | 104 | Position reversal (`CS` + `OL`, §5.2) |
| `Sell` | 1,699 | 0 | 1,698 | 1 *(leaked `#...`)* | 0 | Spot & `#...` outcome markets only |
| `Buy` | 1,560 | 0 | 1,477 | 83 *(leaked `#...`)* | 0 | Spot & `#...` outcome markets only |
| `Merge Outcome` | 18 | 0 | 0 | 18 *(leaked `#...`)* | 0 | Outcome/prediction markets (`#...`) only |
| `Settlement` | 1 | 0 | 0 | 1 *(leaked `#...`)* | 0 | Outcome/prediction markets (`#...`) only |
| **Total** | **94,131** | **36,187** | **3,175** | **54,769** | **54,666** | `103` `#...` fills caught by strict filter |

### 2.2 Critical Filter Pitfall: `#...` Outcome/Prediction Markets
During Task 0.4, an initial scanner that filtered only on `":"`, `"@"`, and `"/"` allowed **`103` `#...` outcome/prediction market fills** (`"#12270"`, `"#301230"`) to leak into `dir_counts_main_perp`:
- `Buy: 83`
- `Merge Outcome: 18`
- `Sell: 1`
- `Settlement: 1`

Verbatim outcome-market samples observed in the scan:
```json
{
  "coin": "#12270",
  "px": "0.0",
  "sz": "5446.0",
  "side": "A",
  "time": 1789581960240,
  "startPosition": "5446.0",
  "dir": "Settlement",
  "closedPnl": "-1176.81744",
  "hash": "0x69296241e3f103256aa304448b6c0f0208e400277ef421f70cf20d94a2f4dd10",
  "oid": 547005278842,
  "crossed": true,
  "fee": "0.0",
  "tid": 80160705084637,
  "feeToken": "USDC",
  "twapId": null
}
```
```json
{
  "coin": "#301230",
  "px": "0.5",
  "sz": "2139.0",
  "side": "A",
  "time": 1794366220556,
  "startPosition": "2139.0",
  "dir": "Merge Outcome",
  "closedPnl": "-145.85823",
  "hash": "0x6a738f5c7632316d6bed0448880cb50203ea00421135503f0e3c3aaf35360b58",
  "oid": 601274985753,
  "crossed": true,
  "fee": "0.8556",
  "tid": 622484763678262,
  "feeToken": "USDC",
  "twapId": null
}
```

Separately, inspecting `spotMeta.universe` confirmed **`0` spot pairs without `@` or `/`** (352 pairs total, see [`candles-cors.md`](candles-cors.md)).

**Resulting Main-Perp Filter Rule (`docs/adr/0002-fill-dedup-and-ordering.md`)**:
A fill belongs to the main perp DEX **iff**:
1. `coin` is in `main_perp_universe` built from `meta.universe` (retaining `isDelisted: true` coins so historical fills still resolve `szDecimals`), **AND**
2. Explicit prefix/delimiter guard: `"#"`, `":"`, `"@"`, `"/"` not in `coin` and `coin != "PURR/USDC"`.

Under this strict filter, true main-perp fills (`54,666`) exhibit **only** the 6 expected perpetual directions:
`{Open Long, Close Long, Open Short, Close Short, Long > Short, Short > Long}`. `Settlement` and `Merge Outcome` follow the `{Buy, Sell, Merge Outcome, Settlement}` outcome-market taxonomy and are outside main-perp scope.

---

## 3. Liquidation Representation & Counterparty Distinction

Across all `94,131` fills in the 50-address scan plus the extended `stage1[50:110]` and Phase 0.2 fixture scan, **zero fills carried a `"Liquidated ..."` `dir` string**. Instead:
- Liquidation executions carry **standard `dir` strings** (`"Open Long"`, `"Close Long"`, `"Close Short"`) **plus** a nested `"liquidation"` object:
  ```json
  "liquidation": {
    "liquidatedUser": "<0x-address>",
    "markPx": "<decimal-string>",
    "method": "market"
  }
  ```

### Verbatim Examples
1. **Main-perp fixture sample** — [`userFillsByTime_0xbf732ea0.json` (L363–L384)](../../fixtures/api/userFillsByTime_0xbf732ea0.json#L363-L384):
   ```json
   {
     "coin": "BTC",
     "px": "74900.9999999999",
     "sz": "21.72525",
     "side": "B",
     "time": 1793977519184,
     "startPosition": "0.0",
     "dir": "Open Long",
     "closedPnl": "0.0",
     "hash": "0xa09a3987fe359df7a21304483488ae0208cd006d9938bcc94462e4dabd3977e2",
     "oid": 597202137637,
     "crossed": false,
     "fee": "0.0",
     "tid": 847460055488218,
     "liquidation": {
       "liquidatedUser": "0x02661cd414db3f170aaf00df326e421c711da7d4",
       "markPx": "74992.0",
       "method": "market"
     },
     "feeToken": "USDC",
     "twapId": null
   }
   ```
2. **Additional Task 0.4 samples** (from `0x7fdafde5cfb5465924316eced2d3715494c517d1`, `0xfc27136e42af1732ddc9ce2605ea9bff1b959d9d`, and `0x023a3d058020fb76cca98f01b3c48c8938a22355`):
   - `0x7fdafde5...` (`ZEC`, `time: 1794701552255`): `"dir": "Close Long"`, `"crossed": false`, `"fee": "-0.010772"`, `"liquidation": {"liquidatedUser": "0x443e2fd8033fc3f3019dd132de9f2d8e824a4ab2", "markPx": "868.46", "method": "market"}`.
   - `0x7fdafde5...` (`XRP`, `time: 1794701561266`): `"dir": "Close Long"`, `"crossed": false`, `"fee": "-0.055568"`, `"liquidation": {"liquidatedUser": "0x6f1da50cc058b9701eb03b5fcd6690f6e858314e", "markPx": "0.92972", "method": "market"}`.
   - `0x023a3d05...` (`ETH`, `time: 1794694118213`): `"dir": "Open Long"`, `"crossed": false`, `"fee": "-0.029889"`, `"liquidation": {"liquidatedUser": "0x090b81441c633ee1e94272e7302401f27a6a2610", "markPx": "1750.5", "method": "market"}`.
   - `0xfc27136e...` (`xyz:DELL`, `xyz:ARM`, `xyz:SILVER`): `"dir": "Close Short"` / `"Open Long"`, `"crossed": false`, `"liquidation": {"liquidatedUser": "0x35c3... / 0xc6cd... / 0xbeba...", "method": "market"}`.

### Critical Attribution Rule (`liquidation != null` vs `liquidatedUser == user`)
Notice that in all `11` observed liquidation-annotated fills above, `"crossed": false` and `liquidation.liquidatedUser != user` — the tracked trader was the **resting maker counterparty** absorbing another participant's forced liquidation (even collecting maker rebates like `fee: "-0.029889"`).

Consequently, blindly treating `fill.liquidation is not None` as `liq = 1` would mislabel maker whales as liquidated!
- **Self-liquidation (`liq = 1`)**: `fill.liquidation is not None` **and** `fill.liquidation.liquidatedUser.lower() == user.lower()`.
- **Maker counterparty to another user's liquidation (`liq = 0`)**: `fill.liquidation is not None` **and** `fill.liquidation.liquidatedUser.lower() != user.lower()`.

---

## 4. Pagination Direction, Overlap Boundary & Composite Dedup Key

### 4.1 Pagination Ordering & Retention
- `POST https://api.hyperliquid.xyz/info` with `{"type": "userFillsByTime", "user": addr, "startTime": cursor, "aggregateByTime": true}` returns up to **`2,000` fills per page in ascending `time` order**.
- The API retains a recent rolling history window (`<= 10,000` raw/windowed fills; multi-page pagination in Task 0.11 yielded `10,217–14,412` deduped aggregated fills across `6–8` pages before truncating).

### 4.2 Dedup Key Uniqueness & `overlapMs = 60_000` Boundary Tests
Evaluating the composite key `(address, time, tid, hash, coin, dir)` (within an address: `(time, tid, hash, coin, dir)`) yielded:
- **`0` key collisions across all `94,131` scanned fills**.
- Exact overlap partitioning across `overlapMs = 60_000` page transitions (`p2_start_t = p1_last_t - 60_000`):

| Address | Page 1 Len | `p1_last_t` (ms) | `p2_start_t` (ms) | Page 2 Len | Overlap Keys Matched | New Keys in Page 2 |
|---|---:|---:|---:|---:|---:|---:|
| `0xfc27136e42af1732ddc9ce2605ea9bff1b959d9d` | 2,000 | 1794587694444 | 1794587634444 | 2,000 | 20 | 1,980 |
| `0x7fdafde5cfb5465924316eced2d3715494c517d1` | 2,000 | 1794704270792 | 1794704210792 | 2,000 | 55 | 1,945 |
| `0xf5d81a135f756ca16544e53c20fc20643ec3ad53` | 2,000 | 1794714889975 | 1794714829975 | 2,000 | 151 | 1,849 |

In all 3 cases, `overlap_keys + new_keys_in_p2 == 2000` exactly, confirming clean inclusive boundary deduplication.

---

## 5. Critical Discovery: `tid` Is Non-Monotonic Within the Same Millisecond

Inspection of the Phase 0.2 fixtures revealed that **`tid` is a 50-bit hash-derived trade identifier, NOT a monotonically increasing chronological sequence number** within a millisecond (`t`). Sorting same-millisecond fills by `(time, tid)` reverses actual execution order!

### Verbatim Fixture Evidence
1. **Intra-ms `Short > Long` reversal** — [`userFillsByTime_0x6ecbb68c.json` (L1262–L1315)](../../fixtures/api/userFillsByTime_0x6ecbb68c.json#L1262-L1315) (`FARTCOIN`, `time = 1793861593746`):
   | API Seq Index | Line Range | `dir` | `sz` | `startPosition` | Post-Fill `szi` | `tid` |
   |---:|---|---|---:|---:|---:|---:|
   | 70 | L1262–L1279 | `Close Short` | `4553.0` | `-7601.0` | `-3048.0` | `943606857324540` |
   | 71 | L1280–L1297 | `Close Short` | `1601.0` | `-3048.0` | `-1447.0` | `876088275547155` |
   | 72 | L1298–L1315 | `Short > Long` | `1961.0` | `-1447.0` | `+514.0` | `40542812849106` |

   Look at the chaining: `-7601.0 -> -3048.0 -> -1447.0 -> +514.0` matches the **API response order**, while `tid` drops monotonically (`943606857324540 -> 876088275547155 -> 40542812849106`). Sorting by `(time, tid)` puts `Short > Long` (`tid = 40542812849106`) **first**, corrupting `§5.2` flip splitting and `§5.3` round-trip boundaries.

2. **Intra-ms multi-fill open chain** — [`userFillsByTime_0xbf732ea0.json` (L2–L58)](../../fixtures/api/userFillsByTime_0xbf732ea0.json#L2-L58) (`BTC`, `time = 1793976031201`):
   | API Seq Index | Line Range | `dir` | `sz` | `startPosition` | Post-Fill `szi` | `tid` |
   |---:|---|---|---:|---:|---:|---:|
   | 0 | L2–L20 | `Open Long` | `10.0` | `10.0` | `20.0` | `58951651081237` |
   | 1 | L21–L39 | `Open Long` | `10.0` | `20.0` | `30.0` | `980152391812683` |
   | 2 | L40–L58 | `Open Long` | `10.0` | `30.0` | `40.0` | `490977211903399` |

   Here `tid` oscillates (`58951651081237 -> 980152391812683 -> 490977211903399`) while `startPosition` chains strictly (`10.0 -> 20.0 -> 30.0 -> 40.0`) in API sequence order.

See [`reconstruct-check.md`](reconstruct-check.md) for the exact 5-address impact (`3 / 5` addresses diverge under `(time, tid)` vs `0.0` error across all `51 / 51` coins under API arrival order) and [`0001-intra-ms-fill-ordering.md`](../proposals/0001-intra-ms-fill-ordering.md) for the `§11.2` norm amendment proposal.

---

## 6. `aggregateByTime: true` vs `false` Ratio (`n = 20`)

Measured across `20` addresses over matched `[t_start, t_end]` intervals:

| # | Address | `t_start` (ms) | `t_end` (ms) | `agg_count` | `raw_count` | `raw / agg` Ratio |
|---:|---|---:|---:|---:|---:|---:|
| 1 | `0xfc667adba8d4837586078f4fdcdc29804337ca06` | 1794702422590 | 1794703685061 | 333 | 457 | 1.3724 |
| 2 | `0x7fdafde5cfb5465924316eced2d3715494c517d1` | 1794699731257 | 1794702763374 | 1,173 | 1,273 | 1.0853 |
| 3 | `0xfc27136e42af1732ddc9ce2605ea9bff1b959d9d` | 1794580596924 | 1794583716463 | 45 | 69 | 1.5333 |
| 4 | `0x023a3d058020fb76cca98f01b3c48c8938a22355` | 1794693426539 | 1794695058017 | 1,550 | 1,756 | 1.1329 |
| 5 | `0x856c35038594767646266bc7fd68dc26480e910d` | 1791292219507 | 1791370175166 | 1,803 | 1,999 | 1.1087 |
| 6 | `0xdcac85ecae7148886029c20e661d848a4de99ce2` | 1768033138549 | 1768056250381 | 1,895 | 1,999 | 1.0549 |
| 7 | `0xd4c1f7e8d876c4749228d515473d36f919583d1d` | 1794700732745 | 1794702656083 | 981 | 1,999 | 2.0377 |
| 8 | `0x9321d8117e73b0c79035f0e87debcfd8dbb1d75a` | 1789482503345 | 1790182334242 | 200 | 1,977 | 9.8850 |
| 9 | `0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36` | 1794576029165 | 1794579973775 | 1,303 | 1,841 | 1.4129 |
| 10 | `0x5b5d51203a0f9079f8aeb098a6523a13f298c060` | 1794643683211 | 1794650780783 | 1,813 | 1,979 | 1.0916 |
| 11 | `0x936cf4fb95c30ce83f658b5bbb247e4bb381bb0f` | 1794564580512 | 1794570994186 | 1,042 | 1,146 | 1.0998 |
| 12 | `0x58b27998b4ff896949d5f57466b259e50ee211f3` | 1793119974422 | 1793220474382 | 223 | 1,614 | 7.2377 |
| 13 | `0x523852be2db1a76a0e088ecbff32e849544054e5` | 1787163065567 | 1787235900363 | 118 | 1,899 | 16.0932 |
| 14 | `0x7f1950ce02f9f60634ba39b81844911a6c2f05bf` | 1793309303502 | 1793309521143 | 56 | 60 | 1.0714 |
| 15 | `0x7bfee91193d9df2ac0bfe90191d40f23c773c060` | 1794366219548 | 1794444988807 | 99 | 102 | 1.0303 |
| 16 | `0xd6e1a6f7bb47aca69aaa511210227d40926ff256` | 1772277523489 | 1777943195045 | 1,392 | 1,906 | 1.3693 |
| 17 | `0xf1e79e5cf063073deec9a0d86c178f7cde84d479` | 1794713663065 | 1794714462329 | 416 | 429 | 1.0312 |
| 18 | `0x325edd95bb016c36027ce3b9f7595af7094a9564` | 1792993156234 | 1793209280967 | 1,297 | 1,898 | 1.4634 |
| 19 | `0xa5ee12ffed6a53622c11ea5f678ccea19b8b243f` | 1739138038872 | 1739891611025 | 810 | 1,999 | 2.4679 |
| 20 | `0x31ca8395cf837de08b24da3f660e77761dfb974b` | 1794713263439 | 1794714692322 | 1,902 | 1,999 | 1.0510 |

- **Summary statistics (`n = 20`)**:
  - `min`: **`1.0303`**
  - `median`: **`1.2511`**
  - `mean`: **`2.7815`**
  - `max`: **`16.0932`**
- **Conclusion for `§5.5` (`hft.fillsPerDay`)**: Keeping the default `hft.fillsPerDay: 500` with `aggregateByTime: true` in `config/pipeline.yaml` is well calibrated. Typical directional traders see only `~1.25x` compression from `aggregateByTime: true`, whereas extreme algorithmic order-splitters (`7.24x–16.09x`) are still caught by `fillsPerDay`, `medianHoldMinutes < 5`, and `mm` (`makerRatio > 0.9` and `turnoverMonth > 30`).

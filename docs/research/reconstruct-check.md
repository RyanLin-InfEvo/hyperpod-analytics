<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Task 0.11 — Position Reconstruction Verification (`startPosition + Σ fills` vs `clearinghouseState.szi`)

## 1. Verification Methodology
Tested forward position reconstruction across **`5` active multi-coin addresses** (`51` main-perp coins with fills + `28` zero-fill open positions):
1. Captured `clearinghouseState` **before and after** fill pagination (`actual_before == actual_after`, `mid_fetch_changed: false` for all `51 / 51` coins) to guarantee no in-flight trades altered positions mid-check.
2. Paginated `userFillsByTime(startTime=cursor, aggregateByTime=true)` across `6–8` pages using inclusive boundary pagination `cursor = last_t` (while `overlapMs = 60_000` boundary deduplication was separately verified in Task 0.4), deduplicating by `(time, tid, hash, coin, dir)` (`10,217–14,412` deduped fills per address; all `5` addresses hit the historical cutoff `truncated_10k: true`).
3. Filtered to main perp DEX coins (`meta.universe`) and compared forward cumulative reconstruction (`initial startPosition + Σ signed_fill_delta`, quantized to each coin's `szDecimals` per `§5.3`) under two intra-millisecond sort rules:
   - **API sequence order `(time, api_seq_index)`**: preserves the server's intra-millisecond execution sequence across deduplicated pages.
   - **Literal spec `(time, tid)` order**: sorts intra-millisecond fills ascending by `tid` per `§5.2` / `§5.3`.

---

## 2. Executive Summary

| Address | Pages | Deduped Fills | `truncated_10k` | Coins w/ Fills | Zero-Fill Open Pos | Exact Match `(time, api_seq_index)` | Exact Match `(time, tid)` | `(time, tid)` Divergences |
|---|---:|---:|---|---:|---:|---:|---:|---|
| `0xfc27136e42af1732ddc9ce2605ea9bff1b959d9d` | 7 | 12,773 | `true` | 21 | 1 | **21 / 21** | 20 / 21 | `AVAX: +400.0` |
| `0x7fdafde5cfb5465924316eced2d3715494c517d1` | 7 | 12,580 | `true` | 9 | 26 | **9 / 9** | 9 / 9 | — |
| `0xfc667adba8d4837586078f4fdcdc29804337ca06` | 8 | 14,412 | `true` | 7 | 1 | **7 / 7** | 6 / 7 | `SOL: -99.85` |
| `0xdf9ea6ec3b7109935ccb4fb267e15ac1fb077ab1` | 6 | 10,217 | `true` | 1 | 0 | **1 / 1** | 1 / 1 | — |
| `0xbf732ea04197942783e34730ed6e0f6099575d58` | 6 | 11,449 | `true` | 13 | 0 | **13 / 13** | 12 / 13 | `ETH: -500.0` |
| **Total** | — | **61,431** | — | **51** | **28** | **51 / 51 (100%)** | **48 / 51** *(3 / 5 addresses fail)* | **3 critical mismatches** |

- **API sequence order `(time, api_seq_index)`**: **`diff_api == 0.0` across ALL `51 / 51` coins (`100%` exact match)** after `szDecimals` quantization.
- **Literal spec `(time, tid)` sort**: **Fails on `3` of `5` addresses** (`+400.0 AVAX`, `-99.85 SOL`, `-500.0 ETH`).

---

## 3. Complete Per-Address / Per-Coin Results

### 3.1 `0xfc27136e42af1732ddc9ce2605ea9bff1b959d9d` (`pages: 7`, `total_deduped_fills: 12,773`, `truncated_10k: true`)
| Coin | `szDecimals` | `n_fills` | `first_t` | `last_t` | Initial `startPosition` | `reconstructed_api_order` | `reconstructed_tid_order` | `actual_szi` (`before == after`) | `diff_api` | `diff_tid` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `AAVE` | 2 | 82 | 1794602174107 | 1794721321297 | `-6311.63` | `-7436.68` | `-7436.68` | `-7436.68` | `0.0` | `0.0` |
| `ADA` | 0 | 30 | 1794582667111 | 1794711705134 | `594078.0` | `532808.0` | `532808.0` | `532808.0` | `0.0` | `0.0` |
| `AVAX` | 2 | 87 | 1794588011145 | 1794720141400 | `3356.03` | `1646.95` | **`2046.95`** | `1646.95` | `0.0` | **`+400.0`** |
| `BCH` | 3 | 33 | 1794593967285 | 1794705768326 | `168.252` | `226.201` | `226.201` | `226.201` | `0.0` | `0.0` |
| `BNB` | 3 | 39 | 1794586570158 | 1794708312926 | `1276.803` | `1256.146` | `1256.146` | `1256.146` | `0.0` | `0.0` |
| `BTC` | 5 | 807 | 1794580596924 | 1794716175534 | `-996.48425` | `-952.17723` | `-952.17723` | `-952.17723` | `0.0` | `0.0` |
| `DOGE` | 0 | 47 | 1794594121822 | 1794712249274 | `-378838.0` | `-471697.0` | `-471697.0` | `-471697.0` | `0.0` | `0.0` |
| `DOT` | 1 | 67 | 1794627142964 | 1794715446724 | `52426.3` | `48644.2` | `48644.2` | `48644.2` | `0.0` | `0.0` |
| `ENA` | 0 | 180 | 1794580816957 | 1794713105574 | `1323421.0` | `1477209.0` | `1477209.0` | `1477209.0` | `0.0` | `0.0` |
| `ETH` | 4 | 479 | 1794588501533 | 1794714748653 | `-4838.26` | `-10.5187` | `-10.5187` | `-10.5187` | `0.0` | `0.0` |
| `HBAR` | 0 | 39 | 1794580721753 | 1794711708142 | `-2693228.0` | `-2680863.0` | `-2680863.0` | `-2680863.0` | `0.0` | `0.0` |
| `HYPE` | 2 | 320 | 1794586524834 | 1794720130131 | `1805.86` | `4211.97` | `4211.97` | `4211.97` | `0.0` | `0.0` |
| `LINK` | 1 | 32 | 1794583863822 | 1794717432987 | `-97557.1` | `-92785.4` | `-92785.4` | `-92785.4` | `0.0` | `0.0` |
| `LTC` | 2 | 82 | 1794582383390 | 1794713694307 | `-2336.03` | `-2290.02` | `-2290.02` | `-2290.02` | `0.0` | `0.0` |
| `ONDO` | 0 | 32 | 1794664877311 | 1794689615833 | `-28230.0` | `30472.0` | `30472.0` | `30472.0` | `0.0` | `0.0` |
| `SOL` | 2 | 94 | 1794608054399 | 1794717533679 | `-44562.09` | `-42461.86` | `-42461.86` | `-42461.86` | `0.0` | `0.0` |
| `SUI` | 1 | 79 | 1794586116752 | 1794708743635 | `710906.4` | `734970.3` | `734970.3` | `734970.3` | `0.0` | `0.0` |
| `WLD` | 1 | 13 | 1794623341033 | 1794683985094 | `-163938.6` | `-168084.7` | `-168084.7` | `-168084.7` | `0.0` | `0.0` |
| `XLM` | 0 | 74 | 1794588627056 | 1794718902872 | `-192157.0` | `98874.0` | `98874.0` | `98874.0` | `0.0` | `0.0` |
| `XRP` | 0 | 295 | 1794596333383 | 1794719723999 | `-3436096.0` | `-3832835.0` | `-3832835.0` | `-3832835.0` | `0.0` | `0.0` |
| `ZEC` | 2 | 596 | 1794587146038 | 1794721710971 | `-2416.9` | `-2439.41` | `-2439.41` | `-2439.41` | `0.0` | `0.0` |

### 3.2 `0x7fdafde5cfb5465924316eced2d3715494c517d1` (`pages: 7`, `total_deduped_fills: 12,580`, `truncated_10k: true`)
| Coin | `szDecimals` | `n_fills` | `first_t` | `last_t` | Initial `startPosition` | `reconstructed_api_order` | `reconstructed_tid_order` | `actual_szi` (`before == after`) | `diff_api` | `diff_tid` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `BTC` | 5 | 613 | 1794703844523 | 1794721799070 | `-504.52639` | `-561.28843` | `-561.28843` | `-561.28843` | `0.0` | `0.0` |
| `DOGE` | 0 | 209 | 1794701991341 | 1794718761976 | `10450313.0` | `9456188.0` | `9456188.0` | `9456188.0` | `0.0` | `0.0` |
| `ETH` | 4 | 676 | 1794701726279 | 1794721089671 | `6388.6317` | `5504.8575` | `5504.8575` | `5504.8575` | `0.0` | `0.0` |
| `FARTCOIN` | 1 | 30 | 1794701713956 | 1794708353824 | `1571.9` | `-64474.3` | `-64474.3` | `-64474.3` | `0.0` | `0.0` |
| `HYPE` | 2 | 2,855 | 1794701796713 | 1794721799685 | `-63825.4` | `-38325.83` | `-38325.83` | `-38325.83` | `0.0` | `0.0` |
| `LIT` | 0 | 1 | 1794709050097 | 1794709050097 | `-265029.0` | `-264878.0` | `-264878.0` | `-264878.0` | `0.0` | `0.0` |
| `SOL` | 2 | 1,070 | 1794701921949 | 1794721811296 | `-6594.34` | `-45201.97` | `-45201.97` | `-45201.97` | `0.0` | `0.0` |
| `XRP` | 0 | 347 | 1794701615050 | 1794719669060 | `3368691.0` | `2759554.0` | `2759554.0` | `2759554.0` | `0.0` | `0.0` |
| `ZEC` | 2 | 2,281 | 1794701615598 | 1794721797044 | `2180.2` | `1353.73` | `1353.73` | `1353.73` | `0.0` | `0.0` |

### 3.3 `0xfc667adba8d4837586078f4fdcdc29804337ca06` (`pages: 8`, `total_deduped_fills: 14,412`, `truncated_10k: true`)
| Coin | `szDecimals` | `n_fills` | `first_t` | `last_t` | Initial `startPosition` | `reconstructed_api_order` | `reconstructed_tid_order` | `actual_szi` (`before == after`) | `diff_api` | `diff_tid` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `BTC` | 5 | 42 | 1794702971831 | 1794715793316 | `-115.1037` | `-116.51836` | `-116.51836` | `-116.51836` | `0.0` | `0.0` |
| `ETH` | 4 | 134 | 1794702543494 | 1794714716510 | `4419.3172` | `5363.8097` | `5363.8097` | `5363.8097` | `0.0` | `0.0` |
| `HYPE` | 2 | 1,567 | 1794702477070 | 1794720800734 | `29524.4` | `89841.38` | `89841.38` | `89841.38` | `0.0` | `0.0` |
| `NEAR` | 1 | 99 | 1794703147858 | 1794721240119 | `613875.5` | `602382.5` | `602382.5` | `602382.5` | `0.0` | `0.0` |
| `SOL` | 2 | 763 | 1794702422590 | 1794721631662 | `17205.9` | `-1741.69` | **`-1841.54`** | `-1741.69` | `0.0` | **`-99.85`** |
| `XRP` | 0 | 164 | 1794704348006 | 1794721742751 | `7949996.0` | `8234279.0` | `8234279.0` | `8234279.0` | `0.0` | `0.0` |
| `ZEC` | 2 | 135 | 1794703316520 | 1794720423496 | `1038.06` | `500.97` | `500.97` | `500.97` | `0.0` | `0.0` |

### 3.4 `0xdf9ea6ec3b7109935ccb4fb267e15ac1fb077ab1` (`pages: 6`, `total_deduped_fills: 10,217`, `truncated_10k: true`)
| Coin | `szDecimals` | `n_fills` | `first_t` | `last_t` | Initial `startPosition` | `reconstructed_api_order` | `reconstructed_tid_order` | `actual_szi` (`before == after`) | `diff_api` | `diff_tid` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `HYPE` | 2 | 5,897 | 1780654084687 | 1780936223855 | `-43305.42` | `-20000.01` | `-20000.01` | `-20000.01` | `0.0` | `0.0` |

### 3.5 `0xbf732ea04197942783e34730ed6e0f6099575d58` (`pages: 6`, `total_deduped_fills: 11,449`, `truncated_10k: true`)
| Coin | `szDecimals` | `n_fills` | `first_t` | `last_t` | Initial `startPosition` | `reconstructed_api_order` | `reconstructed_tid_order` | `actual_szi` (`before == after`) | `diff_api` | `diff_tid` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `ARB` | 1 | 210 | 1793996437027 | 1794186323618 | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` |
| `AVAX` | 2 | 464 | 1794255314585 | 1794257369014 | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` |
| `BTC` | 5 | 549 | 1793976031201 | 1794674224302 | `10.0` | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` |
| `ETH` | 4 | 179 | 1794182297292 | 1794626111287 | `0.0` | `0.0` | **`-500.0`** | `0.0` | `0.0` | **`-500.0`** |
| `HYPE` | 2 | 1,344 | 1794040098909 | 1794669360307 | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` |
| `PUMP` | 0 | 552 | 1794000706219 | 1794162753527 | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` |
| `S` | 0 | 907 | 1793996496737 | 1794232893963 | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` |
| `SOL` | 2 | 1,262 | 1793993965463 | 1794255216682 | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` |
| `SUI` | 1 | 822 | 1793996405910 | 1794226838055 | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` |
| `WLD` | 1 | 464 | 1794162115593 | 1794232880877 | `-4427748.7000000002` | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` |
| `WLFI` | 0 | 360 | 1794257138280 | 1794267905791 | `-32932228.0` | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` |
| `ZEC` | 2 | 1,893 | 1793996655102 | 1794703317449 | `15000.0` | `9000.0` | `9000.0` | `9000.0` | `0.0` | `0.0` |
| `kPEPE` | 0 | 303 | 1794175527367 | 1794186316369 | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` | `0.0` |

---

## 4. Zero-Fill Open Positions (`§6.5` Constant Backfill)

Across the `5` verified addresses, **`28` open `clearinghouseState` positions** had **zero fills** inside the paginated recent history window (`truncated_10k: true`):

| Address | Count | Coins & `actual_szi` |
|---|---:|---|
| `0xfc27136e42af1732ddc9ce2605ea9bff1b959d9d` | 1 | `NEAR: -319997.7` |
| `0x7fdafde5cfb5465924316eced2d3715494c517d1` | 26 | `ADA: -26179.0`, `ASTER: -830322.0`, `AVAX: 26309.1`, `BNB: -469.711`, `ENA: -1810.0`, `GOAT: -327719.0`, `GRAM: -36567.0`, `INJ: -14229.5`, `LINEA: -126660.0`, `LTC: -103.62`, `MON: -13176347.0`, `NEAR: -119065.1`, `ONDO: -241204.0`, `PENGU: 129017.0`, `PONS: 14517.0`, `POPCAT: -4674.0`, `PUMP: -88532548.0`, `TAO: 2.19`, `UNI: -10440.0`, `VVV: -11305.23`, `WIF: -1226.0`, `WLD: -2470879.1`, `XMR: -4825.163`, `XPL: -17211569.0`, `ZRO: -89716.2`, `kPEPE: -201212347.0` |
| `0xfc667adba8d4837586078f4fdcdc29804337ca06` | 1 | `PAXG: 86.153` |
| `0xdf9ea6ec3b7109935ccb4fb267e15ac1fb077ab1` | 0 | — |
| `0xbf732ea04197942783e34730ed6e0f6099575d58` | 0 | — |

All `28` represent positions opened prior to the API's `10,000`-fill rolling cutoff and held unchanged over `[coverageStart, T_ref]`, confirming `§6.5`'s constant backfill rule (backfill `actual_szi` and `entryPx` across `[coverageStart, T_ref]` for zero-fill open positions; positions prior to `coverageStart` remain unknown).

---

## 5. Why `(time, tid)` Fails & Backward-Walk Sanity Check

1. **Root cause of the 3 `(time, tid)` failures**:
   As documented in [`fill-dir.md`](fill-dir.md#L167-L190), `tid` is non-monotonic within a millisecond. When multiple fills for a coin share the earliest observed millisecond `first_t` (whether starting from `0.0` like `0xbf732ea0` `ETH` or truncated mid-position like `0xfc27136e` `AVAX` and `0xfc667adb` `SOL`), sorting by `(time, tid)` reorders intra-millisecond fills and picks a **later execution's `startPosition`** as the starting position, producing immediate errors (`+400.0 AVAX`, `-99.85 SOL`, `-500.0 ETH`). Even when `first_t` has a single fill so final `szi` happens to match (`48/51` coins), intra-ms `tid` inversions scramble `Short > Long` / `Long > Short` flip splits and round-trip zero-crossings (`§5.3`).
2. **Backward-walk verification option**:
   In `reconstruct.py` (`§6.5`), walking backward from `clearinghouseState.szi` through deduplicated fills in reverse `(time, api_seq_index)` order (`prev_szi = curr_szi - delta_sz`) recovers the exact `startPosition` at every fill — confirming zero missing fills in paginated main-perp history and catching any future API anomaly (including rare self-liquidation `liquidation.liquidatedUser == user` fills if encountered).
3. **Action required**:
   Amending `§5.2` and `§5.3` (`【規範】`) to replace `(time, tid)` with API execution sequence `(time, api_seq_index)` requires user approval via [`0001-intra-ms-fill-ordering.md`](../proposals/0001-intra-ms-fill-ordering.md).

<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Task 0.5 — `userFunding` & `cumFunding` Schema, Pagination & Sign Semantics

## 1. Endpoints & Wire Schemas
Probed via `POST https://api.hyperliquid.xyz/info`:
```bash
curl -sS -X POST https://api.hyperliquid.xyz/info \
  -H "Content-Type: application/json" \
  -d '{"type":"userFunding","user":"0xfc27136e42af1732ddc9ce2605ea9bff1b959d9d","startTime":0}'
```
And `clearinghouseState` (`{"type": "clearinghouseState", "user": addr}`) for `assetPositions[].position.cumFunding`. Weight cost: `userFunding = 20 + ceil(n / 20)`; `clearinghouseState = 2`.

### `userFunding` Item Schema
```json
{
  "time": 1727395200000,
  "hash": "0x0000000000000000000000000000000000000000000000000000000000000000",
  "delta": {
    "type": "funding",
    "coin": "BTC",
    "usdc": "0.233333",
    "szi": "-0.01",
    "fundingRate": "0.0000273",
    "nSamples": 13
  }
}
```
Key wire-format properties verified across 11 recorded addresses (`fixtures/api/userFunding_*.json` and full dumps):
1. **`nSamples` is nullable (`int | null`)**:
   - Historical events older than ~5–9 days are **daily-aggregated at `00:00:00.000Z`** with integer `nSamples` (`1..24`, e.g., `5`, `13`, `24`), where `szi` reflects the sample-averaged size (e.g., `"64.7693573333"`).
   - Recent events are **unaggregated hourly** settlements (`:00:00.001Z`–`:00:00.083Z`) with `"nSamples": null`.
   - Observed transition on `0x1f250df59a777d61cb8bd043c12970f3afe4f925`: `94` integer-`nSamples` events (`1749427200000` .. `1793923200000`, `2025-06-09T00:00:00Z` .. `2026-11-06T00:00:00Z`, last having `nSamples: 15`) followed by `94` hourly events with `"nSamples": null` (`1794319200056` .. `1794718800001`, `2026-11-10T14:00:00.056Z` .. `2026-11-15T05:00:00.001Z`).
   - Verbatim recent hourly example (`0x1f250df5`, `time: 1794718800001`):
     ```json
     {"time": 1794718800001, "hash": "0x0000000000000000000000000000000000000000000000000000000000000000", "delta": {"type": "funding", "coin": "BTC", "usdc": "-811.664999", "szi": "1000.0", "fundingRate": "0.0000125", "nSamples": null}}
     ```
2. **`hash` is uniformly zeroed (`0x000...0`)**: Across all 11 addresses (`4,189` recorded events), `hash` is identically `"0x0000000000000000000000000000000000000000000000000000000000000000"`, so `hash` alone cannot distinguish co-timestamped events across coins.
3. **HIP-3 builder-dex coins appear in `userFunding`**: In `userFunding_0xfc27136e.json` (first 500 events, `1776038400000` .. `1780963200000`), **`442 / 500`** events belong to `28` `xyz:*` HIP-3 markets (`xyz:AAPL`, `xyz:CL`, `xyz:GOLD`, `xyz:NVDA`, `xyz:SP500`, etc.) alongside `BTC`. Verbatim HIP-3 example from `0xfc27136e`:
   ```json
   {"time": 1777939200000, "hash": "0x0000000000000000000000000000000000000000000000000000000000000000", "delta": {"type": "funding", "coin": "xyz:CL", "usdc": "-0.217388", "szi": "48.0228571429", "fundingRate": "0.0000020957", "nSamples": 21}}
   ```
   Because HyperPod v1 tracks **only the main perp dex** (`§1`, `§5.2`), `userFunding` aggregation MUST filter `delta.coin` using the canonical `ADR-0002` main-perp predicate (`coin in main_perp_universe` from `meta.universe` including `isDelisted: true`, and `"#"`, `":"`, `"@"`, `"/"` not in `coin` and `coin != "PURR/USDC"`) before accumulating funding totals.

### `clearinghouseState.assetPositions[].position.cumFunding` Schema
Verbatim excerpt from `fixtures/api/clearinghouseState_0xfc27136e.json` (`BTC` short `szi = "-952.17723"`):
```json
"cumFunding": {
  "allTime": "-1254857.3119709999",
  "sinceOpen": "-671562.358391",
  "sinceChange": "-772.848931"
}
```
Note that `clearinghouseState` only exposes `cumFunding` for **currently open positions**; closed positions vanish from `assetPositions`, making `userFunding` mandatory for complete historical funding accounting (`§5.4`).

## 2. Pagination Limits, Order & Boundary Deduplication
Probed full pagination from `startTime: 0` on active master `0x7fdafde5cfb5465924316eced2d3715494c517d1`:

| Page | `len` | `first_t` (ms) | `first_t` (UTC) | `last_t` (ms) | `last_t` (UTC) | Overlap With Prev Page |
|---:|---:|---:|---|---:|---|---|
| `0` | `500` | `1727395200000` | `2024-09-27T00:00:00Z` | `1736208000000` | `2025-01-07T00:00:00Z` | — |
| `1` | `500` | `1736208000000` | `2025-01-07T00:00:00Z` | `1739491200000` | `2025-02-14T00:00:00Z` | `1736208000000` |
| `2` | `500` | `1739491200000` | `2025-02-14T00:00:00Z` | `1742774400000` | `2025-03-24T00:00:00Z` | `1739491200000` |
| `3` | `500` | `1742774400000` | `2025-03-24T00:00:00Z` | `1745712000000` | `2025-04-27T00:00:00Z` | `1742774400000` |
| `4` | `500` | `1745712000000` | `2025-04-27T00:00:00Z` | `1748304000000` | `2025-05-27T00:00:00Z` | `1745712000000` |

Verified pagination & retention rules:
- **Page limit & sort order**: Returns up to **`500` events per response**, sorted in **non-decreasing (ascending) `time` order**.
- **No 10,000-item truncation**: Unlike `userFillsByTime`, `userFunding` retains full history back to 2024 inception (e.g., `0xdf9ea6ec` begins `1705881600000` = `2024-01-22T00:00:00Z`, `0x7fdafde5` begins `2024-09-27`).
- **Inclusive `startTime = last_t` + composite dedup key `(time, hash, delta.coin)`**:
  - Every page boundary overlaps at exact `last_t` (`page[k].last_t == page[k+1].first_t`).
  - Crucially, aggregated `00:00:00Z` events across multiple coins share the **exact same `time`** (and identical `0x000...0` `hash`): in `0xfc27136e`, prior days contain `29` co-timestamped coin events (e.g., `1780444800000`), while page `0` hits the `500`-event cap mid-timestamp at `1780963200000` after `8` coins (`BTC`, `xyz:AAPL`, `xyz:AMZN`, `xyz:BABA`, `xyz:BRENTOIL`, `xyz:CBRS`, `xyz:CL`, `xyz:COIN`), leaving the remaining coins at `1780963200000` on page `1`.
  - Advancing with `startTime = last_t + 1` would **silently drop** remaining coins on a split timestamp. Pagination MUST use inclusive `startTime = last_t` (identical to `§5.2` `userFillsByTime`), deduplicate on **`(time, hash, delta.coin)`**, and terminate when `len(page) < 500` or `added == 0`.

## 3. Sign Convention Proof (`userFunding` vs `cumFunding`)
Cross-checked `userFunding` events against live `clearinghouseState.cumFunding` across 3 active accounts (`0xdf9ea6ec`, `0x1f250df5`, `0xfc27136e`):

| Address | Coin | Position `szi` | Sample `fundingRate` | `userFunding.delta.usdc` | `cumFunding.sinceChange` | `cumFunding.sinceOpen` | `cumFunding.allTime` |
|---|---|---:|---:|---:|---:|---:|---:|
| `0xdf9ea6ec` | `HYPE` | `-20000.01` (Short) | `+0.0000042854` (`03:00Z`)<br>`-0.0000077548` (`04:00Z`)<br>`-0.000010106` (`05:00Z`) | `+5.666765`<br>`-10.256286`<br>`-13.320503`<br>(`14d sum = +5427.428829`) | `-68710.185309` | `-214643.961728` | `-214654.338437` |
| `0x1f250df5` | `BTC` | `+1000.0` (Long; `+600.0` at `02:00Z`) | `-0.0000054697` (`02:00Z`)<br>`+0.0000125` (`03:00Z`)<br>`+0.0000125` (`04:00Z`)<br>`+0.0000125` (`05:00Z`) | `+205.543163`<br>`-811.666249`<br>`-810.312499`<br>`-811.664999` | `+2433.643747` | `+2228.100584` | `+5037739.434842` |
| `0xfc27136e` | `BTC` | `-952.17723` (Short) | `+0.0000125` (`05:00Z`) | `+772.848931` (`05:00Z`) | `-772.848931` | `-671562.358391` | `-1254857.311971` |
| `0xfc27136e` | `ETH` | `-10.5187` (Short) | `+0.0000111706` (`04:00Z`)<br>`+0.0000091372` (`05:00Z`) | `+0.213756`<br>`+0.174262` | `-0.388018` | `-0.388018` | `-599614.769387` |
| `0xfc27136e` | `SOL` | `-42461.86` (Short) | `+0.0000125` (`05:00Z`) | `+37.639785` (`05:00Z`) | `-37.639785` | `-13079.980302` | `-22062.344369` |
| `0xfc27136e` | `BNB` | `+1256.146` (Long) | `+0.0000125` (`03:00Z`)<br>`+0.0000041352` (`04:00Z`)<br>`-0.0000031348` (`05:00Z`) | `-9.328799`<br>`-3.087174`<br>`+2.344017` | `+10.071956` | `+1391.574629` | `-5610.495875` |

### Exact Mathematical Identity Proofs
1. **`0x1f250df5` `BTC` (Long `1000.0`)**:
   - Last 3 unchanged-size events (`03:00Z`–`05:00Z`, `rate = +0.0000125`):
     $$-811.666249 + (-810.312499) + (-811.664999) = -2433.643747$$
     Observed `cumFunding.sinceChange` = **`+2433.643747`** (exact negation to 6 decimal places!).
   - Including the prior `02:00Z` open event (`szi = 600.0`, `rate = -0.0000054697`, `usdc = +205.543163`):
     $$+205.543163 - 2433.643747 = -2228.100584$$
     Observed `cumFunding.sinceOpen` = **`+2228.100584`** (exact negation to 6 decimal places!).
2. **`0xfc27136e` `BTC`, `SOL`, `ETH`, `BNB`**:
   - `BTC` (`05:00Z` after size update `-952.17623 -> -952.17723`): `delta.usdc = +772.848931` vs `sinceChange = -772.848931`.
   - `SOL` (`05:00Z` after size update `-42311.77 -> -42461.86`): `delta.usdc = +37.639785` vs `sinceChange = -37.639785`.
   - `ETH` (`04:00Z` + `05:00Z` after flip from `+69.4922` to `-10.5187`): `0.213756 + 0.174262 = +0.388018` vs `sinceOpen = sinceChange = -0.388018`.
   - `BNB` (`03:00Z`–`05:00Z` unchanged `+1256.146`): `-9.328799 - 3.087174 + 2.344017 = -10.071956` vs `sinceChange = +10.071956`.

### Sign Semantics Summary
- **`userFunding.delta.usdc`** uses **user cashflow perspective**: **`+` = received by user, `-` = paid by user** (`sign(usdc) = -sign(szi * fundingRate)`).
- **`clearinghouseState.cumFunding`** (`allTime`, `sinceOpen`, `sinceChange`) uses **cumulative paid-cost perspective**: **`+` = net funding paid by user, `-` = net funding received by user**.
- Therefore, **`userFunding.delta.usdc` and `clearinghouseState.cumFunding` have strictly opposite signs** (`cumFunding.sinceChange == -Σ_{sinceChange} delta.usdc`).

## 4. Clarification of `§5.4` `fundingShare` & `state/daily` Fields
- `§5.4` **【規範】** defines:
  $$\text{fundingShare} = \frac{|\sum \text{funding\_usd}|}{\max(|\text{pnlAllTime}|, 1)}$$
- In `state/daily/addresses/{shard}.json.gz` (`§4`), incremental `userFunding` updates (after main-perp `delta.coin` filtering) maintain:
  - `cumFundingNet`: signed chronological sum $\sum \text{float}(\text{delta.usdc})$ (`+` net received, `-` net paid).
  - `cumFundingAbs`: gross unsigned sum $\sum |\text{float}(\text{delta.usdc})|$ (retained strictly as an internal diagnostic metric).
- **`fundingShare` MUST compute `abs(cumFundingNet) / max(abs(pnlAllTime), 1.0)`** (`|Σ delta.usdc|`, **not** `Σ |delta.usdc|`!). Taking `abs()` after chronological netting:
  1. Makes `fundingShare` sign-convention-invariant across receipts vs payments while capturing both closed and open position history.
  2. Avoids inflating `fundingShare` when alternating long/short or multi-coin funding inflows and outflows cancel chronologically (e.g., `0xfc27136e` `SOL` receiving `+37.64` after paying `-83.98`, or `0x1f250df5` `BTC` alternating `+205.54` and `-811.67`).
- Because `userFunding` backfills can span thousands of events per trader, keeping `userFunding` at Stage 2 step 5 (`§5.1`, fetched only for addresses passing steps 1–4 and active whitelist members) prevents wasted API quota on high-turnover MMs and vaults.

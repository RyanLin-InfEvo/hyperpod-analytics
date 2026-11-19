<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Phase 3 Live Verification — First Whitelist Generation & Gate Funnel Analysis

- **Date:** `2026-11-18`
- **Specification Scope:** `docs/implementation_plan.md` `§5.1`–`§5.6`, `config/pipeline.yaml` (`schemaVersion: 1`, criteria `version: 1`, `hash: sha256:a32d85a12385196be288f42d78998fe2cdda9f8dd4aa1bf5a42e339eae2c6f4b`)
- **Status:** Verified end-to-end against live Hyperliquid S3 Leaderboard + Info API endpoints, and validated against `schemas/{whitelist,criteria,candidates}.schema.json` with `check-jsonschema`.

---

## 1. CLI Commands Executed

All live runs targeted an isolated temporary workspace `/tmp/hyperpod-phase3-live` (cleaned up before committing) using local rate-limiter budget (`1,000 weight/min` from `config/pipeline.yaml` `ratelimit.localBudgetPerMin`):

```bash
# 1. Stage 1 Discovery against live S3 leaderboard
nix-shell --run "cd pipeline && uv run hyperpod discover \
  --state-dir /tmp/hyperpod-phase3-live/state"

# 2. Top-35 Volume-Ordered Stage 2 Score Run (1,000 weight/min local budget)
nix-shell --run "cd pipeline && uv run hyperpod score \
  --state-dir /tmp/hyperpod-phase3-live/state \
  --data-dir /tmp/hyperpod-phase3-live/data \
  --limit 35 --max-fill-pages 5 --max-funding-pages 5 --local-budget"

# 3. Targeted Live Evaluation of Swing & Steady-ROI Stage 1 Slice
#    (persisting passing + near-pass directional candidates into shard state)

# 4. Offline Re-Score & Schema Validation across all 49 persisted candidates
nix-shell --run "cd pipeline && uv run hyperpod score \
  --state-dir /tmp/hyperpod-phase3-live/state \
  --data-dir /tmp/hyperpod-phase3-live/data --offline \
  && check-jsonschema --schemafile ../schemas/whitelist.schema.json /tmp/hyperpod-phase3-live/data/whitelist.json \
  && check-jsonschema --schemafile ../schemas/criteria.schema.json /tmp/hyperpod-phase3-live/data/criteria.json \
  && check-jsonschema --schemafile ../schemas/candidates.schema.json /tmp/hyperpod-phase3-live/data/candidates.json"
```

---

## 2. Stage 1 Leaderboard Funnel (`hyperpod discover`)

- **Run Timestamp (`generatedAt`):** `1794990594168` (`2026-11-18T08:29:54Z`)
- **Leaderboard Fallback Used:** `false`
- **Total Leaderboard Rows:** **`48,181`**
- **Passed Stage 1 Prefilter:** **`1,720`** (`3.57%`)
- **Seed / Blocklist Additions:** `seedAddedCount: 0`, `blocklistedCount: 0`, `activeWhitelistAddedCount: 0`

| Stage 1 Gate (`config/pipeline.yaml`) | Condition | Rejected Rows | Notes |
|---|---|---:|---|
| `stage1.minAccountValue` | `accountValue >= 1,000,000` (and `> 0`) | 45,793 | Primary size filter |
| `stage1.minPnlAllTime` | `allTime.pnl > 0` | 26,508 | Multi-reason counts overlap per row |
| `stage1.minPnlMonthRatio` | `month.pnl > -0.2 * accountValue` | 12,090 | Recent monthly drawdown floor |
| `stage1.maxTurnoverMonth` | `month.vlm / accountValue <= 150.0` | 7,304 | Pre-excludes highest-turnover MMs |
| `blocklist.txt` | `addr not in blocklist` | 0 | Clean baseline |

> **Ordering Note:** `LeaderboardResponse` rows from Hyperliquid S3 arrive ordered predominantly with the highest-volume/highest-TVL accounts first (`#0` has `$6.26B` 30d volume on `$71.46M` AV = `87.6x` turnover; `#4` is labeled `"Auros"` at `119.9x`). Consequently, sampling only `[:35]` evaluates institutional HFT/MMs, vaults, and `10k`-truncated accounts rather than swing directional traders.

---

## 3. Stage 2 Empirical Findings & Live Wire Verification

### 3.1 Top-35 Live Score Run vs. Combined 49-Candidate State

| Metric | Initial Top-35 Run (`--limit 35`) | Combined Offline Score (`49` evaluated) |
|---|---:|---:|
| `evaluatedCount` | `33` (`+ 4` expanded subAccounts `>= $1M` across shard queue) | `49` |
| `passedCount` | `0` | **`1`** |
| `activeWhitelistCount` | `0` | **`1`** |
| `roles` in shards | — | `user: 42`, `subAccount: 4`, `vault: 3` |
| `abstraction` modes | — | `disabled: 21`, `unifiedAccount: 20`, `portfolioMargin: 4`, `default: 1`, `None (role rej): 3` |
| `totalRequests` / `totalWeight` | `343` / `20,699` (`0` HTTP 429s, `1,205.2s` elapsed) | `0` / `0` (`0.156s` offline replay) |
| `rejectedBy.role` | `2` | `3` |
| `rejectedBy.hft` | `24` | `29` |
| `rejectedBy.mm` | `9` | `11` |
| `rejectedBy.hedge` | `0` | `1` |
| `rejectedBy.passive` | `19` | `25` |
| `rejectedBy.sample` | `27` | `37` |
| `rejectedBy.mdd` | `26` | `28` |
| `rejectedBy.pf` | `25` | `31` |
| `rejectedBy.lucky` | `9` | `12` |

### 3.2 Live Wire Model Fix Discovered During Real API Testing (`SpotBalance`)

Running `task-330` and `task-446` against live accounts surfaced two optional wire fields in `spotClearinghouseState` / `subAccounts[].spotState` absent from the Phase 0 11-address fixture set:
1. **`spotHold` string field:** Unified/portfolio-margin spot balances (e.g., `0x9321d811...`, `0x523852be...`, `0x92cbb22a...`, `0x9aa58935...`, `0x0fd6f65d...`) can include `"spotHold": "1364499.6734719994"`.
2. **Omitted `token` on outcome/HIP-2 spot balances:** Balances with synthetic coin identifiers like `"+14730"` / `"+392730"` (e.g., `0x49e96e25...` subaccounts, `0x7bfee911...`) omit the integer `"token"` key.

**Resolution (`pipeline/src/hyperpod/models/api.py`, `pipeline/tests/test_info_api.py`):**
Added TDD regression test `test_spot_clearinghouse_state_allows_missing_token_and_spot_hold` and updated `SpotBalance` so `token: NonNegInt | None = None` and `spotHold: DecimalStr | None = None`. Re-verified against previously failing addresses `0x9321d8117e73b0c79035f0e87debcfd8dbb1d75a` (`7` balances) and `0x49e96e255ba418d08e66c35b588e2f2f3766e1d0` (`5` subaccounts).

### 3.3 Gate Empirical Behavior (`>150` Live Candidates Inspected)

Across the top-35 volume run plus `>120` sampled swing/steady-ROI Stage 1 candidates (`0.5x–90x` turnover, `month.pnl > 0`):
1. **Why top-volume Stage 1 whales fail `sample` / `hft`:** Accounts running HFT/MM strategies exhaust Hyperliquid's `10,000`-fill rolling API cap in `0.36–7.75` days, causing `closedTrades` within the truncated window to be `0–16` (`sample`) and `fillsPerDay > 500` (`hft`). This confirms the `§5.5` two-part HFT guard (`fillsPerDay > 500` OR `(truncated && spanDays < 7)`) works as intended.
2. **Why `§5.4 maxDrawdown` rejects many multi-year profitable whales (`§11.2` observation — formula NOT modified):** Under the unchanged `【規範】` `§5.4` formula `DD_t = (max_{s<=t} PnL_s - PnL_t) / AV_{argmax}` on `perpAllTime`, accounts that started with a small test deposit (`$5–$1,400`) or withdrew nearly all equity down to `$0.01–$1.42` early in their history, then later deposited millions and lost `$50k–$4M` before recovering, record mathematical `maxDrawdown > 0.45` (often `1.8x–4,634x`!). Examples observed verbatim:
   - `0xfb941d2344b72ea9101400392b818173772d0ddf` (`nTrades=40`, `pfR=4.045`, `winRate=80%`, `AV=$11.32M`, `pnlAllTime=+$15.19M`): passes 9/10 gates, rejected solely by `mdd = 1.857` because `peakAV` at index 13 (`$1.48M`) later fell to `$29.3k` (`-$634k` PnL trough -> `DD = 1.428` -> `1.857`) prior to scaling up to `$11.3M`.
   - `0x7fdafde5cfb5465924316eced2d3715494c517d1` (`BobbyBigSize`): early peak `+$54` on `$1,377` AV followed by `-$4.215M` loss after multimillion-dollar inflows yields `mdd = 3060.80`.
   *(No formula changes were made; flagged for user awareness during Phase 3 review.)*

---

## 4. Passing Whitelist & Notable Evaluated Candidates (`49`-Address Snapshot)

### 4.1 Passing Whitelist Addresses (`passed = 1`, `score = 1.0`)

| Address (`id`) | Equity / Source | History / Trades | `pfRobust` (`pf`) | `winRate` / `topWinShare` | `maxDrawdown` | `fillsPerDay` / `medianHold` | `makerRatio` / `turnoverMonth` | `fundingShare` / `hedgeRatio` | Open Main-Perp Coins |
|---|---|---|---|---|---|---|---|---|---|
| `0xe9ec1d90652e13521623fb048f1c77883c597859` (`id: 0`) | `$1,265,774` (`perp+spot`, `unifiedAccount`) | `84.8d` / `68` (`trunc: false`) | `1.877` (`2.201`) | `64.7%` / `0.147` | `0.383` | `27.3/d` / `140.1m` | `0.967` / `25.02x` | `0.021` / `0.00004` | `BTC`, `ETH`, `SOL` |

*(Note: `0xe9ec1d...` also holds a subaccount `0x5494ac601f0e61bdb4db6c84775ba4ee2a3618cf` below `$1M` and had `xyz:HOOD` funding events, which our strict `meta.universe` filter excluded from main-perp metrics while retaining `xyz:HOOD` in `lastFundingCoins` for cursor dedup.)*

### 4.2 Representative Evaluated Directional / Filtered Whales (`candidates.json`)

| Address | Role / Abs | AV (`$`) | `pnlAllTime` (`$`) | `nTrades` / `hDays` | `pfRobust` | `maxDrawdown` | `fpd` / `hold(m)` | `rejectReasons` |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `0xb45f5c538dd6573cae750d29b22117e45e0f9a6d` | `user` / `unifiedAccount` | `1,100,144` | `768,319` | `18` / `47.9d` | `496.81` | `0.240` | `34.6` / `16,192.1m` | `['sample']` *(18 < 20 trades; 53 open positions)* |
| `0xfb941d2344b72ea9101400392b818173772d0ddf` | `user` / `unifiedAccount` | `11,319,476` | `15,194,669` | `40` / `152.6d` | `4.045` | `1.857` | `16.6` / `3,213.0m` | `['mdd']` *(9/10 gates pass)* |
| `0x015354106478dda69c4aae3c0cf801290b738052` | `user` / `unifiedAccount` | `7,532,196` | `14,481,572` | `18` / `617.8d` | `21.135` | `0.372` | `79.0` / `1,186.3m` | `['passive', 'sample']` |
| `0xf5a523b171032c060d49c39fbf2e9bec473e1286` | `user` / `disabled` | `1,890,358` | `2,400,975` | `77` / `50.9d` | `1.826` | `0.046` | `355.6` / `2.97m` | `['hft', 'mm']` |
| `0xf34d9cf4829ce1d301ea29f92450c47cdd8e25c2` | `user` / `portfolioMargin` | `1,139,120` | `248,391` | `8` / `18.6d` | `null` | `0.130` | `365.8` / `22.8m` | `['sample']` |
| `0x0fd6f65d35cf13ae51795036d0ae9af42f3cbcb4` | `user` / `portfolioMargin` | `3,857,919` | `1,707,910` | `13` / `543.9d` | `0.0` | `0.130` | `75.6` / `1,943.9m` | `['hedge', 'sample', 'pf', 'lucky']` (`hedgeRatio: 1.0`) |
| `0x023a3d058020fb76cca98f01b3c48c8938a22355` *(Auros)* | `user` / `disabled` | `5,170,873` | `58,898,372` | `4` / `0.9d` | `null` | `1.415` | `9,023.0` / `74.9m` | `['hft', 'passive', 'sample', 'mdd']` |

---

## 5. Five Spotlight Addresses for User Manual Spot-Check (`§13`)

Please cross-check these 5 diverse live addresses on the Hyperliquid Explorer to confirm metric accuracy, gate behavior, and directional-trader quality:

1. **Passing Whitelist Trader (`PASS`, `id: 0`, `score: 1.0`)**
   - **Address:** `0xe9ec1d90652e13521623fb048f1c77883c597859`
   - **Explorer:** https://app.hyperliquid.xyz/explorer/address/0xe9ec1d90652e13521623fb048f1c77883c597859
   - **Check Focus:** `unifiedAccount` `perp+spot` equity (`$1.266M`), `68` closed main-perp trades over `84.8d`, `winRate = 64.7%`, `pfRobust = 1.877`, `topWinShare = 0.147`, `mdd = 0.383`, open short `BTC`/`ETH`/`SOL` positions, clean exclusion of `xyz:HOOD` HIP-3 funding.

2. **Borderline Swing Portfolio Whale (`rejected: ["sample"]`)**
   - **Address:** `0xb45f5c538dd6573cae750d29b22117e45e0f9a6d`
   - **Explorer:** https://app.hyperliquid.xyz/explorer/address/0xb45f5c538dd6573cae750d29b22117e45e0f9a6d
   - **Check Focus:** `$1.100M` AV, `+$768.3k` PnL, `pfRobust = 496.81`, `88.9%` win rate, `11.2d` median hold across `53` currently open perp coins; fails strictly because `18` completed round-trip trades `< 20`.

3. **Multi-Million Swing Whale Blocked Solely by Early-Deposit `maxDrawdown` (`rejected: ["mdd"]`)**
   - **Address:** `0xfb941d2344b72ea9101400392b818173772d0ddf`
   - **Explorer:** https://app.hyperliquid.xyz/explorer/address/0xfb941d2344b72ea9101400392b818173772d0ddf
   - **Check Focus:** `$11.32M` AV, `+$15.19M` all-time PnL, `40` closed trades, `80%` win rate, `pfRobust = 4.045`, `turnoverMonth = 9.47x`, open `HYPE` position; rejected by `mdd = 1.857` due to early-lifecycle equity scaling (`$1.48M` -> `<$30k` prior to later multimillion deposits).

4. **Spot-Hedged Delta-Neutral Account (`rejected: ["hedge", "sample", "pf", "lucky"]`)**
   - **Address:** `0x0fd6f65d35cf13ae51795036d0ae9af42f3cbcb4`
   - **Explorer:** https://app.hyperliquid.xyz/explorer/address/0x0fd6f65d35cf13ae51795036d0ae9af42f3cbcb4
   - **Check Focus:** `portfolioMargin` (`$3.86M` AV, `+$1.71M` PnL) holding open perp shorts in `ETH`/`HYPE` matched 1:1 with spot holdings (`hedgeRatio = 1.0`, `spotHold` field validated).

5. **Fast-Turnover Market Maker / Scalper (`rejected: ["hft", "mm"]`)**
   - **Address:** `0xf5a523b171032c060d49c39fbf2e9bec473e1286`
   - **Explorer:** https://app.hyperliquid.xyz/explorer/address/0xf5a523b171032c060d49c39fbf2e9bec473e1286
   - **Check Focus:** `$1.89M` `disabled` perp AV, `pfRobust = 1.826`, `mdd = 0.046`, `77` trades, but `medianHoldMinutes = 2.97m` (`< 5m`), `makerRatio = 0.966` (`> 0.9`), and `turnoverMonth = 51.1x` (`> 30x`) — illustrating exact `hft` + `mm` gate exclusion despite strong headline stats.

---

## 6. Browser Spot-Check Verification (`2026-11-19`)

All 5 spotlight addresses were verified via headless browser (`1d8f0bde-bdbe-4af4-b159-9cc3b92f8bf8`, HTTP `200`, zero CAPTCHA/bot gates) across both official [`app.hyperliquid.xyz`](https://app.hyperliquid.xyz) and public analytics UI [`hyperdash.com`](https://hyperdash.com):

1. **Official Hyperliquid Explorer (`app.hyperliquid.xyz/explorer/address/<addr>`):**
   - Confirmed all `5` addresses are active live accounts (`1-10 of 300` paginated transactions extending to `2026-11-18`–`11-19`, including `Place order` / `Cancel order` / `BorrowLend` / `SendToEvmWithData`).
   - **UI Scope Note:** `/explorer/address/<addr>` displays only raw transaction logs (`Hash`, `Action`, `Block`, `Time`, `User`) without Account Value, PnL, or Open Positions; `/portfolio` shows `$0.00` without an active wallet signature and offers no arbitrary-address lookup.
2. **Independent Numerical Cross-Check (`hyperdash.com/address/<addr>`):**

| # | Address | HyperPod (`2026-11-18`) AV / All-Time PnL | Hyperdash (`2026-11-19`) AV (Perp / Spot) & Combined PnL | Hyperdash Open Positions, Style & Match Notes |
|---|---|---|---|---|
| 1 | `0xe9ec1d90652e13521623fb048f1c77883c597859` | `` `$1,265,774` `` (`perp+spot`) / `` `+$270,732` `` | **AV:** `` `$1,265,164.57` `` (Perps `` `$1,265,104.33` ``, Spot `` `$60.24` ``)<br>**Combined PnL:** `` `+$355,873.78` `` (`30D Perps: +$302,224.91`) | **`4` Shorts (`100% Short`, `Swing`):** `SOL` (`-12.39K`), `HOOD`, `BTC`, `ETH`; exact match to HyperPod `BTC`/`ETH`/`SOL` main-perp shorts + `xyz:HOOD` HIP-3 isolation. |
| 2 | `0xb45f5c538dd6573cae750d29b22117e45e0f9a6d` | `` `$1,100,144` `` (`perp+spot`) / `` `+$768,319` `` | **AV:** `` `$1,103,227.57` `` (`Perps $1.103M`, `Spot $0.00`)<br>**Combined PnL:** `` `+$776,793.80` `` | **`63` Open Positions** (`23% Long / 77% Short`, `BTC 40x` short, `AVAX 10x` short), `82.6%` win rate across `23` lifetime trades (`18` recent main-perp closed trades in HyperPod). |
| 3 | `0xfb941d2344b72ea9101400392b818173772d0ddf` (`RTB 2`) | `` `$11,319,476` `` / `` `+$15,194,669` `` | **AV:** `` `$9,011,345.46` `` (`Perps $9.011M`, `Spot $0.00`)<br>**Combined PnL:** `` `+$15,263,226.80` `` | **`4` Positions (`Swing`, `71.8%` win rate):** `NVDA`, `XYZ100`, `HYPE` (`+15K`), `BTC`; `$2.3M` AV delta reflects recent `SendToEvmWithData` / `BorrowLend` outflows seen on Explorer. |
| 4 | `0x0fd6f65d35cf13ae51795036d0ae9af42f3cbcb4` | `` `$3,857,919` `` (`portfolioMargin`) / `` `+$1,707,910` `` | **AV:** `` `$4,459,620.02` `` (Perps `` `$941,908.55` ``, Spot `` `$3,517,711.47` ``)<br>**Combined PnL:** `` `+$1,688,024.86` `` | **`HYPE 2x` Short (`-37.5K HYPE`, `+$412.0K`)** offset by `` `$3.52M` `` Spot balance — independently confirms HyperPod `hedgeRatio = 1.0` & `spotHold` handling. |
| 5 | `0xf5a523b171032c060d49c39fbf2e9bec473e1286` | `` `$1,890,358` `` (`disabled` perp) / `` `+$2,400,975` `` | **AV:** `` `$1,891,062.33` `` (**Perps** `` `$1,890,358.00` ``, Staked `` `$704.32` ``)<br>**Combined PnL:** `` `+$2,400,968.48` `` | **`Scalping` (`Median Trade Duration 0h 2m`)**, `525` trades, `71.0%` win rate — independently confirms HyperPod `medianHoldMinutes = 2.97m` (`hft` + `mm` exclusion). |


<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Task 0.8 — Subaccounts Role Ratio, Master/Sub Isolation & Default Mode

## Survey Scope & Methodology
Sampled the top **200 Stage 1 candidates** (out of `1,661` passing Stage 1 from the `47,662`-row `leaderboard` snapshot) and the top **50 overall leaderboard rows** by querying:
- `{"type": "userRole", "user": addr}`
- `{"type": "subAccounts", "user": addr}` (for `role == "user"`)
- `{"type": "clearinghouseState", "user": master}` (for masters with `subAccounts`)

## Role Distribution
### 1. Top 200 Stage 1 Candidates
| `userRole` | Count | Share |
|---|---:|---:|
| `user` | 194 | 97.0% |
| `vault` | 6 | 3.0% |
| `subAccount` | 0 | 0.0% |
| `agent` / `missing` | 0 | 0.0% |

Verbatim `vault` examples found inside Stage 1 candidates:
- `0x31ca8395cf837de08b24da3f660e77761dfb974b` (`lb_av = $2,986,420.82`)
- `0x010461c14e146ac35fe42271bdc1134ee31c703a` (`lb_av = $3,224,888.51`)
- `0xb0a55f13d22f66e6d495ac98113841b2326e9540` (`lb_av = $30,000,000.00`)
- `0x07fd993f0fa3a185f7207adccd29f7a87404689d` (`lb_av = $7,086,126.23`)
- `0xd6e56265890b76413d1d527eb9b75e334c0c5b42` (`lb_av = $5,585,788.16`)
- `0x1e37a337ed460039d1b15bd3bc489de789768d5e` (`lb_av = $12,162,210.47`)

### 2. Top 50 Leaderboard Rows (Pre-Stage 1)
- `user`: `49` (`98.0%`)
- `vault`: `1` (`2.0%`, `0xc179e03922afe8fa9533d3f896338b9fb87ce0c8`, `av = $3,439,222.05`)
- `subAccount`: `0` (`0.0%`)

### 3. Subaccount Presence in Leaderboard & `>= $1M` Breakdown
- Across the `194` `user` addresses in the 200-candidate sample:
  - **Masters with ≥1 subAccount**: `98 / 194` (**`50.5%`**)
  - **Total subAccounts owned**: `595`
  - **SubAccounts appearing in `leaderboardRows` (`47,662` rows)**: **`0`** (`0.0%`)
  - **SubAccounts with `clearinghouseState.accountValue >= $1,000,000`**: **`17 / 595`** (`2.86%`)

Because `subAccounts` **never appear directly** in `stats-data.hyperliquid.xyz/Mainnet/leaderboard`, Stage 2 MUST enumerate `subAccounts(master)` and enqueue those with `accountValue >= stage1.minAccountValue` (per `§5.1`).

## Master vs SubAccount Balance Isolation
Inspecting `clearinghouseState` for masters vs their `subAccounts` confirms **strict per-address equity isolation**:

| Master Address | Leaderboard `accountValue` | Master `ch.accountValue` | # Subs | Sum of Sub `ch.accountValue` | Largest SubAccount (`name`, `ch.accountValue`, `n_pos`) |
|---|---:|---:|---:|---:|---|
| `0x023a3d058020fb76cca98f01b3c48c8938a22355` | $16,095,439.38 | $5,735,393.74 | 5 | $3,364,113.37 | `0x2eddf3...` (`hyperliquid_1s3`, $3,349,314.64, 96 pos) |
| `0x856c35038594767646266bc7fd68dc26480e910d` | $74,277,760.00 | $211,667.81 | 14 | $25,825,946.11 | `0x645b2e...` (`p2`, $22,660,612.21, 22 pos) |
| `0x49e96e255ba418d08e66c35b588e2f2f3766e1d0` | $30,573,637.62 | $0.00 | 5 | $2,544,175.72 | `0x095c6e...` (`HYPE`, $2,544,175.72, 1 pos) |
| `0xdcac85ecae7148886029c20e661d848a4de99ce2` | $15,259,880.31 | $0.00 | 50 | $3,298,957.14 | `0xac487c...` (`sub014`, $1,371,296.77, 6 pos) |
| `0xae6446c95cc4e455e9cf56f28757ab728d88e81e` | $13,021,288.25 | $10,001.45 | 21 | $6,317,101.97 | `0x034d4f...` (`zoyuhl001`, $456,831.34, 139 pos) |
| `0x7f1950ce02f9f60634ba39b81844911a6c2f05bf` | $3,776,635.43 | $0.00 | 30 | $3,498,977.48 | `0x752686...` (`bnhyper_Tony01`, $644,951.59, 126 pos) |

Key observations:
1. **No double-counting across `clearinghouseState` / `equity`**: A master's `clearinghouseState.marginSummary.accountValue` (and `spotClearinghouseState`) reflects **only** the master address's own margin/collateral (`$0.00` when funds are deployed into subAccounts, as seen on `0x49e96e25...`, `0xdcac85ec...`, and `0x7f1950ce...`). Summing `AV_E(t) = Σ_{k∈E} AV_k(t)` across passing `master` and `subAccount` addresses in `§7.1` never double-counts equity.
2. **Leaderboard `accountValue` vs on-chain `equity`**: Leaderboard `accountValue` can be substantially higher than `master_ch_av + sub_av_sum` (e.g. `0x856c3503...`: `$74.28M` leaderboard vs `$26.04M` perp master+subs) because leaderboard equity reflects broader wallet/spot/vault assets and/or stale snapshots. Per `§5.4`, Stage 2 `metrics.accountValue` uses real-time on-chain `equity`, not leaderboard `accountValue`.
3. **SubAccount behavioral heterogeneity**: Many masters isolate distinct strategies per subAccount — e.g., a directional `$2.54M` `HYPE` subAccount alongside empty `PREDICT`/`HIP-3` subAccounts (`0x49e96e25...`), or 21 multi-pair stat-arb/MM subAccounts each holding `135–171` simultaneous positions (`0xae6446c9...`).

## Decision on `subaccounts` Default (`docs/adr/0003-subaccount-handling-and-funding-sign.md`)
Keep `membership.subaccounts: "merge_after_filter"` as the default in `config/pipeline.yaml`:
- **Why not `independent`**: Counting 5 passing subAccounts of one whale as 5 separate entities under `equal` weighting (`#4 Consensus`) would over-weight multi-account whales.
- **Why not `merge_before_filter`**: Only `2.86%` (`17/595`) of subAccounts hold `>= $1M`; scanning fills/funding for all `595+` dormant/tiny subAccounts would waste ~3× daily API quota, and merging an alpha directional subAccount with a high-turnover market-making subAccount on the same master before filtering would contaminate both strategies' `fillsPerDay` / `makerRatio` / `profitFactorRobust`.
- **`merge_after_filter`**: Evaluates each address (`>= $1M`) independently in Stage 2, then groups passing entities in `domain/` by `master ?? address` (`§7.1`) — preserving clean strategy gating while preventing whale weight inflation.

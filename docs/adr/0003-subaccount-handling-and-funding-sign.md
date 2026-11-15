<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# ADR 0003: Subaccount Handling & `userFunding` Pagination / Sign Semantics

- **Date**: `2026-11-15`
- **Status**: `Accepted`
- **Scope**: Phase 0 Tasks `0.1`, `0.5`, `0.8` (`docs/implementation_plan.md` `§4`, `§5.1`, `§5.4`, `§5.6`, `§7.1`, `§11.2`)
- **Supporting Research**:
  - [docs/research/leaderboard.md](../research/leaderboard.md)
  - [docs/research/subaccounts.md](../research/subaccounts.md)
  - [docs/research/funding.md](../research/funding.md)

## Context
HyperPod's specification left two Stage 2 / scoring design questions to be settled after Phase 0 empirical probing:
1. **Subaccounts (`§5.1`, `§5.6`, `§7.1`)**: Whether to keep `membership.subaccounts: "merge_after_filter"` as the default in `config/pipeline.yaml` (versus `"independent"` or `"merge_before_filter"`), and how subAccount discovery and balance aggregation behave on live Hyperliquid data.
2. **`userFunding` & `fundingShare` (`§4`, `§5.1`, `§5.4`)**: Pagination bounds, deduplication keys, market-scope filtering, nullable schema fields, and sign reconciliation between `userFunding.delta.usdc` and `clearinghouseState.cumFunding`.

Neither decision modifies any **【規範】** formula or breaks `schemas/*.schema.json`; both clarify implementation boundaries and confirm defaults under `§11.2`.

---

## Decision 1: Subaccount Discovery & `merge_after_filter` Default

### Empirical Evidence (Phase 0.1 & 0.8)
- **Leaderboard absence**: Across `47,662` `leaderboardRows` and `595` subAccounts owned by the top `200` Stage 1 candidates (`194` `user` masters, `6` `vault`s), **`0 / 595` (`0.0%`) subAccounts appear in the leaderboard**.
- **Prevalence & size skew**: `98 / 194` (`50.5%`) of sampled `user` masters own `595` subAccounts, yet only **`17 / 595` (`2.86%`)** hold `clearinghouseState.accountValue >= $1,000,000`.
- **Strict balance isolation**: Inspecting `clearinghouseState` across masters and subAccounts proves zero balance overlap — e.g., `0x49e96e25...` (`master_ch_av = $0.00` vs `$2,544,175.72` in sub `HYPE`), `0xdcac85ec...` (`$0.00` master vs `$3,298,957.14` across 50 subs), and `0x7f1950ce...` (`$0.00` master vs `$3,498,977.48` across 30 subs).
- **Strategy heterogeneity**: Individual masters isolate incompatible strategies into distinct subAccounts (e.g., a directional `$2.54M` `HYPE` subAccount alongside empty `PREDICT`/`HIP-3` subAccounts, or 21–30 multi-pair MM/stat-arb subAccounts holding `126–171` concurrent positions).

### Decision
1. **Confirm `membership.subaccounts: "merge_after_filter"`** as the permanent default in `config/pipeline.yaml`.
   - *Rejected `"independent"`*: Treating 5 passing subAccounts of one whale as 5 independent entities would inflate multi-account whales in `equal`-weighted `#4 Consensus` and `#2`/`#6`/`#7`.
   - *Rejected `"merge_before_filter"`*: Scanning fills/funding for `595+` subAccounts (`97.14%` under `$1M` or dormant) would ~3× daily API quota, and merging an alpha directional subAccount with a high-turnover MM subAccount before `§5.5` gating would contaminate `fillsPerDay`, `makerRatio`, and `profitFactorRobust`.
2. **Stage 2 subAccount expansion (`§5.1`)**:
   - Whenever a candidate passes Stage 2 step 1 with `userRole == "user"`, fetch `subAccounts(master)` (20 weight, weekly refresh).
   - Enqueue every non-blocklisted `subAccountUser` whose on-chain equity (evaluating `clearinghouseState`, and `spotState` USDC when `portfolioMarginEnabled` / unified abstraction applies) meets `>= stage1.minAccountValue` (`$1,000,000`) into Stage 2 scoring, recording `"master": "<master_addr>"` and `"role": "subAccount"` in `state/daily/addresses/{shard}.json.gz` and `whitelist.json`.
   - When an address enters Stage 2 via `seed_addresses.txt` and `userRole` returns `{"role": "subAccount", "data": {"master": "0x..."}}`, preserve its `master` link directly so `§7.1` entity grouping (`entityKey = master ?? address`) remains consistent regardless of discovery path.
3. **Entity aggregation safety (`§7.1`)**: Because `clearinghouseState` / `equity` is strictly isolated per address, summing `AV_E(t) = Σ_{k∈E} AV_k(t)` and `szi_{E,c}(t) = Σ_{k∈E} szi_{k,c}(t)` over passing members of `entityKey = master ?? address` never double-counts collateral or positions.

---

## Decision 2: `userFunding` Pagination, Filtering & Sign Semantics

### Empirical Evidence (Phase 0.5)
- **Pagination & boundary overlap**: `userFunding(user, startTime)` returns up to **`500` items per page** in ascending `time` order with full history back to 2024 inception (no 10,000-item cap). Adjacent pages overlap at `last_t` (e.g., `0x7fdafde5` pages `0..4`), `hash` is uniformly `0x000...0`, and multi-coin daily aggregates share identical `00:00:00Z` timestamps that can split mid-timestamp at the `500`-item cap (`0xfc27136e` page `0` ends with `8` of `29` coins at `1780963200000`).
- **Nullable `nSamples`**: Historical daily-aggregated events carry integer `nSamples` (`1..24`), whereas recent hourly events (~last 5–7 days) carry `"nSamples": null`.
- **Non-main-perp coins**: `userFunding` without a `dex` parameter includes HIP-3 builder-dex funding events (`442 / 500` events on `0xfc27136e` page `0` are `xyz:*`, e.g., `xyz:CL`, `xyz:NVDA`).
- **Exact opposite sign convention vs `cumFunding`**:
  - `userFunding.delta.usdc` is **cashflow-signed** (`+` received by user, `-` paid by user).
  - `clearinghouseState.assetPositions[].position.cumFunding` (`allTime`, `sinceOpen`, `sinceChange`) is **cost-signed** (`+` paid by user, `-` received by user).
  - Verified exact negation `cumFunding.sinceChange == -Σ_{sinceChange} delta.usdc` to 6 decimal places on `0x1f250df5` `BTC` (`+2433.643747 == -(-811.666249 - 810.312499 - 811.664999)`) and `0xfc27136e` `BTC` (`-772.848931`), `ETH` (`-0.388018`), `SOL` (`-37.639785`), and `BNB` (`+10.071956`).

### Decision
1. **Pagination & deduplication (`client/info_api.py` & `state/daily`):**
   - Paginate `userFunding` forward from `lastFundingTime` (or `0` initially) using **inclusive** `startTime = last_t` (never `last_t + 1`).
   - Deduplicate using composite key **`(time, hash, delta.coin)`** and stop when `len(page) < 500` or `added == 0`.
   - **Cross-run boundary deduplication:** Persist `lastFundingCoins: list[str]` (the set of `delta.coin` values across all raw events already processed at `time == lastFundingTime`) in `state/daily/addresses/{shard}.json.gz`. Before incremental `userFunding(user, startTime=lastFundingTime)` pagination, seed the in-memory `seen` set with `{(lastFundingTime, "0x" + "0" * 64, coin) for coin in lastFundingCoins}` so events at `time == lastFundingTime` from prior daily runs are never double-counted into `cumFundingNet`/`cumFundingAbs`.
   - Account weight dynamically via `20 + ceil(len(page) / 20)`.
   - Keep `userFunding` at **Stage 2 step 5** (`§5.1`), calling it only for candidates passing steps 1–4 and current whitelist members.
2. **Pydantic modeling & market scope (`models/`, `scoring/metrics.py`):**
   - Model `delta.nSamples` as `int | None = None`.
   - Filter out non-main-perp events using the exact `ADR-0002` Decision 2 main-perp predicate (`coin in main_perp_universe` from `meta.universe` including `isDelisted: true`, **AND** `"#"`, `":"`, `"@"`, `"/"` not in `coin` and `coin != "PURR/USDC"`) before updating funding totals, matching `§1` / `§5.2` main-perp scope.
3. **`fundingShare` & state accounting (`§4`, `§5.4`):**
   - In `state/daily/addresses/{shard}.json.gz`, accumulate:
     - `cumFundingNet += float(ev["delta"]["usdc"])` (signed net cashflow on main perp dex)
     - `cumFundingAbs += abs(float(ev["delta"]["usdc"]))` (gross diagnostic magnitude on main perp dex)
     - Advance `lastFundingTime` to the maximum observed raw event timestamp and record `lastFundingCoins` at that `lastFundingTime` (across all raw events fetched so incremental cursors neither stall nor double-count split-timestamp boundaries on filtered-only pages).
   - Clarify `§5.4` **`fundingShare` 【規範】** without modification:
     $$\text{fundingShare} = \frac{|\text{cumFundingNet}|}{\max(|\text{pnlAllTime}|, 1.0)} = \frac{|\sum \text{delta.usdc}|}{\max(|\text{pnlAllTime}|, 1.0)}$$
     Netting signed receipts and payments chronologically before taking `abs()` avoids double-counting hedged/alternating carry (`cumFundingAbs`) while remaining invariant to API sign direction.

---

## Consequences & Verification Requirements
- **Phase 1–3 implementation**:
  - `pipeline/src/hyperpod/models/` must model `UserFundingDelta.nSamples: int | None` and `UserRoleResponse` (`{"role": ..., "data": {"master": ...}}` optional `data`).
  - `pipeline/src/hyperpod/client/info_api.py` must implement inclusive `startTime = last_t` pagination with `(time, hash, delta.coin)` dedup seeded from `lastFundingCoins` for `userFunding`, and `subAccounts` expansion for `role == "user"`.
  - `pipeline/src/hyperpod/scoring/metrics.py` must filter `userFunding` using the `ADR-0002` main-perp universe predicate (`meta.universe` + excluding `#`, `:`, `@`, `/`, `"PURR/USDC"`) and compute `fundingShare = abs(cumFundingNet) / max(abs(pnlAllTime), 1.0)`.
- **Required unit/contract tests**:
  - Pagination test simulating a mid-timestamp `500`-event split across multiple coins at the exact same `time` with `0x000...0` `hash`, asserting zero dropped or duplicated events.
  - Cross-run idempotence test simulating incremental `Day d` → `Day d+1` `userFunding(startTime=lastFundingTime)` resumption seeded by `lastFundingCoins`, asserting zero double-counting of `time == lastFundingTime` boundary events in `cumFundingNet`/`cumFundingAbs` and full equivalence with single-pass ingestion.
  - Sign & scope test asserting `xyz:*` HIP-3 and non-main-perp funding events are excluded from `cumFundingNet`/`fundingShare`, alternating positive/negative `delta.usdc` events net chronologically before `abs()`, and `nSamples: null` validates cleanly.
  - Subaccount test asserting `merge_after_filter` excludes `< $1M` subAccounts in Stage 2, passes `>= $1M` subAccounts with `master` populated, and avoids equity double-counting across master and subAccounts.

<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Phase 0 Gate Report — Empirical API Probing & Architecture Validation

- **Phase:** `Phase 0 — 研究實測（不寫產品程式碼）` (`docs/implementation_plan.md` `§9`)
- **Date:** `2026-11-15`
- **Status:** `WAITING_USER_REVIEW` (`Proposal 0001` Option B Approved & Applied)
- **Production Code Written:** None (`0` files under `pipeline/` or `app/`, per `§9` boundary)
- **`【規範】` Modification Gate Triggered (`§11.2`):** Yes — [`docs/proposals/0001-intra-ms-fill-ordering.md`](../proposals/0001-intra-ms-fill-ordering.md) *(Approved 2026-11-15)*

---

## 1. Executive Summary

All twelve Phase 0 empirical tasks (`0.1`–`0.12`) have been completed against live Hyperliquid API endpoints, GitHub Actions Ubuntu runners, and GitHub Pages Fastly CDN, establishing verified schemas, edge cases, storage budgets (`§3.8`), and `89` recorded JSON API fixtures (`fixtures/api/`, committed at `e38db12`).

Three Architectural Decision Records (`ADR-0001`–`0003`) capture non-breaking implementation boundaries and confirm defaults in `config/pipeline.yaml`. One `§11.2` **【規範】** amendment proposal (**Proposal `0001`**) is submitted for user approval before Phases 2–3: empirical verification across `5` active addresses (`51` coins) proved that Hyperliquid's `tid` is a non-monotonic 50-bit hash within the same millisecond, causing `(time, tid)` sorting to fail `clearinghouseState.szi` reconstruction on **`3 / 5` addresses** (`+400.0 AVAX`, `-99.85 SOL`, `-500.0 ETH`), whereas preserving API execution sequence `(time, api_seq_index, leg_idx)` matches **`51 / 51` coins (`0.0` error)**.

---

## 2. Task Completion Matrix (`0.1`–`0.12`)

| Task | Deliverable(s) | Key Empirical Findings & Verification |
|---|---|---|
| **0.1** Leaderboard | [`docs/research/leaderboard.md`](../research/leaderboard.md) | S3/CloudFront hourly refresh (`Last-Modified` `+60m 06s`: `47,662` → `47,649` rows, `~38.94 MB`). Stage 1 funnel: `47,662 → 2,378 >= $1M → 1,661` pass (`3.48%`). `0 / 595` subAccounts appear in leaderboard; `6 / 200` (`3.0%`) Stage 1 candidates are vaults. |
| **0.2** Fixtures | [`fixtures/api/`](../../fixtures/api/index.json) *(commit `e38db12`)* | Recorded `89` valid, secret-scanned JSON files (`11` diverse addresses × `8` endpoints + `index.json`), covering `disabled`/`default`/`unifiedAccount`/`portfolioMargin` modes, masters, subAccounts, vaults, and intra-ms fill edge cases. |
| **0.3** Equity & Collateral | [`docs/research/equity.md`](../research/equity.md)<br>[`docs/adr/0001-equity-calculation.md`](../adr/0001-equity-calculation.md) | `154 / 200` (`77.0%`) Stage 1 candidates use `unifiedAccount` (`111`) or `portfolioMargin` (`43`). Defined `equity = max(ch_av, spot_usdc_total, avail_after_maint_usdc + c_mm)` (`equitySource: "perp+spot"`) for shared collateral vs `ch_av` (`"perp"`) for isolated perp, avoiding double-counting and handling negative borrowed USDC. |
| **0.4** Fill `dir`, Dedup & Filters | [`docs/research/fill-dir.md`](../research/fill-dir.md)<br>[`docs/adr/0002-fill-dedup-and-ordering.md`](../adr/0002-fill-dedup-and-ordering.md) | Scanned `94,131` fills (`50` Stage 1 candidates). Discovered `103` `#...` outcome-market fills (`Buy`/`Sell`/`Merge Outcome`/`Settlement`), requiring strict `coin in meta.universe` (`isDelisted: true` retained) + excluding `#`, `:`, `@`, `/`, `"PURR/USDC"`. Zero `"Liquidated ..."` `dir` strings; nested `liquidation.liquidatedUser == user` isolates self-liquidations from maker counterparty fills (`crossed: false`). `(time, tid, hash, coin, dir)` had `0` collisions (`20/1980`, `55/1945`, `151/1849` boundary splits). `aggregateByTime` ratio (`n=20`): `min=1.0303, median=1.2511, mean=2.7815, max=16.0932`. |
| **0.5** Funding & Sign | [`docs/research/funding.md`](../research/funding.md)<br>[`docs/adr/0003-subaccount-handling-and-funding-sign.md`](../adr/0003-subaccount-handling-and-funding-sign.md) | `userFunding` returns `500`/page ascending with no 10k cap, nullable `nSamples` (`int \| null`), zero `hash`, and HIP-3 `xyz:*` events (`442/500` on `0xfc27136e`) requiring main-perp filtering. Proved exact opposite sign to `clearinghouseState.cumFunding` (`sinceChange == -Σ delta.usdc` to 6 decimals) and specified `lastFundingCoins` + `fundingShare = \|cumFundingNet\| / max(\|pnlAllTime\|, 1.0)`. |
| **0.6** Runner Rate Limit | [`docs/research/runner-ratelimit.md`](../research/runner-ratelimit.md) | Live Ubuntu GitHub Actions run `43681945367`: `100×` sequential `20.161s` (`4.96 req/s`), `4`-worker concurrent `5.115s` (`19.55 req/s`), `0` HTTP `429`s via CloudFront `IAD61-P5`. Temporary probe workflow deleted cleanly at commit `550051d`. |
| **0.7** Portfolio Curve | [`docs/research/portfolio.md`](../research/portfolio.md) | `80 / 80` windows across `10` addresses aligned identically. Cross-checked `userNonFundingLedgerUpdates` (`$8M` & `$7M` USDC withdrawals on `0xfc27136e`), proving `perpAllTime.pnlHistory` is invariant to external cashflows (`§5.4 maxDrawdown` immunity). |
| **0.8** Subaccounts | [`docs/research/subaccounts.md`](../research/subaccounts.md)<br>[`docs/adr/0003-subaccount-handling-and-funding-sign.md`](../adr/0003-subaccount-handling-and-funding-sign.md) | `98 / 194` (`50.5%`) `user` masters own `595` subAccounts (`17 / 595 = 2.86% >= $1M`; `0 / 595` on leaderboard). Strictly isolated balances & heterogeneous strategies confirm `membership.subaccounts: "merge_after_filter"` default. |
| **0.9** Candles & CORS | [`docs/research/candles-cors.md`](../research/candles-cors.md) | Verified CORS (`Access-Control-Allow-Origin: *`), `~5,000`-candle depth limits (`~17d` `5m`, `~52d` `15m`, `~208d` `1h`, `~2.28yr` `4h`), and coin taxonomy (`235` main perp incl. `56` delisted, `516` spot tokens/`352` pairs, `10` HIP-3 builder dexes). |
| **0.10** Pages Gzip | [`docs/research/pages-gzip.md`](../research/pages-gzip.md) | Live GitHub Pages Fastly headers serve `.json.gz` with `content-type: application/gzip` and **no `content-encoding: gzip`** (`cache-control: max-age=600`), confirming browser JS/Dart `DecompressionStream('gzip')` / `archive` decompression is mandatory. |
| **0.11** Position Replay | [`docs/research/reconstruct-check.md`](../research/reconstruct-check.md)<br>[`docs/proposals/0001-intra-ms-fill-ordering.md`](../proposals/0001-intra-ms-fill-ordering.md) | Across `5` active addresses (`51` filled coins + `28` zero-fill open positions), `(time, api_seq_index)` achieves **`0.0` error on `51 / 51` coins**, while `(time, tid)` fails `3 / 5` addresses due to non-monotonic 50-bit hash `tid`. |
| **0.12** Storage Sizing | [`docs/research/storage-sizing.md`](../research/storage-sizing.md)<br>[`docs/implementation_plan.md`](../implementation_plan.md#L382-L394) | Updated `§3.8` empirically: `Year 1 ~110 MB` (incl. `~18.9 MB` bootstrap fills & `~5.9 MB` `backfill_15m_1h`), ongoing `~85 MB/yr` (`~9`-year runway to `800 MB`), first-screen CDN wire transfer `~0.5 MB` (`< 1.5 MB`), per-coin fill sharding keeps monthly parse at `18.11 ms`. |

---

## 3. Architecture Decisions & Specification Amendments

### Accepted ADRs (Autonomous under `§11.2`)
1. **[`ADR-0001` — Equity Calculation Across Margin & Account Abstraction Modes](../adr/0001-equity-calculation.md):** Codifies `equity` formula and `"perp"` vs `"perp+spot"` labeling without modifying `schemas/` or `§5`/`§7` formulas.
2. **[`ADR-0002` — Fill Deduplication, Main-Perp Universe Filter, Liquidation Attribution & HFT Threshold](../adr/0002-fill-dedup-and-ordering.md):** Codifies overlap pagination, `(time, tid, hash, coin, dir)` dedup + `recentScoringFillKeys` cross-run boundary dedup in `state/daily`, strict `meta.universe` main-perp filter (excluding `#` outcome markets), `liquidatedUser == user` self-liquidation attribution, and retains `hft.fillsPerDay: 500`.
3. **[`ADR-0003` — Subaccount Handling & `userFunding` Pagination / Sign Semantics](../adr/0003-subaccount-handling-and-funding-sign.md):** Confirms `merge_after_filter` subaccount expansion/merging, `userFunding` inclusive pagination seeded via `lastFundingCoins`, `ADR-0002` main-perp coin filtering, opposite sign proof vs `cumFunding`, and `fundingShare` chronological netting.

### Pending User Proposal (`§11.2` Stop-and-Propose Gate)
- **[`Proposal 0001` — Replace `(time, tid)` Intra-Millisecond Fill Ordering with API Execution Sequence Order](../proposals/0001-intra-ms-fill-ordering.md)**
  - **Target `【規範】` clauses:** [`docs/implementation_plan.md`](../implementation_plan.md#L476-L492) `§5.2` (`L479`) and `§5.3` (`L487`).
  - **Summary:** Replace `(time, tid)` with chronological first-occurrence deduplication and `(time, api_seq_index, leg_idx)` ordering (where reversal flip splits assign `leg_idx = 0` to closing legs `CL`/`CS` and `leg_idx = 1` to opening legs `OS`/`OL`). Preserves `schemaVersion: 1` and all `§5.4`–`§5.6` / `§7` formulas untouched.

---

## 4. Independent Adversarial Audit (`d32b7d7e`) & Remediations Applied

Before closeout, an independent read-only adversarial audit cross-checked all 11 reports, 89 fixtures, 3 ADRs, Proposal `0001`, `§3.8`, and raw probe JSONs:
- **Verified Clean:** All `11` research reports' empirical counts, mathematical identities, line-range citations (`#L...`), `§3.8` numbers, zero-secret checks, and `§5`/`§7` `【規範】` preservation passed 100%.
- **Remediated Findings:**
  1. **`[MAJOR]` Cross-run stateful dedup seam:** Added `recentScoringFillKeys` (`ADR-0002`) and `lastFundingCoins` (`ADR-0003`) to `state/daily/addresses/{shard}.json.gz` so daily / checkpointed incremental runs match full batch runs without storing raw fills (`AGENTS.md` Hard Invariant #4).
  2. **`[MINOR]` Flip-split leg sub-index & predicate alignment:** Added `(time, api_seq_index, leg_idx)` in `ADR-0002` and `Proposal 0001`; aligned `ADR-0003` and `funding.md` with the strict `ADR-0002` main-perp universe predicate; corrected `reconstruct-check.md` Task 0.11 pagination wording (`cursor = last_t`); added `AGPL-3.0-or-later` SPDX header to `docs/implementation_plan.md` and shifted Proposal `0001` line references (`L479`/`L487`).
  3. **`.gitignore` updated (`docs/` tracked):** Removed `docs/` from `.gitignore` so all `docs/` research reports, ADRs, proposals, phase reports, and `status.md` are tracked cleanly in Git alongside `fixtures/api/`.

---

## 5. Git Commits & Verification Evidence

- **`2d8db9f`** — Initial `.gitignore` and `README.md`
- **`550051d`** — Removed temporary `.github/workflows/temp-ratelimit-probe.yml` after live runner rate-limit measurement
- **`e38db12`** — `docs(research): record phase 0 API fixtures` (`89` JSON files, `119,531` insertions, pre-commit secret scan `CLEAN`)
- **`.gitignore` + `docs/` tracking commit** — Removed `docs/` from `.gitignore` and tracked all `18` markdown files under `docs/` (`implementation_plan.md` w/ `SPDX` + `§3.8` + approved Proposal `0001` `§5.2`/`§5.3`, `11` research reports, `3` ADRs, `Proposal 0001`, `status.md`, and `phase-0.md`).

---

## 6. Required User Actions (`§11.2` / `§11.3` / `§13`)

1. **Authorize transition to Phase 1 (`1.1`–`1.6`):** Review updated `docs/implementation_plan.md` (`§3.8`, `§5.2`, `§5.3`) and confirm starting Phase 1. *(Proposal `0001` Option B and removing `docs/` from `.gitignore` have been approved and applied.)*
2. **Push local `main` commits to remote:** Push the `fixtures/api/` (`e38db12`) and `docs/` commits on `main` to `origin` when ready.

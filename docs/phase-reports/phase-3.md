<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Phase 3 Gate Report — Candidate Discovery & Daily Scoring Engine

- **Phase:** `Phase 3 — 候選發現與評分（daily）` ([`docs/implementation_plan.md`](../implementation_plan.md) `§9`)
- **Date:** `2026-11-18`
- **Status:** `WAITING_USER_REVIEW`
- **`【規範】` Modification Gate Triggered (`§11.2`):** No *(all `§5` formulas kept verbatim; empirical `§5.4 maxDrawdown` behavior on scale-up accounts documented in [`docs/research/first-whitelist.md`](../research/first-whitelist.md) for user review)*

---

## 1. Executive Summary

All five Phase 3 tasks (`3.1`–`3.5`) have been implemented inside the pinned NixOS development shell ([`shell.nix`](../../shell.nix)), verified across unit, property-based (Hypothesis), multi-day simulation, and schema-contract test suites (`89 passed, 1 deselected`), and validated against the live Hyperliquid S3 leaderboard and Info API ([`docs/research/first-whitelist.md`](../research/first-whitelist.md)).

Following initial implementation, an independent read-only adversarial audit (`05b30ac4-d846-43d3-8e31-781334dd6095`) identified `4 MAJOR`, `3 MINOR`, and `1 NIT` findings (`FINDING-01`–`FINDING-08`) across subaccount 7-day cache persistence, deep-copy `StateStore` shard isolation + transient skip protection, blocklist enforcement in offline/backfill/whitelist paths, offline scoring time-window recomputation, `saw_truncated_position` + `main_perp_universe` fill checks, unstarted dynamic `CoinTrack` interval removal, strict hex shard validation, and `failStreak` sync / missing-role re-query. All 8 findings were reproduced first with failing regression tests (`5 failed, 13 passed`), fixed at the root cause, re-verified (`88 passed`), and cleared on secondary adversarial audit (`0 CRITICAL`, `0 MAJOR`, `0 MINOR`, `0 NIT` — **`PASS`**).

Subsequent live verification across the top-35 volume candidates and `>120` sampled swing/steady-ROI Stage 1 candidates uncovered two optional fields in live `spotClearinghouseState` / `subAccounts[].spotState` payloads (`spotHold`, and optional `token` on `+NNNNN` outcome/HIP-2 balances). This discrepancy was reproduced via TDD (`test_spot_clearinghouse_state_allows_missing_token_and_spot_hold`), fixed in `SpotBalance`, verified against previously failing live payloads, and offline-scored across `49` persisted live candidates (`1` passing active whitelist address `0xe9ec1d90652e13521623fb048f1c77883c597859`) with `check-jsonschema` passing on all output contracts (`89 passed, 1 deselected` total).

---

## 2. Task Completion Matrix (`3.1`–`3.5`)

| Task | Deliverable(s) | Key Verification & Coverage |
|---|---|---|
| **3.1** Stage 1 Discovery & UTC Time | [`pipeline/src/hyperpod/util/time.py`](../../pipeline/src/hyperpod/util/time.py), [`pipeline/src/hyperpod/util/__init__.py`](../../pipeline/src/hyperpod/util/__init__.py), [`pipeline/src/hyperpod/discovery/prefilter.py`](../../pipeline/src/hyperpod/discovery/prefilter.py), [`pipeline/src/hyperpod/discovery/__init__.py`](../../pipeline/src/hyperpod/discovery/__init__.py), [`pipeline/tests/test_prefilter.py`](../../pipeline/tests/test_prefilter.py), [`.gitignore`](../../.gitignore) | UTC date helpers (`utc_date_str`, `recent_utc_dates`, `prune_fill_stats`), Stage 1 leaderboard prefilter (`minAccountValue`, `minPnlAllTime`, `minPnlMonthRatio`, `maxTurnoverMonth`), `seed_addresses.txt` / `blocklist.txt` loading with case normalization, and `expand_subaccounts` (`>= $1M` across `disabled` / `unifiedAccount` / `portfolioMargin` subaccounts). `4` unit tests in `test_prefilter.py`. |
| **3.2** Incremental Trade Reconstruction | [`pipeline/src/hyperpod/scoring/trades.py`](../../pipeline/src/hyperpod/scoring/trades.py), [`pipeline/tests/test_trades.py`](../../pipeline/tests/test_trades.py) | `OpenTradeState` incremental round-trip reconstruction ordered by `(time, api_seq_index, leg_idx)`, `szDecimals` quantization (`|szi| < 10^-(szDecimals + 2)` -> `0.0`), 10k-truncation pre-zero fill ignoring + `openTime=0` sentinel + `saw_truncated_position` detection, `recentScoringFillKeys` overlap boundary dedup, and bijective `nFills`/`nMaker` counting on `leg_idx == 0`. `5` unit + Hypothesis property tests verifying incremental chunking == batch equivalence across arbitrary splits. |
| **3.3** Stage 2 Scoring Metrics | [`pipeline/src/hyperpod/scoring/metrics.py`](../../pipeline/src/hyperpod/scoring/metrics.py), [`pipeline/tests/test_metrics.py`](../../pipeline/tests/test_metrics.py) | All `14` `§5.4` metrics (`nTrades`, `winRate`, `profitFactor`, `profitFactorRobust`, `topWinShare`, `maxDrawdown`, `historyDays`, `fillsPerDay`, `medianHoldMinutes`, `makerRatio`, `turnoverMonth`, `fundingShare`, `hedgeRatio`, `equity`) + `ADR-0001` `equitySource` + missing-leaderboard `portfolio` fallbacks (`derive_portfolio_fallbacks`, 30d `notionalDays` turnover). `10` tests covering `>= 3` hand-calculated cases per metric plus boundary zeros/nulls. |
| **3.4** Hard Gates, Whitelist & Coin Scheduling | [`pipeline/src/hyperpod/scoring/filters.py`](../../pipeline/src/hyperpod/scoring/filters.py), [`pipeline/src/hyperpod/scoring/whitelist.py`](../../pipeline/src/hyperpod/scoring/whitelist.py), [`pipeline/tests/test_filters_whitelist.py`](../../pipeline/tests/test_filters_whitelist.py) | All `10` `§5.5` hard gates (`role`, `hft`, `mm`, `funding`, `hedge`, `passive`, `sample`, `mdd`, `pf`, `lucky`), conditional whitelist entry (`§5.6`), `exitAfterFailedDays` (`3`-day grace period & streak reset), immutable `id` assignment, percentile `score` ranking (`pct` with `null` `pfRobust -> 1.0`), `criteriaVersion` + `sha256` criteria hash/history tracking, and `§3.1.1` tracked coin scheduling (`pinned + dynamicTopN` with `30`-day stickiness, `maxTracked = 32` eviction without zero-duration future intervals, and `coinBackfillQueue`). `7` multi-day simulation & gate-boundary tests. |
| **3.5** Sharded State Store, Daily Runner & CLI | [`pipeline/src/hyperpod/storage/state_store.py`](../../pipeline/src/hyperpod/storage/state_store.py), [`pipeline/src/hyperpod/storage/__init__.py`](../../pipeline/src/hyperpod/storage/__init__.py), [`pipeline/src/hyperpod/scoring/daily_runner.py`](../../pipeline/src/hyperpod/scoring/daily_runner.py), [`pipeline/src/hyperpod/scoring/__init__.py`](../../pipeline/src/hyperpod/scoring/__init__.py), [`pipeline/src/hyperpod/cli.py`](../../pipeline/src/hyperpod/cli.py), [`pipeline/src/hyperpod/client/info_api.py`](../../pipeline/src/hyperpod/client/info_api.py), [`pipeline/src/hyperpod/models/api.py`](../../pipeline/src/hyperpod/models/api.py), [`pipeline/tests/test_state_store_and_cli.py`](../../pipeline/tests/test_state_store_and_cli.py), [`pipeline/tests/test_info_api.py`](../../pipeline/tests/test_info_api.py), [`pipeline/tests/test_contracts.py`](../../pipeline/tests/test_contracts.py), [`docs/research/first-whitelist.md`](../research/first-whitelist.md) | Partition-isolated `StateStore` (`state/daily/` vs `state/fast/`), 256 gzipped hex shards (`00`–`ff`) with strict address/shard validation, 90-day `fillStats` & non-whitelist pruning, 500-line `runs.jsonl` cap, `daily_runner` online + `--offline` + resumable `backfill` (`--budget-minutes`, 20-address checkpoints), `discover`/`score`/`backfill` Typer CLI commands, and schema-validated output generation. `6` integration/CLI tests (`test_state_store_and_cli.py`) + updated live `SpotBalance` test (`test_info_api.py`). |

---

## 3. Local CI & Live Verification Evidence (`nix-shell`)

All automated checks ran cleanly inside `nix-shell` after the final code edit (`SpotBalance` wire fix + `docs/research/first-whitelist.md`):

| Check Command | Observed Result |
|---|---|
| `cd pipeline && uv run ruff format --check src tests` | **PASS** — `42 files already formatted` |
| `cd pipeline && uv run ruff check src tests` | **PASS** — `All checks passed!` |
| `cd pipeline && uv run mypy --strict src tests` | **PASS** — `Success: no issues found in 42 source files` |
| `cd pipeline && uv run pytest` | **PASS** — `89 passed, 1 deselected in 5.13s` (`live` test deselected by default `addopts`) |
| `bash scripts/validate_schemas.sh` | **PASS** — `ALL SCHEMA & SITE-DATA VALIDATIONS PASSED!` |
| `cd app && ../flutter.sh analyze && ../flutter.sh test` | **PASS** — `No issues found! (ran in 1.4s)`, `All tests passed!` (`2` tests) |
| Live `hyperpod discover` + bounded `hyperpod score` + `--offline` replay (`/tmp/hyperpod-phase3-live`, cleaned up) | **PASS** — `48,181` leaderboard rows -> `1,720` Stage 1 candidates; `49` live candidates scored (`1` active whitelist PASS `0xe9ec1d90652e13521623fb048f1c77883c597859`), `check-jsonschema` `ok -- validation done` on `whitelist.json`, `criteria.json`, `candidates.json`. |

---

## 4. Independent Adversarial Audit & Test-First Remediation

1. **Primary + Secondary Audit (`05b30ac4-d846-43d3-8e31-781334dd6095`):**
   - Identified `FINDING-01`..`FINDING-08` (`4 MAJOR`, `3 MINOR`, `1 NIT`):
     - `FINDING-01` (`MAJOR`): Persist `eligibleSubAccounts` in `AddressDailyState` so 7-day subaccount cache hits still emit `>= $1M` subaccounts.
     - `FINDING-02` (`MAJOR`): Return/store deep copies in `StateStore.get_address_state`/`put_address_state` and preserve skipped/transiently-errored active whitelist members' `failStreak`, `membership`, open notionals, and `absentStage1Streak`.
     - `FINDING-03` (`MAJOR`): Enforce `blocklist.txt` across `run_backfill`, `run_score`, and `update_whitelist_membership` while keeping contiguous candidate `idx`.
     - `FINDING-04` (`MAJOR`): Scope `run_score(..., offline=True)` to `discovered`/`cursor` + `eligibleSubAccounts` + active whitelist and recompute time-window metrics at `eval_ts`.
     - `FINDING-05` (`MINOR`): Pass `main_perp_universe` to `fetch_user_fills_by_time` and track `saw_truncated_position` for prior mid-stream truncated coins.
     - `FINDING-06` (`MINOR`): Remove unstarted future `CoinTrack` intervals (`trackedFrom >= effective_ts`) on capacity eviction rather than writing `trackedTo == trackedFrom`.
     - `FINDING-07` (`MINOR`): Enforce `^0x[0-9a-f]{40}$` and `^[0-9a-f]{2}$` in `address_shard` and `StateStore.shard_path`.
     - `FINDING-08` (`NIT`): Synchronize `AddressDailyState.failStreak`, fail loudly on missing schema paths, and re-query `"missing"` `userRole`.
   - All 8 findings were reproduced first (`5 failed, 13 passed`), remediated, and cleared on secondary audit (**`PASS`**, `0 CRITICAL / 0 MAJOR / 0 MINOR / 0 NIT`).
2. **Live Wire Contract Remediation (`SpotBalance`):**
   - Live API testing revealed `"spotHold"` strings and omitted `"token"` on `+NNNNN` outcome/HIP-2 spot balances; reproduced via `test_spot_clearinghouse_state_allows_missing_token_and_spot_hold` (`1 failed, 11 deselected`) before updating `SpotBalance` (`12 passed` in `test_info_api.py`, `89 passed` full suite).

---

## 5. Git Commits on `main` (Phase 3)

- `578b413` — `feat(discovery): implement Stage 1 prefilter, subaccount expansion, and UTC time helpers (Task 3.1)`
- `1c5940c` — `feat(scoring): implement incremental round-trip trade reconstruction and property tests (Task 3.2)`
- `227ead0` — `feat(scoring): implement all 14 Stage 2 metrics and portfolio fallbacks (Task 3.3)`
- `b5d0984` — `feat(scoring): implement hard gates, whitelist state machine, criteria hash, and coin tracking (Task 3.4)`
- `0bbf8cd` — `feat(pipeline): implement sharded StateStore, daily_runner, CLI discover/score/backfill, and live whitelist probe (Task 3.5)`

---

## 6. Required User Actions (`§11.3` / `§13`)

1. **Manual Whitelist Spot-Check (`§13`) & `§5.4 maxDrawdown` Observation:** Review [`docs/research/first-whitelist.md`](../research/first-whitelist.md), including the **5 Spotlight Addresses** with direct Hyperliquid Explorer links (`0xe9ec1d90...`, `0xb45f5c53...`, `0xfb941d23...`, `0x0fd6f65d...`, `0xf5a523b1...`), and confirm whether to keep the `【規範】` `§5.4 maxDrawdown` formula verbatim or open a `§11.2` proposal for early-deposit scale-up effects.
2. **Authorize Phase 4 (`4.1`–`4.6`):** Confirm starting **Phase 4 — 快照、公開 Fills、K 線與歷史重建** (`snapshot`, `candles`, `publish`, `reconstruct.py`, `verify-live`, and `git_publish.py`).
3. **Remote Push (`main`):** Push local `main` commits to `origin` when ready (all Phase 0–3 commits remain local).

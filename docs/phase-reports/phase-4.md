<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Phase 4 Gate Report — Snapshot Collector, Historical Reconstruction, Publishing & Workflows

- **Phase:** `Phase 4 — 快照、歷史重建、發佈與部署` ([`docs/implementation_plan.md`](../implementation_plan.md) `§9 Phase 4`)
- **Date:** `2026-11-21`
- **Status:** `WAITING_USER_REVIEW` *(Phase 4a complete; Phase 4b 24h live schedule soak does not block Phase 5 per `§9` & `§11.3`)*
- **`【規範】` Modification Gate Triggered (`§11.2`):** No *(all `§5`, `§6`, `§7` formulas and JSON schemas preserved verbatim without `schemaVersion` bump)*

---

## 1. Executive Summary

All Phase 4 engineering tasks (`4.1`–`4.5`) and documentation (`4.6` / [`README.md`](../../README.md)) have been implemented in the NixOS development shell ([`shell.nix`](../../shell.nix)), verified across `112` unit/property/integration/contract tests (`ruff`, `mypy --strict`, `pytest`, `check-jsonschema`, `flutter analyze/test`), hardened through two rounds of independent read-only adversarial code audits (`e82cf19b-acb3-4c95-abe3-8aa88e02eaad`), and validated end-to-end against live Hyperliquid S3 Leaderboard and Info API endpoints in an isolated temporary workspace (`/tmp/hyperpod-phase4-live`, cleaned up after capture).

Live E2E smoke testing confirmed full compatibility across live `discover`, `bootstrap` (`backfill` -> `score` -> `candles` -> `reconstruct` -> `snapshot` -> single-commit `state` & `gh-pages` push), incremental `snapshot`, network-free writer-locked `publish --no-fetch-fills`, `verify-live --sample 3` (`72 / 72` coin checks passed, `0` mismatches), `health --hours 24` (`healthy: true`), and `validate-contracts`. All Phase 4 commits are clean on local `main`; only human-gated remote pushes, GitHub repository settings (`§13`), and the optional full-cohort 28–35h initial `bootstrap` run remain for user action.

---

## 2. Task Completion Matrix (`4.1`–`4.6`)

| Task | Deliverable(s) | Key Implementation & Verification Highlights |
|---|---|---|
| **4.1** Snapshot Collector & Permanent Candles | [`candles.py`](../../pipeline/src/hyperpod/snapshot/candles.py), [`collect.py`](../../pipeline/src/hyperpod/snapshot/collect.py), [`test_snapshot_and_candles.py`](../../pipeline/tests/test_snapshot_and_candles.py) | `5m` monthly gzip archives (stopping at yesterday's UTC close during daily runs unless `--include-today-5m`), `15m`/`1h` backfill (`52d`/`208d`), `4h` full overview, next-UTC-day `00:00` dynamic coin activation (`resolve_effective_tracked_coins`), 10-minute fast snapshot collector with conditional `fillsCursorPublish`, `unifiedAccount` / `portfolioMargin` `spotClearinghouseState` fetch skip for `disabled`/`default`, `3`-snapshot stale warnings, `>20%` atomic abort (`SnapshotAbortError`), launch-day bridging from full `posChanges`, and same-day exit + flat re-entry zeroing. |
| **4.2** Pre-Launch Historical Reconstruction | [`reconstruct.py`](../../pipeline/src/hyperpod/snapshot/reconstruct.py), [`snapshot/__init__.py`](../../pipeline/src/hyperpod/snapshot/__init__.py), [`test_reconstruct.py`](../../pipeline/tests/test_reconstruct.py) | `reconstructDays` (`365d` default) traversal across `4h` (`>208d`) and `1h` (`<=208d`) candle close lookups, `reconstruct_address_coin_timeline` (`startPosition` chain, `szDecimals` quantization, post-`t_launch` in-flight delta subtraction, zero-fill constant position backfill), `build_av_curve` + linear AV interpolation respecting `coverageStart` (`Unknown != Zero`), `criteriaVersion=0` retrospective intervals `[effStart, launchedAt)`, post-fetch `t_launch` + `<= t_launch` cursor capping, standalone `snapshots/latest.json` emission, and `coinBackfillQueue` clearing on `write_fills=True`. |
| **4.3** Formats, Site Writer & Single-Commit Git Publish | [`formats.py`](../../pipeline/src/hyperpod/storage/formats.py), [`state_store.py`](../../pipeline/src/hyperpod/storage/state_store.py), [`site_writer.py`](../../pipeline/src/hyperpod/storage/site_writer.py), [`git_publish.py`](../../pipeline/src/hyperpod/storage/git_publish.py), [`storage/__init__.py`](../../pipeline/src/hyperpod/storage/__init__.py), [`test_site_writer_and_git_publish.py`](../../pipeline/tests/test_site_writer_and_git_publish.py) | `4/6`-sig-fig + `szDecimals` quantization, deterministic `mtime=0` atomic gzip JSON I/O, `StateStore` partition guards (`fast/` vs `daily/` vs `bootstrap/`), `upsert_coin_fills` with day/month mutual exclusion, `>7d` daily-to-monthly compaction (`merge_snapshots_to_month`, `verify_snapshot_compaction_equivalence` before deleting daily JSONs), `rebuild_manifest` with multi-pass byte convergence & `800 MB` soft-limit warning (never auto-deletes), `_SECRET_PATTERN` (`{12,}` token length + `.gz` decompression scan), `_merge_fill_file`-safe overlays (`fast` & `daily`), and single-commit orphan branch push (`3` `--force-with-lease` retries). |
| **4.4** Health, Live Verification & Phase 4 CLI | [`health.py`](../../pipeline/src/hyperpod/ops/health.py), [`ops/__init__.py`](../../pipeline/src/hyperpod/ops/__init__.py), [`cli.py`](../../pipeline/src/hyperpod/cli.py), [`test_health_and_cli_phase4.py`](../../pipeline/tests/test_health_and_cli_phase4.py), [`test_contracts.py`](../../pipeline/tests/test_contracts.py) | `evaluate_health` (fast success rate `>= 95%`, avg weight `< 1,500`, `>30m` live gap detection including `[launchedAt, first_live_t]`, hourly compaction tolerance in `months/*.json.gz`), `verify_live_positions` (excluding `latest.stale` IDs and reconciling intra-snapshot suffix fills against `clearinghouseState`), and CLI subcommands `snapshot`, `candles`, `reconstruct`, `publish`, `bootstrap`, `health`, `verify-live`, and `validate-contracts`. |
| **4.5** GitHub Actions Workflows (`§6.2`) | [`pipeline-fast.yml`](../../.github/workflows/pipeline-fast.yml), [`pipeline-daily.yml`](../../.github/workflows/pipeline-daily.yml), [`pipeline-backfill.yml`](../../.github/workflows/pipeline-backfill.yml), [`deploy-frontend.yml`](../../.github/workflows/deploy-frontend.yml) | Strict `concurrency: hyperpod-writer` isolation on short publish jobs only (`pipeline-fast`, `daily publish` with `--no-fetch-fills`, `deploy-frontend publish`), lock-free long jobs (`daily score` + fill fetch with artifact staging, `deploy-frontend build`, `pipeline-backfill`), `git ls-remote` orphan branch initialization guards, `verify-live --sample 3` smoke step, and 45-day schedule `enable` keepalive. |
| **4.6** Documentation & Bootstrap Readiness | [`README.md`](../../README.md), bounded live smoke test (`/tmp/hyperpod-phase4-live`) | Updated [`README.md`](../../README.md) with NixOS `bootstrap` resumable command, Phase 4 CLI reference, `800 MB` retention guardrail, and manual GitHub Actions / Pages setup steps; full local + live E2E smoke test executed cleanly. |

---

## 3. Local CI & Live E2E Verification Evidence (`nix-shell`)

### 3.1 Automated Offline CI Suite (`nix-shell`)

All 7 checks passed with exit code `0` after the final code edit:

| Check Command | Observed Result |
|---|---|
| `cd pipeline && uv run ruff check .` | **PASS** — `All checks passed!` |
| `cd pipeline && uv run ruff format --check .` | **PASS** — `53 files already formatted` |
| `cd pipeline && uv run mypy --strict src tests` | **PASS** — `Success: no issues found in 53 source files` |
| `cd pipeline && uv run pytest` | **PASS** — `112 passed, 1 deselected in 34.26s` (`live` test deselected by default `addopts`) |
| `cd pipeline && uv run hyperpod validate-contracts` | **PASS** — `Validated config v1 and site-data manifest (2026-11-05, 346063 B).` |
| `bash scripts/validate_schemas.sh` | **PASS** — `ALL SCHEMA & SITE-DATA VALIDATIONS PASSED!` |
| `cd app && ../flutter.sh analyze && ../flutter.sh test` | **PASS** — `No issues found! (ran in 1.5s)`, `All tests passed!` (`2` tests) |

### 3.2 Bounded Live End-to-End Smoke Verification (`/tmp/hyperpod-phase4-live`, cleaned up)

Executed against live `https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` and `https://api.hyperliquid.xyz/info` using the 5 spotlight addresses from [`docs/research/first-whitelist.md`](../research/first-whitelist.md) (with temporary smoke config overrides `reconstructDays=14`, `reconstruct1hDays=10`, `minTrades=15`, `maxDrawdown=2.0` yielding `3` real active whitelist members):

| Command / Check | Observed Verbatim Result |
|---|---|
| `hyperpod discover` (live S3 leaderboard) | `totalLeaderboardRows: 46271`, `passedLeaderboardCount: 1413`, `seedAddedCount: 1`, `usedLeaderboardFallback: false`, `warnings: []` |
| `hyperpod bootstrap --push-remote .../remote.git` | `completed: true`, `stage: "done"`, `activeWhitelistCount: 3`, `trackedCoins`: `24` coins (`BTC`, `ETH`, `SOL`, `HYPE` + `20` dynamic), `daysReconstructed: 15`, `monthsWritten: ["2026-11"]`, `totalBytesOnDisk: 6957783`, `pushed: ["state", "gh-pages"]`, `sziMismatches: 2` *(see §3.3 TWAP note below)* |
| `hyperpod snapshot` (second live tick) | `activeCount: 3`, `failedCount: 0`, `staleIds: []`, `rateLimitStats.totalRequests: 7`, `totalWeight: 32`, `warnings: []` |
| `hyperpod publish --no-fetch-fills --push-state --push-gh-pages --owner daily` | `rateLimitStats.totalRequests: 0`, `totalWeight: 0`, `pushed: [{branch: "state", attempts: 1}, {branch: "gh-pages", attempts: 1}]`, `totalBytesOnDisk: 6958037`, `warnings: []` |
| `git rev-list --count` on bare remote | `refs/heads/gh-pages: 1`, `refs/heads/state: 1` |
| `hyperpod verify-live --sample 3` | **`passed: true`**, `sampledCount: 3` (`0xfb941d...`, `0xb45f5c...`, `0xe9ec1d...`), `checksCount: 72` (`24` coins × `3` addresses), `mismatches: []`, `totalWeight: 86`, `warnings: []` |
| `hyperpod health --hours 24` | **`healthy: true`**, `fastRunsTotal: 2`, `fastRunsSucceeded: 2`, `fastSuccessRate: 1.0`, `avgFastWeight: 54.0`, `dailyRunsTotal: 6`, `dailyRunsFailed: 0`, `maxGapMinutesObserved: 1.33`, `gaps: []` |
| `hyperpod validate-contracts` + `validate_schemas.sh` | `Validated config v1 and site-data manifest (2026-11-21, 6958037 B).` / `ALL SCHEMA & SITE-DATA VALIDATIONS PASSED!` |

### 3.3 Live Operational Observation: `userFillsByTime` vs TWAP Slice Fills

- During `bootstrap` live reconstruction, `0xfb941d2344b72ea9101400392b818173772d0ddf` reported `sziMismatches: 2` (`HYPE: reconstructed=3088.39, clearinghouse=0.0` and `ONDO: reconstructed=2007474.0, clearinghouse=0.0`).
- Direct API inspection confirmed Hyperliquid's `userFillsByTime` omits TWAP slice executions (`userTwapSliceFillsByTime`, e.g. `twapId=2201792` at `t=1788990422029`), whereas the next regular fill carries the exact updated `startPosition` (`39751.15` vs `42839.54`).
- As designed (`§6.5`), `reconstruct` logged clear warnings without aborting, live `snapshot` immediately synced `latest.json` and live series from `clearinghouseState`, and `verify-live --sample 3` passed all `72 / 72` checks (`mismatches: []`). *(Tracked as an optional future enhancement if pre-launch TWAP slice inclusion is desired.)*

---

## 4. Independent Adversarial Code Audit & TDD Remediation

1. **Primary Audit (`e82cf19b-acb3-4c95-abe3-8aa88e02eaad`):** Identified `6` findings (`2 CRITICAL`, `3 MAJOR`, `1 MINOR`):
   - `FINDING-01` (`CRITICAL`): Move daily fill backfill fetch out of writer-locked `publish` job into lock-free `score` job + `_daily_artifact/data/fills` merge, filter already-tracked coins by existing `publish_fill_cursors` when `coinBackfillQueue` is non-empty, and clear `coinBackfillQueue` in `run_reconstruct(..., write_fills=True)`.
   - `FINDING-02` (`CRITICAL`): Capture `t_launch` after live `InfoApiClient` fetch loop in `run_reconstruct`, subtract post-`t_launch` in-flight deltas before checking `clearinghouseState.szi`, and cap `fill_cursors_by_id` to `<= t_launch`.
   - `FINDING-03` (`MAJOR`): Fix phantom position resurrection on exit + flat re-entry in `merge_snapshots_to_month` and `collect.py`, verify `tracked` boundaries in `verify_snapshot_compaction_equivalence`, and replay full `posChanges` (past `series.t[-1]`) on launch day in `collect.py`.
   - `FINDING-04` (`MAJOR`): Fold `batch_t <= eff_start_ms` fills into `init_pos` (eliminating duplicate same-ms `posChanges` at `eff_start_ms`), backdate `manifest.coins[].trackedFrom`, and emit `snapshots/latest.json` on standalone `run_reconstruct`.
   - `FINDING-05` (`MAJOR`): Decompress `.gz` files during `scan_directory_for_secrets` + require `{12,}` token length on GitHub prefixes, merge `fills/*/days/*.json` in `apply_scoped_overlay` for both `fast` and `daily`, preserve newer `fast` snapshots on `daily` overlay with `rebuild_manifest`, and guard `state`/`gh-pages` workflow clones with `git ls-remote`.
   - `FINDING-06` (`MINOR`): Exclude `latest.stale` IDs in `verify_live_positions` + reconcile post-poll suffix deltas, check initial `[launchedAt, live_day[0]]` gap in `evaluate_health`, and exclude intentional `1h` compacted intervals from `maxGapMinutesObserved`.
2. **Test-First Remediation & Secondary Audit (`e82cf19b-acb3-4c95-abe3-8aa88e02eaad`):**
   - Wrote `8` targeted regression tests first (confirmed `8 failed, 15 deselected` before fixes), applied root-cause remediations, re-ran full 7-step verification (`112 passed, 1 deselected`), and cleared secondary audit with **`PASS` (`0 CRITICAL`, `0 MAJOR`, `0 MINOR`, `0 NIT`)**.

---

## 5. Git Commits on `main` (Phase 4)

- `f882afd` — `chore(phase-4): start Phase 4 and tidy gitignore/status`
- `c15d77d` — `feat(storage): implement formats, site_writer >7d compaction, manifest rebuild, and single-commit git_publish (Task 4.3)`
- `79ba42b` — `feat(snapshot): implement permanent candle archiving and 10m fast snapshot collector (Task 4.1)`
- `7914516` — `feat(snapshot): implement pre-launch snapshot reconstruction and szi verification (Task 4.2)`
- `0eea3cd` — `feat(ops): implement health evaluator, verify-live reconciliation, and Phase 4 CLI commands (Task 4.4)`
- `33554d3` — `ci(workflows): add fast, daily, backfill, and frontend workflows and update README bootstrap guide (Tasks 4.5-4.6)`

---

## 6. Required User Actions (`§11.3` / `§13`)

1. **Push `main` & Configure GitHub Repository Settings (`§13`):**
   - Push local `main` commits (`git push origin main`).
   - Confirm in GitHub Repo Settings:
     - `Settings → Actions → General → Workflow permissions`: **Read and write permissions**.
     - `Settings → Pages → Build and deployment → Source`: **GitHub Actions**.
2. **Initial Full-Cohort Bootstrap & Workflow Smoke (`4.6` & Phase `4b`):**
   - Run local resumable bootstrap (`28–35h` full history) or trigger `pipeline-backfill.yml` on GitHub Actions, push `state` and `gh-pages`, and trigger `pipeline-fast.yml` & `pipeline-daily.yml` once on GitHub Actions (`verify-live --sample 3` runs automatically in `daily`).
   - *Per `§9 Phase 4` & `§11.3`, **Phase 4b** (`hyperpod health --hours 24` after 24h of scheduled cron runs) does NOT block starting Phase 5.*
3. **Approve Gate Review to Start Phase 5:**
   - Review this report and reply to authorize **Phase 5 — 前端基礎與計算引擎 (`Tasks 5.1–5.4`, including Proposal 0003 Stage A optional `tier`/`sources` schema fields)**.

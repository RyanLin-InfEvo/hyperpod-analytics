<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# HyperPod Cross-Session Status (`docs/status.md`)

- `currentPhase`: `Phase 4`
- `currentTask`: `Phase 4 Gate Review (4.1–4.6 Complete; Phase 4b 24h Soak Pending Live Schedule)`
- `state`: `WAITING_USER_REVIEW`
- `lastUpdated`: `2026-11-21T18:35:00+08:00`

## Completed Commits & Artifacts
- `2d8db9f` — Initial repo state on `main` (`Add gitignore and README`)
- `550051d` — `chore(research): remove temporary runner ratelimit probe workflow` (run `43681945367`)
- `e38db12` — `docs(research): record phase 0 API fixtures` (`89` JSON files across `11` addresses + `index.json`, secret-scanned clean)
- `1879125` — Phase 0 documentation (`docs/research/`, `docs/adr/`, `docs/proposals/0001`, `docs/phase-reports/phase-0.md`, updated `docs/implementation_plan.md`)
- `a4d4d96` — Task 1.1: `LICENSE` (`AGPL-3.0-or-later`), `README.md`, `.gitignore`, `shell.nix`, `config/seed_addresses.txt`, `config/blocklist.txt`
- `77a9691` — Task 1.2: 7 draft 2020-12 JSON schemas (`schemas/*.schema.json`) + 8 valid & 8 invalid examples
- `76163d8` — Task 1.3: `scripts/generate_site_fixtures.py` & complete synthetic 10-day `fixtures/site-data/` (`37` files)
- `55a82ef` — Task 1.4: `config/pipeline.yaml` (all parameters with `label` + `description`), `pipeline/` (`pyproject.toml`, `uv.lock`, `config.py`, `models/`, `cli.py`, `tests/`)
- `e36ba08` — Task 1.5: `.fvmrc` (`3.41.9`) & `app/` (Flutter Web scaffold, `very_good_analysis`, `mocktail`, bundled fonts in `app/test/fonts/`, `app_test.dart`)
- `6f78dd3` — Task 1.6: `scripts/validate_schemas.sh` & `.github/workflows/ci.yml` (`schemas`, `python`, `flutter` CI jobs verified in `nix-shell`)
- `2352b57` — `docs(status): record exact commit hashes through Task 1.6`
- `cd3f401` — `fix(contracts): resolve Phase 1 adversarial audit findings across schemas, models, and fixtures` (remediated `2 MAJOR`, `2 MINOR`; secondary audit `PASS`)
- `624008d` — `docs(phase-1): add Phase 1 gate report and set status to WAITING_USER_REVIEW`
- [`flutter.sh`](../flutter.sh) + Chromium (`CHROME_EXECUTABLE`) in [`shell.nix`](../shell.nix) — NixOS Flutter CLI/Chrome wrapper (`./flutter.sh --version`, `./flutter.sh test`, `./flutter.sh devices` verified)
- Phase 1 Gate Report: [`docs/phase-reports/phase-1.md`](phase-reports/phase-1.md)
- `6bfc4eb` — Task 2.1: `WeightRateLimiter` (`pipeline/src/hyperpod/client/ratelimit.py`, `pipeline/tests/test_ratelimit.py`, `pytest-cov` in `pyproject.toml`/`uv.lock`)
- `6d77ca2` — Task 2.2: Strict API Pydantic models (`pipeline/src/hyperpod/models/api.py`), `InfoApiClient` + fill/equity normalization (`pipeline/src/hyperpod/client/info_api.py`, `pipeline/tests/test_info_api.py`)
- `c1b391d` — Task 2.3: Streaming `LeaderboardClient` + gzip cache fallback (`pipeline/src/hyperpod/client/leaderboard.py`, `pipeline/tests/test_leaderboard.py`)
- `a0a58f4` — Task 2.4: `hyperpod debug-address` (`pipeline/src/hyperpod/cli.py`, `pipeline/tests/test_debug_address.py`)
- `d9437c5` — `fix(client): resolve Phase 2 adversarial audit findings across client, models, and CLI` (remediated `3 MAJOR`, `3 MINOR`; `94.59%` `hyperpod.client` coverage; secondary audit `PASS`)
- Phase 2 Gate Report: [`docs/phase-reports/phase-2.md`](phase-reports/phase-2.md)
- `578b413` — Task 3.1: UTC time helpers & Stage 1 prefilter / subaccount expansion (`pipeline/src/hyperpod/util/time.py`, `pipeline/src/hyperpod/discovery/prefilter.py`, `pipeline/tests/test_prefilter.py`, `.gitignore`)
- `1c5940c` — Task 3.2: Incremental round-trip trade reconstruction & Hypothesis equivalence tests (`pipeline/src/hyperpod/scoring/trades.py`, `pipeline/tests/test_trades.py`)
- `227ead0` — Task 3.3: All `14` `§5.4` metrics, `ADR-0001` equity, and `portfolio` fallbacks (`pipeline/src/hyperpod/scoring/metrics.py`, `pipeline/tests/test_metrics.py`)
- `b5d0984` — Task 3.4: Hard gates, conditional whitelist state machine, criteria hash, and tracked coin scheduler (`pipeline/src/hyperpod/scoring/filters.py`, `pipeline/src/hyperpod/scoring/whitelist.py`, `pipeline/tests/test_filters_whitelist.py`)
- `0bbf8cd` — Task 3.5 & Live Verification: Partitioned `StateStore`, `daily_runner`, `discover`/`score`/`backfill` CLI, `SpotBalance` live wire fix, and [`docs/research/first-whitelist.md`](research/first-whitelist.md)
- Phase 3 Gate Report: [`docs/phase-reports/phase-3.md`](phase-reports/phase-3.md)
- Proposal 0002 (`maxDrawdown` scale normalization — **Accepted Option A1**): [`docs/proposals/0002-max-drawdown-scale-normalization.md`](proposals/0002-max-drawdown-scale-normalization.md)
- Proposal 0003 (Multi-source address discovery & capital tiers — **Accepted Two-Stage Rollout, merged into `docs/implementation_plan.md`**): [`docs/proposals/0003-multi-tier-cohort-archetypes.md`](proposals/0003-multi-tier-cohort-archetypes.md)
- `f882afd` — `chore(phase-4): start Phase 4 and tidy gitignore/status`
- `c15d77d` — Task 4.3: [`formats.py`](../pipeline/src/hyperpod/storage/formats.py), [`state_store.py`](../pipeline/src/hyperpod/storage/state_store.py), [`site_writer.py`](../pipeline/src/hyperpod/storage/site_writer.py), [`git_publish.py`](../pipeline/src/hyperpod/storage/git_publish.py), [`test_site_writer_and_git_publish.py`](../pipeline/tests/test_site_writer_and_git_publish.py)
- `79ba42b` — Task 4.1: [`candles.py`](../pipeline/src/hyperpod/snapshot/candles.py), [`collect.py`](../pipeline/src/hyperpod/snapshot/collect.py), [`test_snapshot_and_candles.py`](../pipeline/tests/test_snapshot_and_candles.py)
- `7914516` — Task 4.2: [`reconstruct.py`](../pipeline/src/hyperpod/snapshot/reconstruct.py), [`test_reconstruct.py`](../pipeline/tests/test_reconstruct.py)
- `0eea3cd` — Task 4.4: [`health.py`](../pipeline/src/hyperpod/ops/health.py), [`cli.py`](../pipeline/src/hyperpod/cli.py), [`test_health_and_cli_phase4.py`](../pipeline/tests/test_health_and_cli_phase4.py), [`test_contracts.py`](../pipeline/tests/test_contracts.py)
- `33554d3` — Tasks 4.5–4.6: [`pipeline-fast.yml`](../.github/workflows/pipeline-fast.yml), [`pipeline-daily.yml`](../.github/workflows/pipeline-daily.yml), [`pipeline-backfill.yml`](../.github/workflows/pipeline-backfill.yml), [`deploy-frontend.yml`](../.github/workflows/deploy-frontend.yml), [`README.md`](../README.md)
- Phase 4 Gate Report: [`docs/phase-reports/phase-4.md`](phase-reports/phase-4.md)

## Active Background Jobs
- None *(local full-cohort `hyperpod bootstrap` not started; bounded live E2E smoke test in `/tmp/hyperpod-phase4-live` passed and cleaned up)*

## Pending User Actions / Proposals (§11.2 / §11.3 / §13)
1. **Push `main` & Configure GitHub Repo Settings (`§13`):** Push local `main` commits to `origin`, set `Settings → Actions → General → Workflow permissions` to **Read and write permissions**, and set `Settings → Pages → Build and deployment → Source` to **GitHub Actions**.
2. **Initial Full Bootstrap & Phase 4b 24h Scheduled Soak:** Run local `hyperpod bootstrap --push-remote ...` (or trigger `pipeline-backfill.yml`), trigger `pipeline-fast.yml` & `pipeline-daily.yml` on GitHub Actions, and after 24h of scheduled runs verify `hyperpod health --hours 24` *(Phase 4b does not block starting Phase 5 per `§9` & `§11.3`)*.
3. **Review Gate Report & Authorize Phase 5 (`5.1`–`5.4`):** Review [`docs/phase-reports/phase-4.md`](phase-reports/phase-4.md) and confirm proceeding to **Phase 5 — 前端基礎與計算引擎**.

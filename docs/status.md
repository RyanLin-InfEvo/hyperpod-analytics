<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# HyperPod Cross-Session Status (`docs/status.md`)

- `currentPhase`: `Phase 2`
- `currentTask`: `2.4 (Phase 2 Complete — Awaiting User Review)`
- `state`: `WAITING_USER_REVIEW`
- `lastUpdated`: `2026-11-16T09:42:00+08:00`

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

## Active Background Jobs
- None

## Pending User Actions / Proposals (§11.2 / §11.3 / §13)
1. **Review Phase 2 Gate Report (`docs/phase-reports/phase-2.md`):** Authorize proceeding to **Phase 3 — 候選發現與評分 (`3.1`–`3.5`)**.
2. **GitHub Actions & Remote Push (`§13`):** Confirm GitHub Repo `Settings → Actions → General → Workflow permissions` is set to **Read and write permissions**, push local `main` commits to `origin`, and verify `ci.yml` passes on GitHub Actions.

<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Phase 1 Gate Report — Repository Skeleton, Contracts & Fixtures

- **Phase:** `Phase 1 — 專案骨架與契約` ([`docs/implementation_plan.md`](../implementation_plan.md) `§9`)
- **Date:** `2026-11-15`
- **Status:** `WAITING_USER_REVIEW`
- **`【規範】` Modification Gate Triggered (`§11.2`):** No

---

## 1. Executive Summary

All six Phase 1 tasks (`1.1`–`1.6`) have been completed, verified under a pinned NixOS development shell ([`shell.nix`](../../shell.nix), [`.fvmrc`](../../.fvmrc)), and validated across all 4 cross-layer seams (`schemas/` ↔ Python Pydantic ↔ `fixtures/site-data/` ↔ `app/` Flutter Web scaffold).

Following Task `1.6`, an independent read-only adversarial audit (`fa3e1be1-ec8f-4f5e-84a6-c496d84ab3cb`) identified `2 MAJOR` and `2 MINOR` contract/parity defects. All four findings were reproduced with failing tests first (`8` new tests), remediated cleanly at the root cause in commit `cd3f401`, and confirmed clean (`0 CRITICAL`, `0 MAJOR`, `0 MINOR`, `0 NIT` — **`PASS`**) on secondary audit.

---

## 2. Task Completion Matrix (`1.1`–`1.6`)

| Task | Deliverable(s) | Key Verification & Coverage |
|---|---|---|
| **1.1** Repo, License & Nix Toolchain | [`LICENSE`](../../LICENSE), [`README.md`](../../README.md), [`.gitignore`](../../.gitignore), [`shell.nix`](../../shell.nix), [`config/seed_addresses.txt`](../../config/seed_addresses.txt), [`config/blocklist.txt`](../../config/blocklist.txt), [`docs/status.md`](../status.md) | Full official GNU `AGPL-3.0-or-later` text (`LICENSE`); required disclaimer `"Not financial advice. Data from public Hyperliquid APIs."`, Mermaid architecture diagram, and setup guide in `README.md`; deterministic NixOS `shell.nix` (`python312`, `uv`, `ruff`, `check-jsonschema`, `flutter 3.41.9`); `.worktrees/` and secret patterns excluded in `.gitignore`. |
| **1.2** Draft 2020-12 JSON Schemas & Examples | [`schemas/manifest.schema.json`](../../schemas/manifest.schema.json), [`schemas/whitelist.schema.json`](../../schemas/whitelist.schema.json), [`schemas/snapshot.schema.json`](../../schemas/snapshot.schema.json), [`schemas/fills.schema.json`](../../schemas/fills.schema.json), [`schemas/candles.schema.json`](../../schemas/candles.schema.json), [`schemas/criteria.schema.json`](../../schemas/criteria.schema.json), [`schemas/candidates.schema.json`](../../schemas/candidates.schema.json), [`schemas/examples/valid/`](../../schemas/examples/valid/), [`schemas/examples/invalid/`](../../schemas/examples/invalid/) | All `7` Draft 2020-12 schemas enforce `additionalProperties: false`, semantic invariants (`szi == 0 => entryPx == null`, `passed == (len(rejectReasons) == 0)`, `rejectReasons` enum keys on `stats.rejectedBy`, list-valued `excludeRoles` rule values, and single/backfill `oneOf` candles), accompanied by `8` valid and `8` single-fault invalid examples. |
| **1.3** Synthetic Site-Data Fixtures | [`scripts/generate_site_fixtures.py`](../../scripts/generate_site_fixtures.py), [`fixtures/site-data/`](../../fixtures/site-data/manifest.json) | Deterministic generator producing `37` files (`totalBytesOnDisk = 346,063 B`) spanning `10` UTC days (`2026-10-27..29` in `months/2026-10.json.gz`, `2026-10-30..2026-11-05` in `days/*.json`), `5` addresses (`id 0..4` including master+subaccount pair `0/1`, exited member `3`, and zero-loss `null`-PF newcomer `4`), `2` coins (`BTC`, `ETH`), position flip splits, liquidation (`liq=1`), stale `av: null` tick, `reconstructed: true` (`criteriaVersion=0`) interval, all `4` Stage 1 + `14` gate rules exported from `config/pipeline.yaml`, exact `candidates.json` ↔ `criteria.json` tally parity, and `§5.6` percentile `score` values. |
| **1.4** Python Package, Config & Strict Contracts | [`config/pipeline.yaml`](../../config/pipeline.yaml), [`pipeline/pyproject.toml`](../../pipeline/pyproject.toml), [`pipeline/uv.lock`](../../pipeline/uv.lock), [`pipeline/src/hyperpod/config.py`](../../pipeline/src/hyperpod/config.py), [`pipeline/src/hyperpod/models/`](../../pipeline/src/hyperpod/models/__init__.py), [`pipeline/src/hyperpod/cli.py`](../../pipeline/src/hyperpod/cli.py), [`pipeline/tests/`](../../pipeline/tests/test_contracts.py) | Single-source `config/pipeline.yaml` where every tunable parameter has non-empty `label` and `description` (`40+` specs verified via property tests); strict Pydantic models matching all `7` schemas (rejecting bool/`int` coercion, `NaN`/`inf`, blank strings, invalid intervals, and semantic mismatches); `hyperpod validate-contracts` verifies schemas, contracts, file inventory, and `totalBytesOnDisk`. |
| **1.5** Flutter Web Scaffold & Bundled Fonts | [`.fvmrc`](../../.fvmrc), [`app/pubspec.yaml`](../../app/pubspec.yaml), [`app/analysis_options.yaml`](../../app/analysis_options.yaml), [`app/lib/main.dart`](../../app/lib/main.dart), [`app/lib/app/app.dart`](../../app/lib/app/app.dart), [`app/lib/domain/domain.dart`](../../app/lib/domain/domain.dart), [`app/test/app_test.dart`](../../app/test/app_test.dart), [`app/test/fonts/`](../../app/test/fonts/) | Flutter `3.41.9` pinned in `.fvmrc` and `app/pubspec.yaml`; `very_good_analysis` + `mocktail` configured; pure-Dart `lib/domain/` with zero `package:flutter` imports; `Roboto-Regular.ttf`, `Roboto-Medium.ttf`, and `MaterialIcons-Regular.ttf` bundled in `app/test/fonts/` for deterministic Linux CI tests; required disclaimer rendered in app shell. |
| **1.6** CI Workflow & Schema Validator | [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml), [`scripts/validate_schemas.sh`](../../scripts/validate_schemas.sh) | `ci.yml` defines `schemas`, `python`, and `flutter` jobs; `validate_schemas.sh` validates all `8` valid examples, asserts exit code `1` on all `8` invalid examples, and validates all `37` `fixtures/site-data/` JSON and decompressed `.json.gz` files. |

---

## 3. Local CI & Verification Evidence (`nix-shell`)

All checks ran inside `nix-shell` (`python312 3.12.15`, `uv 0.11.21`, `ruff 0.15.14`, `check-jsonschema 0.37.2`, `flutter 3.41.9`, `dart 3.11.5`) after commit `cd3f401`:

| Check Command | Observed Result |
|---|---|
| `bash scripts/validate_schemas.sh` | **PASS** — `8/8` valid examples passed; `8/8` invalid examples exited with code `1`; `37` `fixtures/site-data/` files (`manifest`, `whitelist`, `criteria`, `candidates`, `snapshots/`, `fills/`, `candles/` incl. `.json.gz`) passed (`ALL SCHEMA & SITE-DATA VALIDATIONS PASSED!`). |
| `cd pipeline && uv run ruff check . && uv run ruff format --check .` | **PASS** — `All checks passed!`; `21 files already formatted`. |
| `cd pipeline && uv run mypy --strict src tests` | **PASS** — `Success: no issues found in 21 source files`. |
| `cd pipeline && uv run pytest` | **PASS** — `33 passed in 3.20s` (`test_config.py`: `9`, `test_contracts.py`: `24`). |
| `cd pipeline && uv run hyperpod validate-contracts` | **PASS** — Exit `0`: `Validated config v1 and site-data manifest (2026-11-05, 346063 B).` |
| `cd app && flutter analyze` | **PASS** — `No issues found! (ran in 0.6s)`. |
| `cd app && flutter test` | **PASS** — `All tests passed!` (`2` tests). |
| `cd app && flutter build web --release --base-href /hyperpod-analytics/` | **PASS** — `✓ Built build/web` (`19.1s`). |

---

## 4. Independent Adversarial Audit & Test-First Remediation (`cd3f401`)

An independent read-only adversarial audit (`fa3e1be1-ec8f-4f5e-84a6-c496d84ab3cb`) audited `a4d4d96..2352b57` and flagged `2 MAJOR` and `2 MINOR` findings:

1. **`[MAJOR-1]` `criteria` list values (`gates.excludeRoles`), `rejectedBy` enum keys, and fixture parity:**
   - Updated `schemas/criteria.schema.json` (`$defs.ruleValue` allowing non-empty string arrays, `$defs.rejectReason` constraining `stats.rejectedBy.propertyNames`) and `pipeline/src/hyperpod/models/criteria.py`.
   - Added `PipelineConfig.export_stage1_rules()` (`4` rules) and `export_gate_rules()` (`14` rules incl. `excludeRoles`) in `pipeline/src/hyperpod/config.py`.
   - Regenerated `fixtures/site-data/` via `scripts/generate_site_fixtures.py` so `criteria.json` exports all rules directly from `config/pipeline.yaml` and `stats` (`candidates=20, passed=4, rejectedBy`) is tallied directly from `candidates.json`.
2. **`[MAJOR-2]` Strict literal, string, and finite-float parity:**
   - Implemented `_require_strict_int` and `_require_strict_bool` validators in `pipeline/src/hyperpod/models/common.py` (`SchemaVersion`, `LiqFlag`, `StrictFalse`) so `True`/`False` are rejected on integer literals while preserving `mypy --strict` static types.
   - Applied `StrictStr`, `JsonNumber`, `NonNegNumber`, `PosNumber`, `UnitInterval`, `PosUnitFraction`, and `DustFraction` across `models/*.py` and `config.py`.
3. **`[MINOR-1]` Semantic and interval-order invariants:**
   - Enforced `szi == 0 => entryPx == null` in `schemas/snapshot.schema.json` and `Snapshot._check_coin_dim`.
   - Enforced `passed == (len(rejectReasons) == 0)` in `schemas/candidates.schema.json` (`if`/`then`/`else`) and `CandidateRow`.
   - Enforced `end >= start` on `CoinTrack`, `WhitelistAddress.membership`, `Snapshot.tracked`, `CriteriaHistoryItem`, and `reconstruct1hDays <= reconstructDays` on `SnapshotConfig`.
4. **`[MINOR-2]` Whitelist `§5.6` scores, single-fault invalid examples, CLI inventory check, and exit code `1` assertion:**
   - Updated `scripts/generate_site_fixtures.py` to compute active whitelist `score` values via the `§5.6` percentile composite formula (`0.7167`, `0.35`, `0.2`, `0.7333`).
   - Isolated single faults in `schemas/examples/invalid/*.json`, added `_verify_manifest_matches_disk()` in `pipeline/src/hyperpod/cli.py`, and enforced exit code `1` specifically in `scripts/validate_schemas.sh`.

- **Test-First Discipline:** All findings were reproduced with `8` failing tests first (`8 failed, 25 passed`), fixed at the root cause in `cd3f401`, re-verified across the full suite, and cleared on secondary adversarial audit (`0 CRITICAL`, `0 MAJOR`, `0 MINOR`, `0 NIT` — **`PASS`**).

---

## 5. Git Commits on `main` (Phase 1)

- `a4d4d96` — `chore(repo): initialize AGPL-3.0 license, README, Nix shell, and base config files`
- `77a9691` — `feat(schemas): add draft 2020-12 JSON schemas with valid and invalid examples`
- `76163d8` — `feat(fixtures): generate complete 10-day site-data fixture directory`
- `55a82ef` — `feat(pipeline): initialize Python package, pipeline.yaml, contract models, and tests`
- `e36ba08` — `feat(app): initialize Flutter 3.41.9 web app with bundled test fonts and very_good_analysis`
- `6f78dd3` — `ci(workflows): add ci.yml and schema validation script for python, flutter, and contracts`
- `2352b57` — `docs(status): record exact commit hashes through Task 1.6`
- `cd3f401` — `fix(contracts): resolve Phase 1 adversarial audit findings across schemas, models, and fixtures`

---

## 6. Required User Actions (`§11.3` / `§13`)

1. **Review & Authorize Phase 2 (`2.1`–`2.4`):** Review Phase 1 deliverables and confirm starting **Phase 2 — Hyperliquid 客戶端與正規化** (`rate limiter`, `API client`, `normalizer`, and `hyperpod debug-address`).
2. **Push Local Commits & Verify CI (`§13`):** Push `main` to `origin` when ready (`11` commits ahead of `origin/main` incl. Phase 0/Phase 1) and confirm the [`ci.yml`](../../.github/workflows/ci.yml) workflow (`schemas`, `python`, `flutter`) passes cleanly on GitHub Actions, alongside verifying repository workflow write permissions (`Settings → Actions → General → Read and write permissions`).

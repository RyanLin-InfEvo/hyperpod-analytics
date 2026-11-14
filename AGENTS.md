<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# HyperPod (`hyperpod-analytics`) Agent Engineering Guardrails

Authoritative specification: [docs/implementation_plan.md](docs/implementation_plan.md)
Cross-session handoff state: [docs/status.md](docs/status.md) (create on Phase 0 start if absent).

## 1. Bootstrapping & Continuity
1. At the start of every session, read `docs/status.md` (if present) and the relevant sections of `docs/implementation_plan.md` before taking action.
2. Update `docs/status.md` before Phase boundaries, long-running background jobs (`hyperpod bootstrap`), or whenever waiting on user review (`WAITING_USER_REVIEW`, `WAITING_PROPOSAL`, `WAITING_SOAK`).

## 2. Hard Invariants (Non-Negotiable)
1. **【規範】 Formulas Are Protected:** Do NOT modify any formula, gate definition, or data contract marked **【規範】** (`§5`, `§7`) without user approval via `docs/proposals/NNNN-*.md` (`§11.2`).
2. **Schema-First Contract Sync:** `schemas/*.schema.json` (draft 2020-12) is the single source of truth between `pipeline/` and `app/`.
   - Adding optional fields does not bump `schemaVersion`, but MUST update schemas, Python Pydantic models, Dart models, and `fixtures/site-data/` together.
   - Any breaking change (remove, rename, type change, semantic change, `schemaVersion + 1`) requires a `docs/proposals/` stop-and-ask gate.
3. **No Live Network Calls in Default CI:** Any test hitting real Hyperliquid / GitHub endpoints MUST be marked `@pytest.mark.live` and excluded from default CI runs.
4. **Never Persist Raw Candidate Fills:** Never store raw candidate fills in `state` (store reconstructed trades and daily summary stats only) or `gh-pages` (store aggregated whitelist `fills/` only).
5. **Never Delete Archived Public History:** Never auto-delete historical snapshots, whitelist fills, or 5m candles in `gh-pages/data/`. The ONLY permitted file removal is the verified >7-day daily-to-monthly compaction move (`§3.3`), allowed ONLY after the `.json.gz` monthly file passes schema validation and position-replay equivalence checks. If `data/` approaches 800 MB, emit a warning only.
6. **Unknown != Zero:** Positions outside a file's `tracked` range or an address's `coverageStart` are unknown and MUST be excluded—never treated as `0`.
7. **Single-Source Parameters:** All tunable thresholds, weights, and rate limits belong in `config/pipeline.yaml` with `label` and `description` metadata—never hardcode thresholds in Python or Dart.
8. **State Ownership Isolation:** In `state`, `fast` owns `state/fast/` (`fillsCursorPublish`) and `daily` owns `state/daily/` (`fillsCursorScoring`, `publish_fill_cursors.json`). Never share mutable state files or cursors across `fast` and `daily`.
9. **Writer Lock Discipline:** Never hold `concurrency: hyperpod-writer` during long-running API collection (`daily score`) or Flutter web builds (`deploy-frontend build`). Only fast-completing publish jobs hold the writer lock.
10. **Human-Only Gate Respect:** Never bypass items listed in `docs/implementation_plan.md` `§13`.
11. **Zero Secrets in Repo:** HyperPod requires no private API keys (Hyperliquid Info API is public; Actions use built-in `GITHUB_TOKEN`). Never write PATs, SSH keys, `.env` files, or credentials inside the repository. Before every commit, verify staged diffs contain no `github_pat_`, `ghp_`, `PRIVATE KEY`, or auth headers.

## 3. Authorization Matrix (`§11.2`)
- **Autonomous (No Stop Required):** Implementation architecture, refactoring, dependency choices, performance optimizations (e.g., Web Worker / binary fallback), CI fixes, tuning default numeric thresholds in `config/pipeline.yaml` (preserving formulas, documented in `docs/adr/NNNN-*.md`), adding optional JSON fields, clarifying unspec'd edge cases that do not alter existing `【規範】` outputs (documented in ADR).
- **Stop & Propose (`docs/proposals/NNNN-*.md`):** Any change to `【規範】` formulas (`§5`, `§7`), breaking schema changes (`schemaVersion + 1`), or changes to license (`AGPL-3.0-or-later`), market scope (HIP-3/spot), or data retention policy.

## 4. Phase Workflow & Cross-Language Parity
- Work autonomously task-by-task within the active Phase; keep task commits/PRs atomic and verify all tests + linters pass before moving to the next task.
- At the end of each Phase, generate `docs/phase-reports/phase-N.md` (completed tasks, CI/test evidence, ADRs written, any `§13` human actions needed), set `docs/status.md` to `WAITING_USER_REVIEW`, and **stop for user review** before starting the next Phase.
- All `app/lib/domain/` math must remain pure Dart (no `package:flutter` imports) and pass golden parity tests against `pipeline/tests/reference/frontend_math.py` (`relTol <= 1e-9, absTol <= 1e-9`).
- Golden widget tests must use bundled fonts in `app/test/fonts/` and treat CI Linux rendering as canonical.

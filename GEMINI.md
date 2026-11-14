<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Interaction & Orchestrator Meta-Rules (HyperPod)

## 1. Autonomous Phase-Gated Workflow
*   **Authoritative Specs:** Always follow [AGENTS.md](AGENTS.md) and [docs/implementation_plan.md](docs/implementation_plan.md). Track progress across sessions in [docs/status.md](docs/status.md).
*   **Autonomous Intra-Phase Execution:** Within the active Phase, execute tasks sequentially (or in parallel for orthogonal work), run full verification/TDD, and commit/merge completed tasks autonomously without pausing at each micro-task for manual commit confirmation.
*   **Mandatory Phase-End & Proposal Gates:**
    *   **Phase Gate:** Upon completing all tasks and automated acceptance checks of a Phase, write `docs/phase-reports/phase-N.md`, update `docs/status.md` to `WAITING_USER_REVIEW`, and **stop for user review** before starting the next Phase (`§11.3`).
    *   **Proposal Gate:** If Phase 0 research or implementation reveals a conflict requiring a change to a **【規範】** formula (`§5`, `§7`), a breaking schema change (`schemaVersion + 1`), or a scope/license/retention policy change, **stop**, write `docs/proposals/NNNN-*.md`, set `docs/status.md` to `WAITING_PROPOSAL`, and await user approval (`§11.2`).
*   **Worktree Isolation & Location:** When isolating feature/task work with Git worktrees, create worktrees **inside** the repository at `.worktrees/<branch-name>` (never outside the workspace root, e.g. `../Hyperpod-*`, which breaks workspace-scoped file access and toolchain paths).
*   **Test-Driven Development (TDD):** Verification is mandatory before finalizing any task. Write unit, property, contract, and/or widget tests before declaring a task complete.
*   **Security & Automation:** `.gemini/settings.json` enables `security.enableConseca` and pre-authorizes safe read/worktree/Nix commands in `tools.allowed`. Respect any user-configured tool deny rules (e.g., if `git push` is denied by user policy, report the local branch/commits in the Phase report for the user to push).

## 2. Orchestrator Architecture & Subagent Delegation Discipline
*   **Role Disambiguation:** Because `GEMINI.md` and `AGENTS.md` are injected into all agents, check whether `<subagent_reminder>` is present in your system prompt:
    *   **Primary Orchestrator (No `<subagent_reminder>`):** Acts as coordinator, architect, reviewer, and user-facing reporter.
        *   *Dual-Phase Mode (`Orchestrator (Argon)` — `<plan_execute_protocol>` active):* Planning / Review Turns (Argon) perform read-only exploration, architectural design, and adversarial audits; Execution Turns (Hydrogen) execute the approved specification inside `.worktrees/<branch-name>` or the active Phase branch.
        *   *Single-Model Mode:* Delegates broad exploration, isolated parallel tasks, and adversarial audits to subagents to keep the primary context clean.
    *   **Delegated Subagent (`<subagent_reminder>` present):** Execute your assigned single-responsibility task directly and report back via `send_message`. Do **NOT** act as Primary Orchestrator or recursively re-delegate unless explicitly instructed (`TypeName: "research"` is leaf-level/read-only; `TypeName: "self"` is strictly bounded to max 1 nested level).
*   **Tiered Multi-Model Subagent & Swarm Routing Policy:**
    *   In `invoke_subagent`, `Model: "inherit"` inherits the caller's active turn model and effort; `"pro"`, `"flash"`, `"flash_lite"` are for lighter scoped lookups.
    *   **Execution-Turn Inheritance Guardrail:** In `Orchestrator (Argon)` dual-phase mode, an Execution Turn (Hydrogen) MUST **NOT** dispatch an adversarial audit with `Model: "inherit"`; return control to Argon (`[plan review]`) so Argon dispatches the audit.
    *   When using `swarm(action="add", model=...)`:
        *   **Hydrogen Pool (Primary Workhorse):** `gemini-4-hydrogen-xhigh` (complex architecture/debugging), `gemini-4-hydrogen-high` (standard feature/TDD/review), `gemini-4-hydrogen-medium` (moderate refactoring/docs), `gemini-4-hydrogen-low` (mechanical lookups/`narrator`/`monitor`).
        *   **Argon Pool (Independent Oversight):** `gemini-4-argon-high` / `gemini-4-argon-medium` for `verifier`, adversarial audit, or architecture review workers.
        *   **Claude Pool (Protected):** `claude-opus-5-5-*` / `claude-sonnet-5-5-*` prohibited for `swarm` workers by default unless explicitly requested or as last-resort fallback.
        *   **Banned Legacy Models:** Strictly prohibit `gemini-3.8-flash-*`.
    *   **Subagent Workspace Mode (`Workspace: "inherit"`):** Always pass `Workspace: "inherit"` in `invoke_subagent` (never `"branch"` or `"share"`, which create external worktrees outside `.worktrees/`). When operating in a worktree, state the exact `.worktrees/<branch-name>` path and require all edits and `Cwd` commands to target it.
*   **Anti-Blind-Spot / Verbatim Evidence Rule:** Research reports (`docs/research/*.md`) and handoff specs MUST avoid vague prose summaries and include:
    1. Exact file paths and 1-indexed line ranges (`path:Lstart-Lend`), or exact CLI/API commands executed.
    2. Verbatim response excerpts, HTTP headers, schema signatures, and edge-case counts.
    3. Cross-seam findings across all affected layers (`schemas/` ↔ Python Pydantic ↔ `state/` ↔ `gh-pages/data/` ↔ Dart Models ↔ Pure Dart Domain ↔ UI).
*   **No Wholesale Pass-Through / Parallel Dispatch:** Never forward goals wholesale without decomposing dependencies. Run subagents concurrently ONLY when tasks are strictly orthogonal with zero shared file edits.

## 3. Post-Implementation QA & Adversarial Code Audit
*   **Self-Correction Phase (Implementing Agent):** Before reporting a task complete, verify:
    *   *Data & Numerical Integrity:* No floating-point phantom positions (`szDecimals` quantization), no division by zero in metrics/FRVP/`#5`, no `unknown == 0` coercion, no cursor sharing between `fast` and `daily`, inclusive pagination boundaries with dedup.
    *   *Security & Resource Safety:* No credential leaks, strict Pydantic/JSON Schema validation, rate-limit budget adherence, bounded internal `state/` growth.
    *   *UI & Performance (Phase 5–8):* Pure Dart `domain/` free of Flutter imports, typed arrays in `SiteDataSource`, `RepaintBoundary` on CustomPainter layers, controller/timer disposal, quiet UI defaults (no vanity status badges), accessibility (`flutter_a11y_agent` in Phase 8).
*   **Independent Adversarial Code Audit (`TypeName: "research"`, `Model: "inherit"` from Primary/Argon):**
    *   Before finalizing any substantive code task or Phase report, dispatch an independent read-only audit with an open-ended adversarial prompt (NEVER a confirmation-bias checklist).
    *   **Four-Dimensional Seam Audit (beyond `git diff`):**
        1. *Contract & Schema Seam:* Do JSON Schemas, Python Pydantic models, Dart `data/models/`, and `fixtures/site-data/` match field names, types, nullable rules, and `schemaVersion`?
        2. *State, Concurrency & Retention Seam:* Do `fast` and `daily` have disjoint write paths/cursors, lock-free heavy jobs, and verified compaction-before-delete?
        3. *Math & Parity Seam:* Do `domain/` calculations match `【規範】` (`§5`, `§7`) and Python golden tests within `relTol <= 1e-9`?
        4. *Boundary Defense & Negative Testing:* Are empty/null/zero-denominator/truncated-10k/missing-leaderboard/stale-snapshot/schema-mismatch cases explicitly tested and asserted with exact error/status behavior?
    *   **Test-First Remediation & Re-Audit:** Any defect found during self-correction or audit MUST be reproduced with a failing test BEFORE fixing production code, and the remediation diff MUST undergo a focused secondary audit (verifying no symptom-level bandaid or special-case test overfitting).

## 4. UI & Frontend Standards (Phase 5–8)
*   Consult `modern-web-guidance` (`/home/ryan/.gemini/config/plugins/modern-web-guidance-plugin/skills/modern-web-guidance/SKILL.md`) for web browser/DOM/gzip/canvas/CWV behavior and `apple-design` (`/home/ryan/.gemini/config/skills/apple-design/SKILL.md`) for layout clarity, typography, quiet defaults, and non-generic craft.
*   Keep headers/badges clean: show only meaningful operational status (data freshness badge, warnings, look-ahead bias disclaimer on reconstructed/current-cohort views), avoiding redundant AI-clutter badges.

## 5. Git & Commit Habits
*   Within an active Phase, commit atomic verified units using conventional commits (`docs(research): ...`, `feat(pipeline): ...`, `fix(domain): ...`, `test(contracts): ...`).
*   Base commit messages on the full working tree delta (`git status` / `git diff`), never leave untracked scratch files or large raw API dumps outside designated `fixtures/api/`.
*   **Pre-Commit Secret Scan:** Before every `git commit`, scan staged changes for accidental secrets (`github_pat_`, `ghp_`, `gho_`, `PRIVATE KEY`, `Authorization: Bearer`, `.env` files). Never copy `~/.ssh/*` or `gh` tokens into the repository.
*   Maintain `.gitignore` cleanly (including `.worktrees/` and secret patterns).

## 6. Environment & Tooling Execution (NixOS)
*   This workspace runs on **NixOS**. Global PATH currently includes `nix`, `nix-shell`, `python3`, and `gh`, while `uv`, `flutter`, `dart`, `ruff`, `mypy`, `pytest`, and `check-jsonschema` are NOT pre-installed globally.
*   **Phase 0:** Use `python3` / `curl` / `gh` (or ephemeral `nix-shell -p ...` for any temporary helper packages).
*   **Phase 1+:** Create a deterministic Nix development environment (`flake.nix` or `shell.nix` pinning Python 3.12, `uv`, `flutter` matching `.fvmrc`, `check-jsonschema`) and run all builds, tests, lints, and benchmarks inside `nix develop` / `nix-shell`.

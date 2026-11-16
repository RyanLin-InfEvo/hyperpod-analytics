<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Phase 2 Gate Report — Hyperliquid API Client, Rate Limiter & Normalization

- **Phase:** `Phase 2 — API Client 與限流器` ([`docs/implementation_plan.md`](../implementation_plan.md) `§9`)
- **Date:** `2026-11-16`
- **Status:** `WAITING_USER_REVIEW`
- **`【規範】` Modification Gate Triggered (`§11.2`):** No

---

## 1. Executive Summary

All four Phase 2 tasks (`2.1`–`2.4`) have been implemented, verified under the pinned NixOS development shell ([`shell.nix`](../../shell.nix)), and validated offline and against the live Hyperliquid Info API.

Following Task `2.4`, an independent read-only adversarial audit (`3853e152-5d57-4535-b113-a9fc47770270`) identified `3 MAJOR` and `3 MINOR` findings across concurrency/rate-limit locking, `aggregateByTime=true` 10k truncation detection, `userFunding` 500-item boundary saturation, duplicate cursor requests, shared leaderboard telemetry, and wire contract edge guards. Every finding was reproduced with a failing regression test first (`7 failed, 11 passed` across the targeted client/CLI suites before editing production code), fixed at the root cause in commit `d9437c5`, re-verified across the full CI suite (`94.59%` coverage on `hyperpod.client`), and cleared with `0 CRITICAL`, `0 MAJOR`, `0 MINOR`, `0 NIT` (**`PASS`**) on secondary adversarial audit.

---

## 2. Task Completion Matrix (`2.1`–`2.4`)

| Task | Deliverable(s) | Key Verification & Coverage |
|---|---|---|
| **2.1** Weight-Aware Rate Limiter | [`pipeline/src/hyperpod/client/ratelimit.py`](../../pipeline/src/hyperpod/client/ratelimit.py), [`pipeline/tests/test_ratelimit.py`](../../pipeline/tests/test_ratelimit.py), [`pipeline/pyproject.toml`](../../pipeline/pyproject.toml), [`pipeline/uv.lock`](../../pipeline/uv.lock) | `WeightRateLimiter` enforces both token-bucket refill and a 60s rolling weight ledger (`800/min` CI, `1000/min` local), post-response dynamic weight surcharges (`ceil(n/20)` fills/funding, `ceil(n/60)` candles) including temporary debt recovery, `2^k + jitter` exponential backoff, and `300s` budget halving on `>= 2` consecutive HTTP `429` responses (`97%` coverage). |
| **2.2** Strict API Models, Typed Client & Normalization | [`pipeline/src/hyperpod/models/api.py`](../../pipeline/src/hyperpod/models/api.py), [`pipeline/src/hyperpod/models/__init__.py`](../../pipeline/src/hyperpod/models/__init__.py), [`pipeline/src/hyperpod/client/info_api.py`](../../pipeline/src/hyperpod/client/info_api.py), [`pipeline/tests/test_info_api.py`](../../pipeline/tests/test_info_api.py) | Strict Pydantic validation of all `88` recorded Phase 0 API fixtures (`11` addresses × `8` endpoints); `InfoApiClient` with bounded `max_concurrency = 4` around rate-limit acquisition and HTTP execution, inclusive boundary pagination + `(time, tid, hash, coin, dir)` dedup + dense-ms livelock guard (`userFillsByTime`), inclusive `startTime = last_t` pagination + `(time, hash, delta.coin)` dedup + `lastFundingCoins` seeding (`userFunding`), `ADR-0001` `compute_account_equity`, `ADR-0002` main-perp filtering & self-vs-counterparty `liq` attribution, `Proposal 0001` `(time, api_seq_index, leg_idx)` ordering & flip-splits, and `§3.4` VWAP aggregation (`94%` coverage). |
| **2.3** Streaming Leaderboard Client | [`pipeline/src/hyperpod/client/leaderboard.py`](../../pipeline/src/hyperpod/client/leaderboard.py), [`pipeline/src/hyperpod/client/__init__.py`](../../pipeline/src/hyperpod/client/__init__.py), [`pipeline/tests/test_leaderboard.py`](../../pipeline/tests/test_leaderboard.py) | Chunked streaming download with `Content-Length` and `max_bytes` caps, strict `LeaderboardResponse` validation, atomic gzipped JSON cache (`tempfile` + `os.replace`), fallback with warning on HTTP/transport/JSON/schema failures, and shared `WeightRateLimiter` status telemetry (`92%` coverage). |
| **2.4** `hyperpod debug-address` & Offline/Live Tests | [`pipeline/src/hyperpod/cli.py`](../../pipeline/src/hyperpod/cli.py), [`pipeline/tests/test_debug_address.py`](../../pipeline/tests/test_debug_address.py) | Implemented `hyperpod debug-address` supporting live `InfoApiClient` inspection and offline `--fixtures-dir` inspection; outputs normalized equity, `equitySource`, open positions, portfolio summary, fills summary, funding summary, `rateLimitStats`, and warnings. Verified with `respx` mocks, fixture directory tests, and `@pytest.mark.live` smoke test. |

---

## 3. Local CI, Coverage & Live Verification Evidence (`nix-shell`)

All checks ran inside `nix-shell` after commit `d9437c5`:

| Check Command | Observed Result |
|---|---|
| `bash scripts/validate_schemas.sh` | **PASS** — `ALL SCHEMA & SITE-DATA VALIDATIONS PASSED!` |
| `cd pipeline && uv run ruff format --check . && uv run ruff check .` | **PASS** — `29 files already formatted`, `All checks passed!` |
| `cd pipeline && uv run mypy --strict src tests` | **PASS** — `Success: no issues found in 29 source files` |
| `cd pipeline && uv run pytest --cov=hyperpod.client --cov-report=term-missing --cov-fail-under=85` | **PASS** — `56 passed, 1 deselected in 8.32s`; `hyperpod.client` total coverage **`94.59%`** (`__init__.py` `100%`, `info_api.py` `94%`, `leaderboard.py` `92%`, `ratelimit.py` `97%`) |
| `cd pipeline && uv run pytest -m live` | **PASS** — `1 passed, 56 deselected in 1.13s` (`debug-address 0xfc27136e...` against live Hyperliquid API) |
| `cd app && ../flutter.sh analyze && ../flutter.sh test` | **PASS** — `No issues found! (ran in 0.8s)`, `All tests passed!` (`2` tests) |

---

## 4. Independent Adversarial Audit & Test-First Remediation (`d9437c5`)

An independent read-only adversarial audit (`3853e152-5d57-4535-b113-a9fc47770270`) inspected `68c5da7..a0a58f4` and reported `3 MAJOR` and `3 MINOR` findings:

1. **`[MAJOR-1]` Concurrency / rate-limit bypass (`InfoApiClient._post_info`):**
   - Moved `async with self._semaphore:` around `rate_limiter.acquire()`, `_client.post()`, `record_response()` / `record_transport_error()`, and `adjust()`, releasing `_semaphore` prior to `wait_for_retry()` sleeps so concurrent tasks cannot acquire pre-request weights before prior dynamic surcharges or `429` halving take effect.
2. **`[MAJOR-2]` `truncated_10k` false negatives and overlap false positives (`InfoApiClient.fetch_user_fills_by_time`):**
   - Removed `raw_count >= 10_000` (which counted `overlap_ms` boundary duplicates) and added initial `startPosition != 0` inspection across each main-perp coin's earliest fill when `start_time == 0` with empty `seed_keys` so compressed `< 10,000`-aggregated-fill 10k-truncated histories are still flagged `truncated_10k = True`.
3. **`[MAJOR-3]` `userFunding` 500-item boundary saturation & empty-page seed preservation (`InfoApiClient.fetch_user_funding`):**
   - Preserved `(start_time, sorted(seed_coins))` on empty incremental responses, matched `ev.time == start_time and ev.delta.coin in seed_coins` regardless of hash, and advanced `cursor = page[-1].time + 1` when a full `500`-item page yields `added == 0`.
4. **`[MINOR-1]` Redundant duplicate page call on single-millisecond fill saturation (`InfoApiClient.fetch_user_fills_by_time`):**
   - Immediately advanced `cursor = last_t + 1` with the dense boundary warning when all `2,000` fills fall on `cursor`.
5. **`[MINOR-2]` `LeaderboardClient` shared `WeightRateLimiter` telemetry & streak reset:**
   - Added `WeightRateLimiter.record_transport_error()` and wired `LeaderboardClient` to record final-attempt `429`/`5xx`, `200 OK`/`4xx`, and final transport errors on `self.rate_limiter`.
6. **`[MINOR-3]` Wire contract & normalization edge guards:**
   - Made `AssetPositionDetail.entryPx` nullable, added `SpotClearinghouseStateResponse.evmEscrows`, defaulted optional `RawFill.feeToken` to `"USDC"`, added `"deleverag"` fallback dir matching with self-vs-counterparty `liq` attribution, guarded `addr_id >= 0` in `aggregate_public_fill_rows`, and filtered residual `szi == 0.0` positions in `debug-address`.

- **Test-First Discipline:** All `6` findings were reproduced with failing tests first (`7 failed, 11 passed, 1 deselected` across `test_info_api.py`, `test_leaderboard.py`, and `test_debug_address.py`), fixed at the root cause in `d9437c5`, re-verified across the full suite (`56 passed`), and cleared on secondary adversarial audit (`0 CRITICAL`, `0 MAJOR`, `0 MINOR`, `0 NIT` — **`PASS`**).

---

## 5. Git Commits on `main` (Phase 2)

- `6bfc4eb` — `feat(client): implement weight-aware rate limiter and 429 backoff (Task 2.1)`
- `6d77ca2` — `feat(client): add strict API models, InfoApiClient pagination, and fill/equity normalization (Task 2.2)`
- `c1b391d` — `feat(client): add streaming leaderboard client with schema validation and cache fallback (Task 2.3)`
- `a0a58f4` — `feat(cli): implement hyperpod debug-address and offline/live tests (Task 2.4)`
- `d9437c5` — `fix(client): resolve Phase 2 adversarial audit findings across client, models, and CLI`

---

## 6. Required User Actions (`§11.3` / `§13`)

1. **Review & Authorize Phase 3 (`3.1`–`3.5`):** Review Phase 2 deliverables and confirm starting **Phase 3 — 候選發現與評分 (`daily`)** (`prefilter.py`, `trades.py`, `metrics.py`, `filters.py`, `whitelist.py`, `state_store.py`, and `discover` / `score` / `backfill` CLI commands).
2. **Remote Push & GitHub Actions (`§13`):** Push `main` to `origin` when ready (all Phase 0–2 commits remain local) and verify `ci.yml` passes on GitHub Actions.

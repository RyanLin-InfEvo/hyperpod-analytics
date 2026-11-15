<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# ADR 0001: Account Equity Calculation Across Hyperliquid Abstraction Modes

- **Date:** `2026-11-15`
- **Status:** `Accepted`
- **Authoritative Spec:** `docs/implementation_plan.md` (`§5.1`, `§5.4`, `§6.3`, `§6.5`, `§11.2`)
- **Research Evidence:** [`docs/research/equity.md`](../research/equity.md)

## Context
`docs/implementation_plan.md §5.4` specifies `equity` as:
> Phase 0.3 驗證後確認（預設：`default` 模式 = perp `marginSummary.accountValue`；統一帳戶 / 組合保證金 = perp `accountValue` + 現貨 USDC 等值，避免重複計算）

Empirical probing in Task 0.3 showed that Hyperliquid's `clearinghouseState`, `spotClearinghouseState`, `portfolio`, and `leaderboard` endpoints expose overlapping or partial views of account equity depending on `userAbstraction`:
1. **Double-counting hazard (`unifiedAccount`)**: In `unifiedAccount`, `clearinghouseState.marginSummary.accountValue` mirrors the perp-utilized portion of `spotClearinghouseState.balances[USDC].total`, not a separate balance. Adding them doubles equity (`$13.42M` instead of `$6.71M` on `0x1f250df5...`).
2. **Undercounting hazard (`unifiedAccount` / `portfolioMargin`)**: When idle USDC sits in spot (`0xdf9ea6ec...`), when USDC is supplied into portfolio margin (`0xbf732ea0...`), or when a unified account holds zero open main-perp positions (`ch_av = 0`), `clearinghouseState.marginSummary.accountValue` excludes the idle/supplied collateral.
3. **Negative spot USDC hazard (`portfolioMargin`)**: When an account borrows USDC against spot collateral such as `HYPE` (`0xb1ec7feb...`), `spotClearinghouseState.balances[USDC].total` is negative (`-$847.3k` / `-$837.9k`), while `tokenToAvailableAfterMaintenance[0] + crossMaintenanceMarginUsed` (`$4.75M`) accurately reflects haircutted shared collateral plus perp equity.
4. **HIP-3 & non-main-perp scope (`§1`, `§5.2`)**: `leaderboard.accountValue`, `portfolio.allTime`, and `portfolio.perpAllTime` include HIP-3 builder DEXes (`xyz`, `io`, `cash`, etc.) and/or non-USDC spot tokens and subaccounts, whereas HyperPod tracks **main perp dex only**.

Under `docs/implementation_plan.md §11.2` (Authorization Matrix — *"釐清未定義的實作邊界（不影響既有【規範】公式結果）"*), formalizing the exact computation clarifies `§5.4` without altering any `【規範】` formula or requiring a `docs/proposals/` stop-and-ask gate.

## Empirical Evidence Summary
- **200-candidate Stage 1 survey (Phase 0.3 / 0.8 live survey)**: `unifiedAccount` = `111` (`55.5%`), `portfolioMargin` = `43` (`21.5%`), `disabled` = `38` (`19.0%`), `default` = `8` (`4.0%`). Together, **`77.0%`** (`154 / 200`) share collateral between spot and perp.
- **9-address verification (Phase 0.3 sequential probe & `fixtures/api/`)**:
  - `0x1f250df5` (`unifiedAccount`): `ch_av` (`6,709,328.0252320003`) ≡ `spot.USDC.total` (`6,709,328.0252327602`) ≡ `avail_after_maint_usdc + c_mm` (`6,709,328.0252327602`) within `7.6e-7` USD when no tick occurs between requests.
  - `0xdf9ea6ec` (`unifiedAccount`): `spot.USDC.total` (`$1,341,247.52`) captures `$781,093.32` perp equity + `~$560,154.20` idle spot USDC; two-tick probes (`3s` apart + `~0.15s` RTT) confirm sub-second differences (`+$1,128` vs `+$948` on `0xdf9ea6ec`, `-$600` vs `-$100` on `0x1f250df5`) come from live mark-price ticks between sequential REST calls.
  - `0xbf732ea0` (`portfolioMargin`): `spot.USDC.total` (`$1,687,466.66` in fixture) ≡ `ch_av` (`$1,118,838.42`) + `supplied` USDC (`$568,628.24`) ≡ `avail_after_maint_usdc + c_mm`.
  - `0xb1ec7feb` (`portfolioMargin`): `spot.USDC.total` is `-$837,850.11` (`-$847,343.44` in fixture, `$5.33M` borrowed USDC against `123,085.46 HYPE` at `ltv = 0.65`), while `avail_after_maint_usdc + c_mm = $4,749,502.76` (`$4,736,433.31` in fixture) cleanly captures haircutted shared collateral + perp equity.
  - `0x023a3d05` & `0xfc27136e` (`disabled`): `0x023a3d05` spot USDC (`$1.65M`) is isolated from main perp `ch_av` (`$5.74M`), and `0xfc27136e` HIP-3 equity (`$20.42M` in `xyz`/`cash`) is summed into `pf_perpAllTime_av` (`$43.52M`) and `lb_av` (`$44.01M`) but excluded from main-perp `ch_av` (`$23.10M`).

## Decision
1. **Exact `equity` & `equitySource` formula (`§5.4`)**:
   - Extract from `clearinghouseState` (main perp dex, no `dex` param):
     - `ch_av = float(marginSummary.accountValue)`
     - `c_mm = float(crossMaintenanceMarginUsed)` (default `0.0`)
   - If `userAbstraction in {"disabled", "default"}`:
     - `equity = ch_av`
     - `equitySource = "perp"`
   - Otherwise (`"unifiedAccount"`, `"portfolioMargin"`, `"dexAbstraction"`, or any future shared-margin abstraction):
     - Extract from `spotClearinghouseState`:
       - `spot_usdc_total`: `float(total)` for balance with `coin == "USDC"` (or `token == 0`), defaulting to `0.0` if absent.
       - `avail_after_maint_usdc`: `float(val)` for token `0` in `tokenToAvailableAfterMaintenance`, or `None` if token `0` is absent.
     - `terms = [ch_av, spot_usdc_total]`
     - If `avail_after_maint_usdc is not None`: append `avail_after_maint_usdc + c_mm`
     - `equity = max(terms)`
     - `equitySource = "perp+spot"`

2. **Why `max(ch_av, spot_usdc_total, avail_after_maint_usdc + c_mm)`**:
   - Prevents double-counting in `unifiedAccount` (since `ch_av` is already inside `spot_usdc_total` and `avail_after_maint_usdc + c_mm`).
   - Recovers idle spot USDC (`0xdf9ea6ec`) and handles `ch_av == 0.0` when a `unifiedAccount` has no open main-perp positions.
   - Prevents negative/understated equity when USDC is borrowed against non-USDC collateral in `portfolioMargin` (`0xb1ec7feb`).
   - Remains resilient to live mark-price ticks between sequential `clearinghouseState` and `spotClearinghouseState` HTTP calls and safely defaults unknown future abstractions to `"perp+spot"`.

3. **Abstraction Caching & API Quota Discipline (`§5.1`, `§6.3`, `§4`)**:
   - `daily` queries `userAbstraction` per candidate and stores each whitelisted address's abstraction mode in `state/daily/whitelist_state.json` (plus `state/daily/addresses/{shard}.json.gz`).
   - In `daily` Stage 2 (`§5.1`), `spotClearinghouseState` (`weight = 2`) is called when `userAbstraction not in {"disabled", "default"}` OR when the address holds any open perp short position (needed for `hedgeRatio`).
   - In `fast` (`§6.3`), `collect.py` reads `state/daily/whitelist_state.json` (read-only, preserving `§4` state isolation) and calls `spotClearinghouseState` (`weight = 2`) **only** when `userAbstraction not in {"disabled", "default"}` (`~77%` of active whales), skipping `disabled`/`default` even if short because `fast` computes `AV` only.

## Consequences
- **`schemas/` (`whitelist.schema.json`, `snapshot.schema.json`, `candidates.schema.json`)**: No `schemaVersion` bump or breaking schema change. `whitelist.json` keeps `metrics.equitySource` enum `["perp", "perp+spot"]` (`§3.2`), and snapshot `series.av` / `latest.json` store the resulting main-perp / shared-margin `equity` in USD.
- **`pipeline/models/` (Phase 1–2)**:
  - Pydantic `SpotClearinghouseState` model MUST accept optional `portfolioMarginEnabled: bool`, optional string decimals `ltv`, `supplied`, `borrowed` on balances (allowing negative `total` and `hold` strings on borrowed USDC), and optional `tokenToAvailableAfterMaintenance: list[tuple[int, str]]` (absent on `disabled`/`default`).
  - `UserAbstraction` parses string values (`"disabled"`, `"default"`, `"unifiedAccount"`, `"portfolioMargin"`, `"dexAbstraction"`), routing unrecognized strings safely to the non-isolated `"perp+spot"` branch with a warning rather than crashing.
- **`pipeline/scoring/metrics.py` (Phase 3)**:
  - Implements `compute_equity(...)` cleanly as a pure function; uses `equity` as `metrics.accountValue` (and as `accountValue` fallback for addresses absent from `leaderboard`, per `§5.4`), never raw `leaderboard.accountValue`.
- **`pipeline/snapshot/` (`collect.py` & `reconstruct.py`, Phase 4)**:
  - `collect.py` (`§6.3`) reuses `compute_equity(...)` during 10-minute snapshots using the cached `abstraction` from `whitelist_state.json`.
  - `reconstruct.py` (`§6.5`) MUST account for the fact that `portfolio.perpAllTime` includes HIP-3 DEXes (`0xfc27136e`, `0xb1ec7feb`) while `portfolio.allTime` includes non-collateralized spot assets (`0x87f9cd15`, `0xdf9ea6ec`) when anchoring historical `AV(t)` curves to live main-perp `equity` (see also Task 0.7 `docs/research/portfolio.md`).
- **`fixtures/`**:
  - `fixtures/api/` already records verified responses covering all observed modes (`0xfc27136e` `disabled` + HIP-3, `0x87f9cd15` `default`, `0xdf9ea6ec` `unifiedAccount` mixed, `0x1f250df5` `unifiedAccount` pure perp, `0xbf732ea0` `portfolioMargin` supplied USDC, `0xb1ec7feb` `portfolioMargin` borrowed negative USDC).
  - Phase 1 `fixtures/site-data/` and Phase 2–3 unit tests MUST include regression cases for: (a) `unifiedAccount` non-double-counting, (b) `unifiedAccount` idle spot / `ch_av == 0`, (c) `portfolioMargin` negative `spot.USDC.total` + `avail_after_maint + c_mm`, (d) missing token `0` in `tokenToAvailableAfterMaintenance`, and (e) `disabled`/`default` ignoring isolated spot USDC.

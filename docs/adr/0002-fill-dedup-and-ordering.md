<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# ADR 0002 — Fill Deduplication, Main-Perp Universe Filter, Liquidation Attribution & HFT Threshold

- **Date:** `2026-11-15`
- **Status:** `Accepted` (Decisions 1–5; Decision 5 approved via Proposal 0001 on 2026-11-15)
- **Empirical Evidence:** [`docs/research/fill-dir.md`](../research/fill-dir.md), [`docs/research/reconstruct-check.md`](../research/reconstruct-check.md)
- **Related Specification:** `docs/implementation_plan.md` `§5.2`, `§5.3`, `§5.5`, `§3.4`

---

## Context
Phase 0.4 (`94,131` fills across `50` Stage 1 candidates + `20`-address `aggregateByTime` benchmark + `3`-address overlap boundary checks) and Phase 0.11 (`5`-address, `51`-coin position reconstruction check) tested the assumptions behind `§5.2`–`§5.5`:
1. Whether `(time, tid, hash, coin, dir)` uniquely deduplicates overlapping `userFillsByTime` pages.
2. Whether `":"`, `"@"`, `"PURR/USDC"` string checks alone isolate main perp DEX fills.
3. How liquidation and settlement events appear in `userFillsByTime` payloads.
4. Whether `hft.fillsPerDay: 500` aligns with `aggregateByTime: true`.
5. Whether `(time, tid)` accurately orders intra-millisecond fills.

---

## Decisions

### 1. Overlap Pagination & Composite Deduplication Key (`Accepted`)
- **Pagination:** Call `userFillsByTime(startTime=cursor, aggregateByTime=true)` inclusively (`cursor = max(0, last_t - overlapMs)` with `overlapMs = 60_000` between pages and checkpoints), consuming pages in ascending `time` order until `len(page) < 2000` or the API's rolling history cutoff is reached.
- **Dedup key:** Deduplicate per address using composite key `(time, tid, hash, coin, dir)`, retaining the **first occurrence** in API page-arrival sequence (`0` collisions observed across `94,131` fills; exact overlap accounting across `3` boundary tests: `20/1980`, `55/1945`, `151/1849`).
- **Cross-run & checkpoint deduplication (`state/daily/addresses/{shard}.json.gz`):** While public `fills/` overwrites millisecond buckets idempotently, `openTrades`, `closedTrades`, and `fillStats.days` in `state/daily/addresses/{shard}.json.gz` are stateful accumulators and raw candidate fills must never be persisted (`AGENTS.md` Hard Invariant #4). Therefore, `state/daily/addresses/{shard}.json.gz` MUST persist `recentScoringFillKeys: list[tuple[int, int, str, str, str]]` holding only already-applied `(time, tid, hash, coin, dir)` keys inside the active overlap window `t >= fillsCursorScoring - overlapMs` (pruned whenever `fillsCursorScoring` advances). On incremental daily runs or `--budget-minutes` checkpoint resumes starting at `max(0, fillsCursorScoring - overlapMs)`, keys present in `recentScoringFillKeys` are skipped before `trades.py` and `fillStats` accumulation, preserving exact incremental-vs-batch equivalence (`Phase 3.2`).
- **Boundary livelock guard (`§11.2` autonomous boundary clarification):** If a page of length `2,000` yields `0` new dedup keys under `last_t - overlapMs` (i.e., `>= 2,000` fills inside `60 s`), retry once at `startTime = last_t` (without subtracting `overlapMs`); if `2,000` identical-millisecond fills still saturate a single `last_t`, advance to `last_t + 1` and emit a `runs.jsonl` warning so pagination never enters an infinite loop.

### 2. Main Perp DEX Universe Filter (`Accepted`)
- A fill is accepted into main perp DEX scoring (`§5.2`) and public `fills/` (`§3.4`) **only if**:
  1. `coin in main_perp_universe`, where `main_perp_universe` is loaded from `meta.universe` (**including `"isDelisted": true` markets** so historical fills retain valid `szDecimals` quantization), **AND**
  2. Explicit guard: `"#"` not in `coin`, `":"` not in `coin`, not `coin.startswith("@")`, `"/"` not in `coin`, and `coin != "PURR/USDC"`.
- **Rationale:** Phase 0.4 caught `103` `#...` outcome/prediction market fills (`#12270`, `#301230`, carrying `Buy: 83`, `Merge Outcome: 18`, `Sell: 1`, `Settlement: 1`) leaking past a naive `: / @` string check, while `spotMeta.universe` had `0` pairs lacking `@` or `/`. Under the strict universe filter, main-perp fills exhibit exclusively `{Open Long, Close Long, Open Short, Close Short, Long > Short, Short > Long}`.

### 3. Liquidation Detection & Counterparty Attribution (`Accepted`)
- **Observed wire shape:** Main-perp liquidation fills do **not** emit a distinct `"Liquidated ..."` `dir` string; they carry standard `dir` values plus a nested `"liquidation": {"liquidatedUser": str, "markPx": str, "method": "market"}` object.
- **Self-liquidation vs maker counterparty:** Maker fills (`crossed: false`) matching against another trader's forced liquidation also carry `"liquidation"`, with `liquidation.liquidatedUser != user` (observed in all `11` liquidation-annotated samples in Phase 0.4 / fixtures, e.g., `fixtures/api/userFillsByTime_0xbf732ea0.json:L363-L384`).
- **Rule:**
  - Detect `liq_obj = fill.get("liquidation")`.
  - Set `liq = 1` (`True`) **only when** `liq_obj is not None` **and** `liq_obj.get("liquidatedUser", "").lower() == user_address.lower()` (the tracked address itself was liquidated).
  - Keep `liq = 0` (`False`) when `liq_obj` belongs to a counterparty (`liquidatedUser != user_address`), preventing resting liquidity providers from being mislabeled as liquidated on `#2` fill markers or merged into `liq = 1` rows under `§3.4` `(time, addrId, dirCode, liq)` aggregation.
  - Map `dirCode` from `fill.dir` (`OL`, `CL`, `OS`, `CS`, or `Long > Short` / `Short > Long` split); only if `liq_obj` ever appears with an unexpected non-standard `dir`, fall back to deriving `CL` / `CS` from `startPosition` + `side` (`§5.2`) and log a `runs.jsonl` warning.

### 4. Retain `hft.fillsPerDay: 500` Default Under `aggregateByTime: true` (`Accepted`)
- Across `20` addresses (`docs/research/fill-dir.md`), `raw / aggregated` fill ratios had `min = 1.0303`, `median = 1.2511`, `mean = 2.7815`, `max = 16.0932`.
- Because the median compression is only `1.25x` while algorithmic splitters (`7.24x–16.09x`) are still cleanly separated by `fillsPerDay`, `medianHoldMinutes < 5`, and `mm`, retain `hft.fillsPerDay: 500` (`aggregateByTime: true`) in `config/pipeline.yaml`.

### 5. Intra-Millisecond Ordering (`Accepted via Proposal 0001`)
- Phase 0.4 (`0x6ecbb68c` `FARTCOIN`, `0xbf732ea0` `BTC`) and Phase 0.11 (`3 / 5` addresses failing under `(time, tid)`: `+400.0 AVAX`, `-99.85 SOL`, `-500.0 ETH`, vs `0.0` error across `51 / 51` coins under API sequence order) prove that `tid` is non-monotonic within a millisecond.
- Because `§5.2` and `§5.3` are marked **【規範】**, replacing `(time, tid)` with API execution sequence `(time, api_seq_index)` was approved by user via [`docs/proposals/0001-intra-ms-fill-ordering.md`](../proposals/0001-intra-ms-fill-ordering.md) (`2026-11-15`, Option B) per `§11.2`.
- When `§5.2` splits a `Long > Short` or `Short > Long` reversal fill into two normalized legs (`CL` + `OS` or `CS` + `OL`), both legs inherit `(time, api_seq_index)` and MUST carry `leg_idx` (`0` for the closing leg `CL`/`CS`, `1` for the opening leg `OS`/`OL`), so `§5.3` sorts normalized fills by `(time, api_seq_index, leg_idx)` and always closes the prior trade (`position == 0`) before opening the reversed trade.

---

## Consequences & Verification Plan
- `pipeline/` unit and property tests (Phases 2–3) MUST cover:
  - Inclusive boundary pagination + `(time, tid, hash, coin, dir)` deduplication + `recentScoringFillKeys` cross-run/checkpoint idempotence (incremental-vs-batch equivalence, `Phase 3.2`) + same-60s / same-ms saturation fallback.
  - Rejection of `:` (HIP-3), `@` / `/` / `"PURR/USDC"` (Spot), and `#` (Outcome/Prediction) coins, plus `szDecimals` resolution for `isDelisted: true` main-perp coins.
  - `liq = 1` solely for `liquidatedUser == user`, and `liq = 0` for maker counterparty fills (`liquidatedUser != user`).
  - Once Proposal `0001` is approved, chronological reconstruction using `(time, api_seq_index, leg_idx)` with intra-ms `startPosition` chain assertions and flip-split leg ordering (`CL`/`CS` before `OS`/`OL`).

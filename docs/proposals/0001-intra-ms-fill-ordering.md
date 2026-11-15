<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Proposal 0001 — Replace `(time, tid)` Intra-Millisecond Fill Ordering with API Execution Sequence Order

- **Proposal:** `0001`
- **Date:** `2026-11-15`
- **Status:** `Accepted (2026-11-15, Option B)`
- **Affects:** `docs/implementation_plan.md` `§5.2` (L479) and `§5.3` (L487) **【規範】**
- **Empirical Evidence:** [`docs/research/fill-dir.md`](../research/fill-dir.md), [`docs/research/reconstruct-check.md`](../research/reconstruct-check.md)
- **Related ADR:** [`docs/adr/0002-fill-dedup-and-ordering.md`](../adr/0002-fill-dedup-and-ordering.md)

---

## 1. Problem Statement

`docs/implementation_plan.md` currently mandates sorting fills by `(time, tid)` in two **【規範】** clauses:
- **`§5.2` (L479):** `若同毫秒內多筆 fill 跨頁，以去重鍵（於 Phase 0.4 實測確認唯一性，預設 (time, tid, hash, coin, dir)）去重後，依 (time, tid) 遞增排序。`
- **`§5.3` (L487):** `依 (address, coin) 按 (time, tid) 排序處理正規化後的 fills。`

This rule assumed Hyperliquid's `tid` field increases monotonically in chronological execution order within a single millisecond (`time`). Live API recordings and multi-address reconstruction checks prove that assumption false.

---

## 2. Empirical Evidence

### 2.1 `tid` Is Non-Monotonic Within the Same Millisecond (Phase 0.4 & Fixtures)
`tid` is a 50-bit hash-derived trade identifier, not an increasing sequence counter. Real fixtures contain frequent same-millisecond `tid` inversions where `startPosition` strictly tracks the API return order while `tid` moves backward or oscillates:

1. **Intra-ms `Short > Long` reversal** — [`fixtures/api/userFillsByTime_0x6ecbb68c.json` (L1262–L1315)](../../fixtures/api/userFillsByTime_0x6ecbb68c.json#L1262-L1315) (`FARTCOIN`, `t = 1793861593746`):
   - API seq 70 (`L1262–L1279`): `dir = "Close Short"`, `sz = "4553.0"`, `startPosition = "-7601.0"` → post-fill `-3048.0`, **`tid = 943606857324540`**
   - API seq 71 (`L1280–L1297`): `dir = "Close Short"`, `sz = "1601.0"`, `startPosition = "-3048.0"` → post-fill `-1447.0`, **`tid = 876088275547155`**
   - API seq 72 (`L1298–L1315`): `dir = "Short > Long"`, `sz = "1961.0"`, `startPosition = "-1447.0"` → post-fill `+514.0`, **`tid = 40542812849106`**

   Sorting by `(time, tid)` moves the `Short > Long` flip (`tid = 40542812849106`) **before** the two `Close Short` fills, breaking `§5.2` flip splitting and `§5.3` round-trip trade boundaries (`nTrades`, `openTime`/`closeTime`, `netPnl`, `medianHoldMinutes`).

2. **Intra-ms open chain** — [`fixtures/api/userFillsByTime_0xbf732ea0.json` (L2–L58)](../../fixtures/api/userFillsByTime_0xbf732ea0.json#L2-L58) (`BTC`, `t = 1793976031201`):
   - API seq 0 (`L2–L20`): `startPosition = "10.0"`, `sz = "10.0"`, `tid = 58951651081237`
   - API seq 1 (`L21–L39`): `startPosition = "20.0"`, `sz = "10.0"`, `tid = 980152391812683`
   - API seq 2 (`L40–L58`): `startPosition = "30.0"`, `sz = "10.0"`, `tid = 490977211903399`

   API arrival order chains `10.0 -> 20.0 -> 30.0 -> 40.0`, while `tid` jumps `58.95T -> 980.15T -> 490.98T`.

### 2.2 Position Reconstruction Fails on `3 / 5` Real Addresses Under `(time, tid)` (Phase 0.11)
Comparing `initial startPosition + Σ fills` against `clearinghouseState.szi` (with `actual_before == actual_after`, `mid_fetch_changed == false`) across `5` addresses (`51` coins with fills) in [`docs/research/reconstruct-check.md`](../research/reconstruct-check.md):

| Ordering Method | Passing Coins | Failing Addresses | Observed Errors |
|---|---:|---:|---|
| **`(time, tid)` ascending** *(current spec)* | `48 / 51` | **`3 / 5`** | `0xfc27136e` `AVAX`: `2046.95` vs `1646.95` (**`+400.0`**)<br>`0xfc667adb` `SOL`: `-1841.54` vs `-1741.69` (**`-99.85`**)<br>`0xbf732ea0` `ETH`: `-500.0` vs `0.0` (**`-500.0`**) |
| **`(time, api_seq_index)`** *(API execution order)* | **`51 / 51 (100%)`** | **`0 / 5`** | **`diff_api == 0.0` (`0.00000000`) on all 51 coins** |

---

## 3. Options Considered

| Option | Description | Pros | Cons / Verdict |
|---|---|---|---|
| **Option A: Keep `(time, tid)`** | Retain existing `§5.2` / `§5.3` sort by `(time, tid)`. | No spec edit. | **Rejected.** Provably wrong (`3/5` real addresses fail `szi` check by up to `500 ETH` / `400 AVAX`; corrupts intra-ms flips and `§5.3` round trips). |
| **Option B: Preserve API execution order `(time, api_seq_index)` after dedup + optional `startPosition` chain check** | Deduplicate overlapping pages in chronological page-arrival order (retaining first occurrence of `(time, tid, hash, coin, dir)` and assigning monotonically increasing `api_seq_index`), then sort by `(time, api_seq_index)`. Optionally assert/tie-break intra-ms `startPosition` chaining. | **Recommended.** Exact `0.0` error on `51/51` real coins; `O(N)` deterministic across `overlapMs = 60_000` re-queries (overlapping pages return identical same-ms sequences); immune to Eulerian cycles. | Requires `§11.2` proposal to update two `【規範】` lines (`§5.2`, `§5.3`). |
| **Option C: Sort solely by `startPosition` chaining within each millisecond** | Build a directed graph connecting `prev.startPosition + prev.delta == next.startPosition` inside each `(coin, time)` bucket. | Independent of API order when all steps visit unique `startPosition` values. | **Not recommended as primary sort.** Ambiguous when an account revisits the same `startPosition` inside 1 ms (e.g., `0 -> +10 -> 0 -> +10 -> 0` creates an Eulerian cycle with multiple valid paths). Keep `startPosition` chaining solely as an invariant assertion / fallback diagnostic on top of Option B. |

---

## 4. Exact Proposed Specification Diff

Apply **Option B** via the following 2-line change to [`docs/implementation_plan.md`](../implementation_plan.md):

```diff
--- a/docs/implementation_plan.md
+++ b/docs/implementation_plan.md
@@ -476,15 +476,15 @@
 ### 5.2 Fill 取得、去重與正規化
 - **【規範】分頁與去重規則**：
   - 呼叫 `userFillsByTime(startTime=cursor, aggregateByTime=true)` 時，**包含邊界時間戳**（用 `cursor` 而非 `cursor + 1`，若尚無歷史成交則從 `0` 起抓，並與前次檢查點保留 `overlapMs = 60_000` 重疊窗），直到單頁回傳筆數 `< 2,000` 或觸及官方 10,000 筆上限。
-  - 若同毫秒內多筆 fill 跨頁，以去重鍵（於 Phase 0.4 實測確認唯一性，預設 `(time, tid, hash, coin, dir)`）去重後，依 `(time, tid)` 遞增排序。
+  - 若同毫秒內多筆 fill 跨頁，以去重鍵（於 Phase 0.4 實測確認唯一性，預設 `(time, tid, hash, coin, dir)`）依首次出現順序去重後，依 API 回傳執行順序 `(time, api_seq_index)` 遞增排序（可以同毫秒 `startPosition` 鏈接作一致性檢驗）。
 - 只處理主 perp dex（`coin` 不含 `:` 且不以 `@` 開頭、不是 `PURR/USDC`）。
 ...
 ### 5.3 交易重建（round trip）
-- 依 `(address, coin)` 按 `(time, tid)` 排序處理正規化後的 fills。
+- 依 `(address, coin)` 按 `(time, api_seq_index, leg_idx)`（`time` 遞增、同毫秒保留 API 執行順序；翻倉拆單時先平倉腿 `leg_idx = 0` 後開倉腿 `leg_idx = 1`）排序處理正規化後的 fills。
```

### Non-Impact Confirmation
- **`schemaVersion` does NOT change** (`schemaVersion: 1` unchanged; `fills/{coin}/` tuples `[time, addrId, dirCode, px, sz, notional, liq]` are aggregated by `(time, addrId, dirCode, liq)` and already keyed on `time`).
- **No formulas in `§5.4`–`§5.6` or `§7` change.** Only the intra-millisecond input ordering prior to flip splitting (`§5.2`), round-trip reconstruction (`§5.3`), and snapshot backfill (`§6.5`) is corrected.

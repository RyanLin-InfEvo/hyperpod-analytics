<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Proposal 0002 — Normalize `§5.4 maxDrawdown` Across Capital Scaling & Eliminate the `(AV=0, PnL=0)` Blind Spot

- **Proposal:** `0002`
- **Date:** `2026-11-19`
- **Status:** `Accepted (2026-11-19, Option A1)`
- **Affects:** [`docs/implementation_plan.md`](../implementation_plan.md#L502) `§5.4` (`maxDrawdown`, L502) **【規範】**
- **Empirical Evidence:** [`docs/research/portfolio.md`](../research/portfolio.md), [`docs/research/first-whitelist.md`](../research/first-whitelist.md), [`fixtures/api/portfolio_*.json`](../../fixtures/api/)

---

## 1. Problem Statement

[`docs/implementation_plan.md`](../implementation_plan.md#L502) `§5.4` currently specifies:

> 取 `perpAllTime.pnlHistory` 與 `accountValueHistory` 依時間戳對齊；`DD_t = (max_{s≤t} PnL_s − PnL_t) / AV_{argmax}`（分母 `≤ 0` 略過該點）；無有效點時為 `0.0`，結果下限 `0.0`

Empirical evaluation across Phase 0 fixtures and `60` live Stage 1 candidates proves that this definition suffers from **three structural failure modes**:

### 1.1 Opening `(AV=0, PnL=0)` Blind Spot (Day-1 Loss Miss)
Every Hyperliquid `perpAllTime` history starts at index `0` with `[t_0, AV=0.0, PnL=0.0]`.
- If an account experiences cumulative net losses immediately from inception (`PnL_t < 0` for subsequent samples, never reclaiming `PnL > 0`), `argmax_{s≤t} PnL_s` stays locked at index `0` where **`AV_{argmax} = 0.0`**.
- Because `AV_{argmax} <= 0` skips every subsequent point, **massive initial blowups produce `maxDrawdown = 0.0000`**:
  - [`fixtures/api/portfolio_0x1f250df5.json`](../../fixtures/api/portfolio_0x1f250df5.json): Starts at `[0] (AV=0, PnL=0)`, deploys `~$29.85M` at `[1] (AV=$27,987,462.72, PnL=-$1,860,635.28)`, and draws down to `-$32,201,064.79` PnL at `[3] (AV=$6,983,741.29)` — yet `orig_mdd = 0.0000`.
  - Live `0xae6446c95cc4e455e9cf56f28757ab728d88e81e`: Starts `[0] (AV=0.00, PnL=0.00)` → `[1] (AV=0.00, PnL=-23.34)` → `[3] (AV=93.81, PnL=-23.34)` — `orig_mdd = 0.0000`.

### 1.2 Capital Scale-Up Distortion (Early Test Deposit / Post-Withdrawal Dust)
Fixing the drawdown denominator statically at `AV_{argmax}` causes normal trading volatility after a capital scale-up to be divided by an obsolete tiny balance:
- **`0xfc27136e42af1732ddc9ce2605ea9bff1b959d9d`:** Early post-withdrawal dust `AV_{argmax} = $1.42` (`PnL = +$5,815.13`) followed by a `-$23,010` swing after depositing `~$57k` produces `orig_mdd = 16,205.46` (`1,620,546%`), vs true post-peak drawdown `0.3844` (`38.44%`).
- **`0x7fdafde5cfb5465924316eced2d3715494c517d1`:** Early test peak `AV_{argmax} = $1,377.15` (`PnL = +$54.02`) followed by `>$10M` deposits and a `-$4.22M` drawdown produces `orig_mdd = 3,060.80`, vs `0.5011` (`50.11%`).
- **`0x6355f7cf36b24044cd5b089a845113327d0ee58e`:** Early `AV_{argmax} = $8.23` followed by `$1M–$6M` scaling yields `orig_mdd = 2,222.26`.
- **`0x84abc08c0ea62e687c370154de1f38ea462f4d37`:** Early `AV_{argmax} = $109.48` followed by `$4.99M` deposit yields `orig_mdd = 4,634.03`.
- Across `60` live Stage 1 candidates (`>= $1M` current AV), **`43 / 60` (`71.7%`) recorded `orig_mdd > 1.0` (`> 100%`)**, and only `9 / 60` passed `mdd <= 0.45`.

### 1.3 Why Naive Single-Point `max(AV_{argmax}, AV_t + dd_usd)` Fails on Full Withdrawals
Replacing the denominator with the single-point expression `denom_t = max(AV_{argmax}, AV_t + dd_usd)` (where `dd_usd = max(0, peak_pnl - PnL_t)`) solves the scale-up case **only while funds remain in the account**.
- If a trader starts small → deposits millions → incurs a modest `20%` loss → later withdraws all remaining funds (`AV_t -> 0`), then at the post-withdrawal timestamp `AV_t + dd_usd = 0 + dd_usd = dd_usd`, forcing **`DD_t = dd_usd / dd_usd = 1.0000` (a false 100% blowup!)**.
- **Verified live on `0x6355f7cf36b24044cd5b089a845113327d0ee58e`:** After earning `$1,713,125.07` (`AV=$0.01` residual at peak), a `$0.01` tick to `$1,713,125.06` at `[45] (AV=0.00)` yields **`single_pt = 1.0000`** (`100%` false blowup), whereas remembering the `$55.81` / `$6.00M` effective capital watermark yields **`path_peak_cap = 0.1847` (`18.47%`)**.

---

## 2. Mathematical Formulation & Theoretical Limits

### 2.1 Candidate Fix A: Running Path-Maximum Effective Capital (`peak_cap_t`)
From [`docs/research/portfolio.md`](../research/portfolio.md), external deposits/withdrawals shift `AV_t` without changing cumulative `PnL_t`, obeying `AV_t = NetPrincipal_t + PnL_t`. For any timestamp `u >= argmax` during an active drawdown (`peak_pnl - PnL_u >= 0`), had zero drawdown occurred from `peak_pnl`, the capital deployed at `u` would be:
$$\mathrm{eff\_cap}_u = \max(\mathrm{AV}_u, 0) + (\mathrm{peak\_pnl} - \mathrm{PnL}_u)$$
Tracking the **running maximum since `argmax`**:
$$\mathrm{peak\_cap}_t = \max_{\mathrm{argmax} \le u \le t} \Big(\max(\mathrm{AV}_u, 0) + (\mathrm{peak\_pnl} - \mathrm{PnL}_u)\Big), \qquad DD_t = \frac{\mathrm{peak\_pnl} - \mathrm{PnL}_t}{\mathrm{peak\_cap}_t}$$
- **Peak updates:** A new PnL peak updates `(peak_pnl, peak_cap)` **only** when `AV_t > 0 and PnL_t > peak_pnl` (setting `peak_cap = AV_t`), preventing `(AV=0, PnL=0)` or zeroed-out balances from becoming `argmax`.
- **Upper bound guarantee:** Since `peak_cap_t >= eff_cap_t = max(AV_t, 0) + (peak_pnl - PnL_t) >= peak_pnl - PnL_t >= 0`, `DD_t` is mathematically bounded to **`[0.0, 1.0]`** for all non-negative equity paths.
- **Full withdrawal immunity:** Withdrawing remaining capital (`AV_t -> 0`) after a `20%` loss cannot shrink `peak_cap_t`, so `DD_t` remains `0.20` rather than jumping to `1.00`.
- **Two variants for an initial `(AV <= 0, PnL == 0)` prefix followed by day-1 losses (`AV_k > 0, PnL_k < 0`):**
  - **Option A1 (`init_zero_baseline = True`, Recommended):** If an opening `AV <= 0, PnL == 0` sample precedes the first funded point `(AV_k > 0, PnL_k < 0)`, anchor the pre-funding origin at `peak_pnl = 0.0` and `peak_cap = AV_k + (-PnL_k)`, immediately capturing day-1 losses (e.g., `0x1f250df5 -> 0.9567`, `0xae6446c9 -> 0.9999`, `0x87f9cd15 -> 0.0187`).
  - **Option A2 (`init_zero_baseline = False`):** Ignore all points prior to the first `AV_k > 0` completely (`0x1f250df5 -> 0.9547` from `[1]` onward, `0x87f9cd15 -> 0.0000`, `0xae6446c9 -> 0.0000` because `0xae6446c9`'s `$23.34` loss occurred entirely before its first `AV > 0` sample).

### 2.2 Alternative Fix B: Chained Time-Weighted Return (Modified Dietz TWR)
Given consecutive aligned points `(AV_{k-1}, PnL_{k-1})` and `(AV_k, PnL_k)`:
$$\Delta\mathrm{PnL}_k = \mathrm{PnL}_k - \mathrm{PnL}_{k-1}, \quad CF_k = (\mathrm{AV}_k - \mathrm{AV}_{k-1}) - \Delta\mathrm{PnL}_k, \quad r_k = \frac{\Delta\mathrm{PnL}_k}{\mathrm{AV}_{k-1} + 0.5 \cdot CF_k}$$
Chaining `I_k = I_{k-1} (1 + r_k)` produces a cashflow-invariant return index, **but suffers two empirical defects on Hyperliquid `perpAllTime`**:
1. **Negative / Zero Dietz Denominators (`AV_{k-1} + 0.5 * CF_k <= 0`):** Whenever a profitable trader withdraws profits exceeding `2 * AV_{k-1}` within one ~7–14 day bucket, the Modified Dietz denominator turns **negative** or zero (undefined/inverted return sign!). Observed verbatim on **`24 / 60` (`40.0%`) live accounts (`244` intervals total)**, including `50` intervals on `0x1f250df5`, `27` on `0x6355f7cf`, and `10` on `0x01535410`.
2. **Midpoint Weighting (`w = 0.5`) Deposit Bias:** When an account deposits `$10M` near the beginning of a weekly bucket and loses `20%` (`-$2M`) in that same bucket, Modified Dietz uses denominator `$100 + 0.5 * $10M = $5M`, reporting a **`40%` loss instead of `20%`** (`Small test -> deposit $10M & 20% loss same bucket`: `path_peak_cap = 0.2000`, `twr_dietz = 0.4000`; `0xfc27136e`: `pc0 = 0.3844` vs `twr = 0.7284`; `0x7fdafde5`: `pc0 = 0.5011` vs `twr = 0.8626`).

### 2.3 Theoretical Limit of Coarse Weekly Sampling (`perpAllTime`)
Because `perpAllTime` uses ~7-day (`167h`) to ~14-day (`335h`) buckets for historical accounts (Phase 0.7):
- **Separate buckets (consecutive weeks):** If a trader blows up `90%` (`$1M -> $100k`) in bucket `k` and tops up `+$10M` (`-> $10.1M`) in bucket `k+1`, **`path_peak_cap` captures `DD_k = 0.9000` cleanly** before the top-up occurs, and retains `0.9000` even after the `+$10M` deposit.
- **Same unsplit bucket (intra-week blowup + top-up):** If a trader loses `-$900k` (`-90%` of `$1M`) **and** deposits `+$10M` before the next `perpAllTime` snapshot (`[$1M, +$50k] -> [$10.1M, -$850k]`), **no path-level or Modified Dietz formula can distinguish that sequence from depositing `+$10M` first and losing `8.18%`** (`orig` appears `0.90` only because it mistakenly froze at `$1M`, whereas `pc0 = 0.0818` and `twr = 0.1500`). *(Note: splicing `perpMonth`/`perpWeek`/`perpDay` reduces recent bucket width to ~`2h–14h`, while historical `>30d` data remains bounded by `perpAllTime` resolution.)*

---

## 3. Empirical Before/After Evidence

### 3.1 Synthetic Edge Cases (Executed Directly Against All 4 Formulas)

| Synthetic Scenario | `orig` | `single_pt` | `path_peak_cap` (`A1`) | `twr_dietz` (`B`) | Notes |
|---|---:|---:|---:|---:|---|
| Opening `(0,0)` + day-1 loss (`AV: 0→1000→500, PnL: 0→-100→-600`, `$1,100` dep.) | **`0.0000`** *(miss)* | `0.5000` | **`0.5455`** | `0.5000` | Fixes `(0,0)` & `[0→1]` `-$100` loss |
| Small `$100` → deposit `$10M` → `20%` loss → full withdrawal (`AV=0`) | `20,000.0` | **`1.0000`** *(false blowup)* | **`0.2000`** | `0.2000` | Fixes withdrawal-to-0 trap |
| Small `$100` → deposit `$10M` + `20%` loss in same bucket → `AV=0` | `20,000.0` | **`1.0000`** *(false blowup)* | **`0.2000`** | `0.4000` *(2x bias)* | Avoids Dietz `w=0.5` bias |
| Real `90%` blowup (`$1M→$100k`) → next-week `$10M` top-up | `0.9000` | `0.9000` | **`0.9000`** | `0.9000` | Preserves sequential blowup |
| Same-bucket `90%` blowup (`$1M→$100k`) + `$10M` top-up *(sampling limit)* | `0.9000` | `0.0818` | `0.0818` | `0.1500` | Coarse `perpAllTime` limit |
| High profit + profit withdrawal (`AV: 100k→50k→40k, PnL: 0→500k→490k`) | `0.2000` | `0.2000` | `0.2000` | `0.2000` (`neg_denom=1`) | Dietz denom turned negative |

### 3.2 Ten Non-Vault Phase 0 API Fixtures (`fixtures/api/portfolio_*.json`)

| Fixture Address | `orig` | `single_pt` | `path_peak_cap` (`A1`) | `twr_dietz` (`B`) | Dietz `neg_denom` |
|---|---:|---:|---:|---:|---:|
| `0x023a3d05` (`Auros`) | `1.4151` | `0.6020` | **`0.5280`** | `0.6964` | `0` |
| `0x1f250df5` | **`0.0000`** *( -$32.2M loss!)* | `1.0000` | **`0.9567`** | `1.0000` | **`50`** |
| `0x6ecbb68c` | `0.3707` | `0.2643` | **`0.2643`** | `0.3073` | `0` |
| `0x7fdafde5` (`BobbyBigSize`) | `3,060.7997` | `0.6051` | **`0.5011`** | `0.8626` | `0` |
| `0x87f9cd15` | `0.0000` | `0.0000` | **`0.0187`** | `0.0000` | `0` *(opening -$26.79 / $1,431)* |
| `0xb1ec7feb` | `3.2754` | `1.0000` | **`0.9772`** | `1.0000` | `0` |
| `0xbf732ea0` | `2.8694` | `1.0000` | **`0.9898`** | `1.0000` | `6` |
| `0xdf9ea6ec` | `2.9673` | `0.9773` | **`0.7993`** | `0.9974` | `1` |
| `0xf5d81a13` | `0.3271` | `0.2721` | **`0.2694`** | `0.2913` | `0` |
| `0xfc27136e` | `16,205.4610` | `0.4328` | **`0.3844`** | `0.7284` | `0` |

### 3.3 Sixty-Address Live Stage 1 Portfolio Benchmark (`2026-11-19`)

Sampled `60` live Stage 1 candidates (10 anchor whales + top 20 volume candidates + 30 swing candidates with `1.0 <= turnover <= 40.0` and `pnlMonth > 0`):

| Metric Across `n = 60` Live Accounts | `orig` | `single_pt` | `path_peak_cap` (`A1`) | `path_peak_cap` (`A2`) | `twr_dietz` (`B`) |
|---|---:|---:|---:|---:|---:|
| **`mdd <= 0.45` (passes MDD gate)** | `9 / 60` (`15.0%`) | `18 / 60` (`30.0%`) | **`22 / 60` (`36.7%`)** | `23 / 60` (`38.3%`) | `13 / 60` (`21.7%`) |
| **`mdd > 1.0` (`> 100%` impossible)** | **`43 / 60` (`71.7%`)** | `0 / 60` (`0%`) | **`0 / 60` (`0%`)** | `0 / 60` (`0%`) | `0 / 60` (`0%`) |
| **`(0,0)` blind-spot accounts caught** | `0` (`0xae6446c9` missed as `0.0000`) | `0` (`0xae6446c9` missed as `0.0000`) | **`1`** (`0xae6446c9` → `0.9999`) | `0` (`0xae6446c9` missed as `0.0000`) | `0` (`0xae6446c9` = `0.0000`) |
| **Dietz negative-denom accounts / intervals** | N/A | N/A | **`0` / `0`** | `0` / `0` | **`24 / 60`** (`244` intervals) |

Notable live anchor accounts:
- `0x6355f7cf36b24044cd5b089a845113327d0ee58e`: `orig=2,222.26`, `single_pt=1.0000` (withdrawal trap), **`A1=0.1847`**, `twr=0.3899` (`27` neg denoms).
- `0x856c35038594767646266bc7fd68dc26480e910d`: `orig=1.3885`, `single_pt=0.8831`, **`A1=0.4109`**, `twr=0.7842`.
- `0xec85bc052ec5b6d6cb2b40f5caa45e39b61a77fe`: `orig=72.990`, **`A1=0.104`**, `twr=0.132`.
- `0xfb941d2344b72ea9101400392b818173772d0ddf`: `orig=1.8565`, **`A1=0.9862`** *(still fails `mdd > 0.45` because `$1.48M -> <$30k` happened while actually trading with `$1.48M`, prior to later scaling!)*
- `0xe9ec1d90652e13521623fb048f1c77883c597859`: `orig=0.3829`, **`A1=0.1401`**, `A2=0.1078`, `twr=0.1484` *(passes under all formulas)*.

### 3.4 Full Stage 2 Before/After Check (`15` Swing Candidates)
Among `15` evaluated swing accounts (`turnover <= 45x`) with `mdd <= 0.45` under `orig` or `A1`:
- **`6` accounts that previously failed `mdd` due to scale-up (`0x60a8c761`, `0x856c3503`, `0xda3768e9`, `0x9aa58935`, `0xec85bc05`, `0xb829fd12`, `0x3eb6bed6` = `7` accounts including `orig > 0.45`) cleared the `mdd` gate cleanly**, while **all other strict quality gates (`hft`, `mm`, `passive`, `sample`, `pf`, `hedge`, `lucky`) continued filtering out immature or HFT profiles** (in that 15-account slice, `0xe9ec1d90` remained `1` full pass).

---

## 4. Options Considered

| Option | Rule | Verdict |
|---|---|---|
| **Keep current `§5.4`** | Fixed `AV_{argmax}` denominator, `(0,0)` allowed as `argmax` | **Rejected.** Misses day-1 losses (`0.0` on `0x1f250df5` / `0xae6446c9`) and rejects `71.7%` (`43/60`) of live accounts with `> 100%` scale-up artifacts. |
| **Single-point denominator** | `denom_t = max(AV_{argmax}, AV_t + dd_usd)` | **Rejected.** Falsely triggers `DD = 1.0000` whenever funds are withdrawn (`AV_t -> 0`), observed live on `0x6355f7cf`. |
| **Option A1: Path `peak_cap_t` + `(0,0)` baseline** *(Recommended)* | Track `peak_cap_t = max_{argmax <= u <= t}(max(AV_u, 0) + (peak_pnl - PnL_u))`; skip `AV <= 0` peaks, with `(AV <= 0, PnL == 0)` pre-funding origin baseline for day-1 losses | **Recommended.** Bounded in `[0.0, 1.0]`, zero negative-denominator failures, immune to withdrawals-to-zero, catches opening blowups (`0x1f250df5`, `0xae6446c9`), exact match on stable equity, single-pass `O(N)`. |
| **Option A2: Path `peak_cap_t` (ignore all `AV <= 0`)** | Same as A1 except ignore all `AV <= 0` entries before first `AV > 0` without `(0,0)` baseline | Alternative if ultra-small pre-funding fees like `-$23.34` on `0xae6446c9` before initial deposit should be ignored (`0x1f250df5` is still caught as `0.9547` because its `-$32.2M` drop occurred after `[1]`). |
| **Option B: Modified Dietz TWR** | Chain period returns `ΔPnL / (AV_{k-1} + 0.5 CF_k)` | **Not recommended.** Fails/needs heuristics on `40%` (`24/60`) of live accounts due to negative Dietz denominators, and double-counts drawdowns after large intra-week deposits (`w = 0.5`). |

---

## 5. Exact Proposed Specification Diff (`Option A1`)

If **Option A1** is approved, apply the following diff to [`docs/implementation_plan.md`](../implementation_plan.md#L502) (replacing line 502):

```diff
--- a/docs/implementation_plan.md
+++ b/docs/implementation_plan.md
@@ -500,3 +500,3 @@
-| `maxDrawdown` | 取 `perpAllTime.pnlHistory` 與 `accountValueHistory` 依時間戳對齊；`DD_t = (max_{s≤t} PnL_s − PnL_t) / AV_{argmax}`（分母 `≤ 0` 略過該點）；無有效點時為 `0.0`，結果下限 `0.0` |
+| `maxDrawdown` | 取 `perpAllTime.pnlHistory` 與 `accountValueHistory` 依時間戳對齊：僅將 `AV > 0` 之點納為有效新高（若開頭出現 `AV ≤ 0 且 PnL == 0`，其後首個 `AV > 0 且 PnL < 0` 區間以 `peak_pnl = 0`、`peak_cap = AV + (−PnL)` 起算）。自最近有效新高 `argmax` 起沿途追蹤最大有效資金水位 `peak_cap_t = max_{argmax≤u≤t}(max(AV_u, 0) + (peak_pnl − PnL_u))`；`DD_t = (peak_pnl − PnL_t) / peak_cap_t`；無有效點時為 `0.0`，結果夾限於 `[0.0, 1.0]` |
```

### Non-Impact & Sync Confirmation
- **`schemas/*.schema.json` (`schemaVersion: 1`) does NOT change** (`maxDrawdown` remains a non-negative `number` in `whitelist.schema.json` and `candidates.schema.json`).
- **If approved:** `pipeline/src/hyperpod/scoring/metrics.py` (`compute_max_drawdown`), `pipeline/tests/test_metrics.py`, and `docs/metrics.md` (Phase 8) will be updated atomically before continuing to Phase 4, and `docs/adr/0004-*.md` will document the rule refinement cleanly.

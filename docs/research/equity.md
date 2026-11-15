<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Task 0.3 — Account Abstraction Modes & Equity Calculation Verification

## 1. Survey Scope & `userAbstraction` Distribution
Queried `POST https://api.hyperliquid.xyz/info` (`{"type": "userAbstraction", "user": addr}`) across the top **200 Stage 1 candidates** (out of `1,661` passing Stage 1 from the `47,662`-row `leaderboard` snapshot; see [`subaccounts.md`](subaccounts.md)):

| `userAbstraction` | Count | Share | Equity Scope |
|---|---:|---:|---|
| `unifiedAccount` | 111 | 55.5% | Shared spot + perp USDC collateral (`perp+spot`) |
| `portfolioMargin` | 43 | 21.5% | Cross-asset portfolio margin collateral + perp (`perp+spot`) |
| `disabled` | 38 | 19.0% | Isolated perp clearinghouse (`perp`) |
| `default` | 8 | 4.0% | Isolated perp clearinghouse (`perp`) |
| **Total (`unifiedAccount` + `portfolioMargin`)** | **154** | **77.0%** | **Requires `spotClearinghouseState`** |

**Key takeaway:** **77.0%** (`154 / 200`) of Stage 1 candidates use `unifiedAccount` or `portfolioMargin`, making accurate handling of shared spot/perp margin essential for both daily Stage 2 scoring (`§5.1`) and the 10-minute fast loop (`§6.3`).

---

## 2. API Payloads & Verbatim Fixture Excerpts
All probes hit `POST https://api.hyperliquid.xyz/info` (`Content-Type: application/json`):

- `{"type": "userAbstraction", "user": "<address>"}` — returns a JSON string (`"unifiedAccount"`, `"portfolioMargin"`, `"disabled"`, `"default"`, etc.).
- `{"type": "clearinghouseState", "user": "<address>"}` — main perp clearinghouse state (`weight = 2`); HIP-3 builder dexes are queried separately via `{"type": "clearinghouseState", "user": "<address>", "dex": "<dex>"}`.
- `{"type": "spotClearinghouseState", "user": "<address>"}` — spot balances and unified/PM margin availability (`weight = 2`).
- `{"type": "portfolio", "user": "<address>"}` — `day`/`week`/`month`/`allTime` and `perpDay`/`perpWeek`/`perpMonth`/`perpAllTime` histories (`weight = 20`).
- `{"type": "perpDexs"}` — returns `[null, {"name": "xyz", ...}, ...]` (`null` = main perp dex, plus 10 HIP-3 builder dexes: `xyz`, `flx`, `vntl`, `hyna`, `km`, `abcd`, `cash`, `para`, `mkts`, `io`).

### Verbatim Fixture Snippets (`fixtures/api/`)

#### A. `unifiedAccount` — Pure Perp Utilization (`0x1f250df59a777d61cb8bd043c12970f3afe4f925`)
`userAbstraction_0x1f250df5.json`:
```json
"unifiedAccount"
```
`clearinghouseState_0x1f250df5.json` (lines 1–16):
```json
{
  "marginSummary": {
    "accountValue": "6709328.0252320003",
    "totalNtlPos": "64772000.0",
    "totalRawUsd": "-58062671.9747679979",
    "totalMarginUsed": "1619300.0"
  },
  "crossMarginSummary": {
    "accountValue": "6709328.0252320003",
    "totalNtlPos": "64772000.0",
    "totalRawUsd": "-58062671.9747679979",
    "totalMarginUsed": "1619300.0"
  },
  "crossMaintenanceMarginUsed": "809650.0",
  "withdrawable": "232128.025232",
```
`spotClearinghouseState_0x1f250df5.json` (lines 1–17):
```json
{
  "balances": [
    {
      "coin": "USDC",
      "token": 0,
      "total": "6709328.0252327602",
      "hold": "6477200.0",
      "entryNtl": "0.0"
    }
  ],
  "tokenToAvailableAfterMaintenance": [
    [
      0,
      "5899678.0252327602"
    ]
  ]
}
```

#### B. `unifiedAccount` — Mixed Perp + Idle Spot USDC (`0xdf9ea6ec3b7109935ccb4fb267e15ac1fb077ab1`)
`userAbstraction_0xdf9ea6ec.json`: `"unifiedAccount"`

`clearinghouseState_0xdf9ea6ec.json` (lines 1–16):
```json
{
  "marginSummary": {
    "accountValue": "782937.32182",
    "totalNtlPos": "1317596.6587980001",
    "totalRawUsd": "2100533.980618",
    "totalMarginUsed": "131759.665879"
  },
  "crossMarginSummary": {
    "accountValue": "782937.32182",
    "totalNtlPos": "1317596.6587980001",
    "totalRawUsd": "2100533.980618",
    "totalMarginUsed": "131759.665879"
  },
  "crossMaintenanceMarginUsed": "65879.832939",
  "withdrawable": "651177.655941",
```
`spotClearinghouseState_0xdf9ea6ec.json` (lines 1–9, 242–248):
```json
{
  "balances": [
    {
      "coin": "USDC",
      "token": 0,
      "total": "1342369.5245916699",
      "hold": "131820.06591",
      "entryNtl": "0.0"
    },
```
```json
  "tokenToAvailableAfterMaintenance": [
    [
      0,
      "1276459.49163667"
    ]
  ]
}
```

#### C. `portfolioMargin` — Borrowed Negative Spot USDC Against HYPE Collateral (`0xb1ec7febea4f841d525785955b7fe0e812d6e86a`)
`userAbstraction_0xb1ec7feb.json`: `"portfolioMargin"`

`clearinghouseState_0xb1ec7feb.json` (lines 1–16):
```json
{
  "marginSummary": {
    "accountValue": "2847465.45811",
    "totalNtlPos": "17008525.3271640018",
    "totalRawUsd": "-11650630.9767940007",
    "totalMarginUsed": "3228845.4029100002"
  },
  "crossMarginSummary": {
    "accountValue": "2847465.45811",
    "totalNtlPos": "17008525.3271640018",
    "totalRawUsd": "-11650630.9767940007",
    "totalMarginUsed": "3228845.4029100002"
  },
  "crossMaintenanceMarginUsed": "841845.654374",
  "withdrawable": "0.0",
```
`spotClearinghouseState_0xb1ec7feb.json` (lines 1–13, 55–63, 116–124):
```json
{
  "portfolioMarginEnabled": true,
  "balances": [
    {
      "coin": "USDC",
      "token": 0,
      "total": "-847343.4361653794",
      "hold": "-847343.4361653794",
      "entryNtl": "0.0",
      "ltv": "0.0",
      "borrowed": "5327099.0254313899"
    },
```
```json
    {
      "coin": "HYPE",
      "token": 150,
      "total": "123085.45653588",
      "hold": "123085.45653588",
      "entryNtl": "0.0",
      "ltv": "0.65",
      "supplied": "123085.45653588"
    },
```
```json
  "tokenToAvailableAfterMaintenance": [
    [
      0,
      "3894587.6513679898"
    ],
    [
      150,
      "71571.47865267"
    ],
```

---

## 3. Side-by-Side Comparison Across 9 Probed Addresses (Phase 0.3 Sequential Probe)

> Note: This table was captured in a sequential live probe right after the Task 0.2 `fixtures/api/` capture; minor USD differences relative to the fixture files reflect live mark-price ticks between captures.

| Tag (`address`) | `abstraction` | `ch_av` | `c_mm` | Spot USDC `total` | `avail_after_maint + c_mm` | `hip3_av` | `pf_perpAllTime_av` | `pf_allTime_av` | `lb_av` | `computed_equity` | `equitySource` |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `whale_disabled` (`0xfc27136e...`) | `disabled` | 23,102,609.37 | 1,302,171.76 | 0.00 | `null` | 20,420,349.55 | 43,522,958.92 | 44,195,195.87 | 44,010,523.72 | **23,102,609.37** | `perp` |
| `whale_default` (`0x87f9cd15...`) | `default` | 24,060,995.35 | 0.00 | 0.00 | `null` | 0.00 | 24,060,995.35 | 57,079,612.23 | 129,160,945.42 | **24,060,995.35** | `perp` |
| `mm_auros` (`0x023a3d05...`) | `disabled` | 5,735,306.37 | 1,158,850.94 | 1,654,676.93 | `null` | 74,438.28 | 5,809,744.64 | 10,155,380.25 | 16,095,439.38 | **5,735,306.37** | `perp` |
| `hft_top1` (`0xf5d81a13...`) | `disabled` | 1,620,759.04 | 60,121.24 | 2,522.46 | `null` | 0.00 | 1,620,759.04 | 1,655,721.37 | 101,173,078.39 | **1,620,759.04** | `perp` |
| `unified_1` (`0xdf9ea6ec...`) | `unifiedAccount` | 781,093.32 | 65,972.03 | 1,341,247.52 | 1,341,253.42 | 0.00 | 781,211.32 | 1,456,692.10 | 5,821,798.81 | **1,341,253.42** | `perp+spot` |
| `unified_2` (`0x1f250df5...`) | `unifiedAccount` | 6,731,328.03 | 809,925.00 | 6,731,328.03 | 6,731,328.03 | 0.00 | 6,709,328.03 | 6,709,328.03 | 6,720,950.00 | **6,731,328.03** | `perp+spot` |
| `pm_1` (`0xbf732ea0...`) | `portfolioMargin` | 1,119,288.42 | 406,305.00 | 1,687,916.68 | 1,687,916.68 | 0.00 | 1,120,080.42 | 1,754,845.86 | 1,834,434.39 | **1,687,916.68** | `perp+spot` |
| `pm_2` (`0xb1ec7feb...`) | `portfolioMargin` | 2,856,412.35 | 842,258.40 | -837,850.11 | 4,749,502.76 | 1,632,662.20 | 4,489,483.66 | 7,969,684.88 | 8,045,971.44 | **4,749,502.76** | `perp+spot` |
| `master_subs` (`0x7fdafde5...`) | `disabled` | 8,592,580.70 | 1,631,002.73 | 1,051.54 | `null` | 11,198,955.93 | 19,791,139.89 | 20,461,309.87 | 19,986,715.49 | **8,592,580.70** | `perp` |

---

## 4. Mathematical & Semantic Analysis by Abstraction Mode

### 4.1 `disabled` / `default`
- **Isolated margin pockets**: Under `disabled` and `default`, `spotClearinghouseState` omits `tokenToAvailableAfterMaintenance` and `portfolioMarginEnabled`; spot balances do not back main-perp positions. Thus `equity = clearinghouseState.marginSummary.accountValue` (`equitySource = "perp"`).
- **Example (`mm_auros`, `0x023a3d05...`)**: Main perp `ch_av = $5,735,306.37` and isolated `spot.USDC.total = $1,654,676.93` (`hold = $509,064.44` for open spot orders) are completely separate pockets. Per `§1` (main perp scope) and `§5.4`, we use `ch_av` (`$5,735,306.37`) and do not add spot USDC.
- **HIP-3 exclusion (`§1` / `§5.2`) vs `portfolio` / `leaderboard`**: `leaderboard.accountValue`, `portfolio.allTime`, and `portfolio.perpAllTime` include HIP-3 builder perp DEX balances (`xyz`, `io`, `cash`, etc.). On `whale_disabled` (`0xfc27136e...`):
  - Main perp `ch_av` = `$23,102,609.37`
  - HIP-3 `hip3_av` = `$20,420,349.55` (`xyz: $20,420,349.54`, `cash: $0.01`)
  - Sum = `$43,522,958.92` ≡ `pf_perpAllTime_av` (`$43,522,958.92`)
  - Likewise on `mm_auros` (`$5,735,306.37 + $74,438.28 = $5,809,744.64 ≡ pf_perpAllTime_av`) and `master_subs` (`0x7fdafde5`: `$8,592,580.70 + $11,198,955.93 = $19,791,536.63` vs `pf_perpAllTime_av = $19,791,139.89`).
  - Meanwhile on `whale_default` (`0x87f9cd15...`), main perp `ch_av = $24,060,995.35 ≡ pf_perpAllTime_av`, while `pf_allTime_av = $57,079,612.23` and `lb_av = $129,160,945.42` include non-perp/subaccount/spot assets. Because HyperPod tracks **main perp dex only** (`§1`, `§5.2`), HIP-3 equity is intentionally excluded.

### 4.2 `unifiedAccount`
- **Exact identity proof (`unified_2`, `0x1f250df5...`)**:
  - In the Task 0.2 fixtures (`clearinghouseState_0x1f250df5.json` & `spotClearinghouseState_0x1f250df5.json`), BTC did not tick between the two requests, yielding an exact identity to `7.6e-7` USD:
    - `clearinghouseState.marginSummary.accountValue = "6709328.0252320003"`
    - `spotClearinghouseState.balances[USDC].total = "6709328.0252327602"`
    - `tokenToAvailableAfterMaintenance[0] ("5899678.0252327602") + crossMaintenanceMarginUsed ("809650.0") = 6709328.0252327602`
  - The same exact identity holds in the Phase 0.3 sequential probe (`ch_av = 6731328.025232`, `spot.USDC.total = 6731328.02523276`, `avail_after_maint_usdc (5921403.02523276) + c_mm (809925.0) = 6731328.02523276`).
  - **Double-counting hazard**: Naive summation (`ch_av + spot.USDC.total`) would double-count perp equity (`$13.42M` instead of `$6.71M` in the fixture; `$13.46M` instead of `$6.73M` in the Phase 0.3 sequential probe).
- **Idle spot USDC + perp equity (`unified_1`, `0xdf9ea6ec...`)**:
  - In the Phase 0.3 sequential probe: `ch_av = $781,093.32` captures only the perp-allocated collateral + unrealized PnL, whereas `spot.USDC.total = $1,341,247.52` equals idle spot USDC (`~$560,154.20`) plus perp equity (`$781,093.32`). Similarly in the fixture: `ch_av = $782,937.32`, `spot.USDC.total = $1,342,369.52` (`~$559,432.20` idle spot USDC + perp equity).
  - Using `ch_av` alone would understate `0xdf9ea6ec`'s usable USD collateral by `~$560k`; taking `max(ch_av, spot_usdc_total, avail_after_maint_usdc + c_mm)` yields `$1,341,253.42` without double-counting.
  - Note also that when a `unifiedAccount` holds **no open main-perp positions**, `clearinghouseState.marginSummary.accountValue` drops to `0.00` while all USDC collateral remains in `spotClearinghouseState.balances[USDC].total`; using `max(...)` ensures such accounts are not falsely treated as `0.0` equity.
- **Sequential HTTP tick drift (`tick_checks`)**:
  - Probing `0xdf9ea6ec` and `0x1f250df5` twice (`t1`, then `time.sleep(3.0)`, then `t2`, with sequential `clearinghouseState` and `spotClearinghouseState` calls separated by `~0.15s` RTT) gave:
    - `0x1f250df5` (`1000.0 BTC` long): `ch_av` moved `6,683,928.03 → 6,683,328.03` (`delta_ch_av = -$600.00`), while `spot.USDC.total` moved `6,683,928.03 → 6,683,828.03` (`delta_spot_usdc = -$100.00`) — BTC ticked `-$0.60` between `t1` and `t2`, then `+$0.50` in the `~0.15s` between `ch2` and `sp2`.
    - `0xdf9ea6ec` (`-20000.01 HYPE` short): `ch_av` moved `781,413.32 → 782,541.32` (`delta_ch_av = +$1,128.00`), while `spot.USDC.total` moved `1,341,629.52 → 1,342,577.52` (`delta_spot_usdc = +$948.00`).
  - Minor single-digit to hundred-dollar discrepancies between `ch_av`, `spot.USDC.total`, and `avail_after_maint_usdc + c_mm` in sequential REST snapshots are normal live mark-price tick artifacts, not accounting mismatches.

### 4.3 `portfolioMargin`
- **Negative `spot.USDC.total` under collateralized borrowing (`pm_2`, `0xb1ec7feb...`)**:
  - In `spotClearinghouseState_0xb1ec7feb.json`: `balances[USDC].total = "-847343.4361653794"` (`-$837,850.11` in the Phase 0.3 sequential probe) because the account has `"borrowed": "5327099.0254313899"` USDC backed by `"123085.45653588"` supplied `HYPE` (`"ltv": "0.65"`).
  - `ch_av` (`$2,856,412.35` in the Phase 0.3 probe; `$2,847,465.46` in fixture) reflects only perp margin summary, while `spot.USDC.total` is **negative** (`-$837,850.11`).
  - Meanwhile `tokenToAvailableAfterMaintenance[0]` (`"3907244.36107799"` in the Phase 0.3 probe; `"3894587.6513679898"` in fixture) `+ crossMaintenanceMarginUsed` (`"842258.397058"` in the Phase 0.3 probe; `"841845.654374"` in fixture) `= $4,749,502.76` (`$4,736,433.31` in fixture) captures the true haircutted shared collateral + perp equity cleanly without crashing or misreporting on negative `spot.USDC.total`.
- **Supplied USDC without borrow (`pm_1`, `0xbf732ea0...`)**:
  - In `spotClearinghouseState_0xbf732ea0.json`, the account holds `"total": "1687466.6593072"` USDC with `"supplied": "568628.2405182"` (`borrowed` absent).
  - Notice `1687466.6593072 - ch_av (1118838.418789) = 568628.2405182 == supplied` exactly, and `tokenToAvailableAfterMaintenance[0] (1281184.1593072) + crossMaintenanceMarginUsed (406282.5) = 1687466.6593072 == spot.USDC.total` (in the Phase 0.3 sequential probe: `avail_plus_cmm = $1,687,916.68 == spot.USDC.total`).

---

## 5. Confirmed `equity` Formula & Pipeline Quota Impact

### Confirmed Formula (`§5.4`)
Let:
- `ch_av = float(clearinghouseState["marginSummary"]["accountValue"])`
- `c_mm = float(clearinghouseState.get("crossMaintenanceMarginUsed", 0.0))`
- `spot_usdc_total = float(usdc_balance["total"])` (or `0.0` if no `USDC` / token `0` balance is present)
- `avail_after_maint_usdc = float(avail_map[0])` if token `0` exists in `spotClearinghouseState.get("tokenToAvailableAfterMaintenance", [])`, else `None`

Then:
- **If `userAbstraction in {"disabled", "default"}`:**
  $$\text{equity} = \text{ch\_av}, \qquad \text{equitySource} = \texttt{"perp"}$$
- **Otherwise (`"unifiedAccount"`, `"portfolioMargin"`, `"dexAbstraction"`, etc.):**
  $$\text{equity} = \max\Big(\text{ch\_av},\; \text{spot\_usdc\_total},\; \text{avail\_after\_maint\_usdc} + \text{c\_mm}\Big), \qquad \text{equitySource} = \texttt{"perp+spot"}$$
  *(If token `0` is absent from `tokenToAvailableAfterMaintenance`, omit the third term / treat it as $-\infty$.)*

### Impact on `§5.1` Stage 2 & `§6.3` Fast Loop
1. **Stage 2 Daily Scoring (`§5.1`)**: Because `77.0%` of Stage 1 candidates operate under `unifiedAccount` or `portfolioMargin`, Stage 2 must fetch `spotClearinghouseState` (`weight = 2`) for all non-`disabled`/`default` candidates (and additionally for any `disabled`/`default` candidate with open perp short positions to compute `hedgeRatio`). Candidates in `disabled`/`default` with no open perp shorts skip `spotClearinghouseState`.
2. **Fast Loop (`§6.3`)**: `daily` persists each whitelisted address's `userAbstraction` inside `state/daily/whitelist_state.json`. The 10-minute `fast` job reads `whitelist_state.json` (read-only per `§4`) and issues `spotClearinghouseState` (`weight = 2`) only for non-`disabled`/`default` addresses (`~77%` of active whales, skipping `disabled`/`default` even if short since `fast` does not compute `hedgeRatio`). Across 200 active whales (`~154` requiring `spotClearinghouseState`), this adds `~308` weight per fast cycle (derived from `154 × 2`), keeping the total fast loop (`~1,700` weight) well within the `800 weight/min` CI budget and `7`-minute timeout.

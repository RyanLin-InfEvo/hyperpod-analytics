<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Task 0.7 — `portfolio` API Windows, Timestamp Alignment & Deposit/Withdrawal Invariance

## 1. Survey Scope & Probe Commands
Audited `POST https://api.hyperliquid.xyz/info` across **10 diverse fixture addresses** (excluding the protocol vault `vault_hlp`) and **3 heavy-transfer accounts** via:
- `{"type": "portfolio", "user": "<address>"}` (`weight = 20`; returns 8 windows: `day`, `week`, `month`, `allTime`, `perpDay`, `perpWeek`, `perpMonth`, `perpAllTime`)
- `{"type": "userNonFundingLedgerUpdates", "user": "<address>", "startTime": 0}` (`weight = 20`, up to 2,000 events per call; audited on `0xfc27136e...`, `0x7fdafde5...`, and `0xdf9ea6ec...`)

Verbatim wire response structure (`fixtures/api/portfolio_0xfc27136e.json`, `perpAllTime` tail):
```json
[
  "perpAllTime",
  {
    "accountValueHistory": [
      [1794113400048, "42600324.217569001"],
      [1794304200058, "48473777.9208210036"],
      [1794501688681, "43017842.1642000005"],
      [1794697800064, "45466974.5869119987"],
      [1794721519895, "43502758.6100940034"]
    ],
    "pnlHistory": [
      [1794113400048, "22048020.7375690006"],
      [1794304200058, "23171474.6408209987"],
      [1794501688681, "25715538.8841999993"],
      [1794697800064, "36350420.2269119993"],
      [1794721519895, "34386204.2500939965"]
    ],
    "vlm": "14006963530.7700004578"
  }
]
```

---

## 2. Point Counts & Timestamp Alignment Across All 10 Addresses (`80 / 80` Windows)

Across all `10` addresses × `8` windows (`80 / 80` windows = **100%**):
- **`len(accountValueHistory) == len(pnlHistory)`** holds on **80 / 80** windows.
- **`ts_aligned == true`** holds on **80 / 80** windows: `[x[0] for x in accountValueHistory] == [x[0] for x in pnlHistory]` bit-for-bit at every index `k`.

| Tag (`address`) | `day` | `week` | `month` | `allTime` | `perpDay` | `perpWeek` | `perpMonth` | `perpAllTime` | `perpAllTime` Span (days) | `ts_aligned` (all 8) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| `whale_disabled` (`0xfc27136e...`) | 13 | 62 | 45 | 42 | 13 | 62 | 45 | 41 | 220.3 | `true` (8/8) |
| `whale_default` (`0x87f9cd15...`) | 26 | 62 | 44 | 80 | 26 | 62 | 62 | 131 | 514.3 | `true` (8/8) |
| `mm_auros` (`0x023a3d05...`) | 31 | 63 | 46 | 81 | 31 | 63 | 46 | 86 | 899.4 | `true` (8/8) |
| `hft_top1` (`0xf5d81a13...`) | 12 | 61 | 46 | 55 | 12 | 61 | 46 | 54 | 332.3 | `true` (8/8) |
| `unified_1` (`0xdf9ea6ec...`) | 14 | 62 | 46 | 96 | 14 | 62 | 46 | 93 | 927.2 | `true` (8/8) |
| `unified_2` (`0x1f250df5...`) | 16 | 64 | 54 | 97 | 16 | 64 | 59 | 107 | 521.3 | `true` (8/8) |
| `pm_1` (`0xbf732ea0...`) | 18 | 62 | 50 | 96 | 17 | 66 | 63 | 135 | 710.3 | `true` (8/8) |
| `pm_2` (`0xb1ec7feb...`) | 14 | 63 | 44 | 79 | 14 | 63 | 45 | 81 | 500.3 | `true` (8/8) |
| `master_subs` (`0x7fdafde5...`) | 14 | 64 | 48 | 84 | 14 | 64 | 48 | 83 | 773.4 | `true` (8/8) |
| `subaccount_active` (`0x6ecbb68c...`) | 15 | 64 | 47 | 58 | 15 | 64 | 47 | 58 | 360.3 | `true` (8/8) |

### Resolution & Bucket Characteristics
- **`day` / `perpDay`**: `12–31` points (typically ~2h buckets plus live terminal point, e.g. median `2.00h` on `0xfc27136e`, down to `0.63h–1.00h` on high-activity accounts `mm_auros` and `whale_default`).
- **`week` / `perpWeek`**: `61–64` points (`66` on `pm_1` `perpWeek`), roughly ~`2.3h` median buckets across `7` days.
- **`month` / `perpMonth`**: `44–63` points (`month`: `44–54`, `perpMonth`: `45–63`), roughly ~`14h` typical buckets across `30.3` days (ranging `~6h–23h` median depending on activity).
- **`allTime` / `perpAllTime`**: `42–97` points (`allTime`) and `41–135` points (`perpAllTime`), spanning full account lifetime (`220–927` days on `perpAllTime`, up to `1,032` days on `allTime`). Buckets scale dynamically with account age (roughly ~7-day `167h` steps for 220–520d accounts, ~14-day `335h` steps for ~900d accounts, with extra intermediate points emitted around sharp PnL/transfer changes).

### Implications for `§5.4 maxDrawdown` & `§6.5 reconstruct.py`
1. **`§5.4 maxDrawdown` Alignment & Cadence**:
   - Because `ts_aligned == true` on `100%` of windows (`80/80`), `perpAllTime.pnlHistory[k]` and `perpAllTime.accountValueHistory[k]` share identical timestamps, making `DD_t = (max_{s≤t} PnL_s - PnL_t) / AV_{argmax}` directly computable in one pass with zero timestamp-matching gaps.
   - `perpAllTime` captures multi-day/weekly peak-to-trough swings cleanly across full account history (`41–135` points) in a single `weight = 20` call without pagination or API limits.
2. **`§6.5 reconstruct.py` Interpolation & Enrichment**:
   - For pre-launch historical reconstruction (`reconstructDays = 365`, hourly within 208d and 4-hourly beyond 208d), linear interpolation of `AV(t)` across `perpAllTime.accountValueHistory` yields a smooth continuous equity envelope.
   - Where finer recent equity resolution is desired, `perpMonth`, `perpWeek`, and `perpDay` points can be spliced onto `perpAllTime` in monotonically increasing timestamp order: cross-window comparison on `0xfc27136e` confirms that all `5` overlapping timestamps with `perpWeek` and all `11` overlapping timestamps with `perpMonth` have **bit-for-bit identical** `accountValueHistory` values.

---

## 3. Deposit & Withdrawal Correlation Proof (`userNonFundingLedgerUpdates` vs `perpAllTime`)

Under accounting identity $\mathrm{AV}(t) = \mathrm{NetPrincipal}(t) + \mathrm{PnL}_{\mathrm{cum}}(t)$, rearranging gives:
$$\mathrm{NetPrincipal}(t) = \mathrm{AV}(t) - \mathrm{PnL}_{\mathrm{cum}}(t)$$
If `perpAllTime.pnlHistory` is invariant to external capital flows, then any deposit or withdrawal of $\Delta C$ changes `AV(t)` and `AV(t) - PnL(t)` by $\Delta C$ while leaving `pnlHistory` purely driven by trading PnL (and funding/fees).

We verified this against `userNonFundingLedgerUpdates` on three high-transfer accounts:
- `0xfc27136e42af1732ddc9ce2605ea9bff1b959d9d` (`327` events; types: `cStakingTransfer`, `deposit`, `send`, `spotGenesis`, `spotTransfer`, `withdraw`)
- `0x7fdafde5cfb5465924316eced2d3715494c517d1` (`2,000` events; types: `accountClassTransfer`, `deposit`, `send`, `spotGenesis`, `spotTransfer`, `vaultDeposit`, `withdraw`)
- `0xdf9ea6ec3b7109935ccb4fb267e15ac1fb077ab1` (`2,000` events; types: `accountClassTransfer`, `deposit`, `internalTransfer`, `spotGenesis`, `spotTransfer`, `subAccountTransfer`, `withdraw`)

### Case Study: `0xfc27136e42af1732ddc9ce2605ea9bff1b959d9d` (`perpAllTime` Tail)

| Timestamp (`ms` UTC) | `accountValue` (`AV`, USD) | `pnl` (`PnL`, USD) | `NetPrincipal = AV - PnL` (USD) | Interval Notes (`userNonFundingLedgerUpdates`) |
|---:|---:|---:|---:|---|
| `1793574600006` | `33,916,615.60` | `15,436,826.27` | `18,479,789.33` | Baseline before `$9.00M` net inflow |
| `1793657400006` | `45,929,558.39` | `18,449,769.26` | `27,479,789.13` | `AV` `+$12.01M` (`+$12,012,942.79`), `PnL` `+$3.01M` (`+$3,012,942.99`) → `AV - PnL` rose **`+$9.00M`** (`+$8,999,999.80`) |
| `1794113400048` | `42,600,324.22` | `22,048,020.74` | `20,552,303.48` | Post-`$5.00M` `withdraw` (`t=1793721298901`, `usdc=4,999,999.0`, `fee=1.0`) & subsequent inflows |
| `1794304200058` | `48,473,777.92` | `23,171,474.64` | `25,302,303.28` | Peak `AV` prior to `-$8.00M` outflow |
| `1794501688681` | `43,017,842.16` | `25,715,538.88` | `17,302,303.28` | `AV` dropped `-$5.46M` (`-$5,455,935.76`), `PnL` **ROSE** `+$2.54M` (`+$2,544,064.24`) → `AV - PnL` dropped by **exact `-$8,000,000.00`** |
| `1794697800064` | `45,466,974.59` | `36,350,420.23` | `9,116,554.36` | During `$6,999,999` `withdraw` (`t=1794513992754`, `fee=1.0`) + transfer: `AV - PnL` dropped `-$8.19M` (`-$8,185,748.92`), while `PnL` climbed `+$10.63M` (`+$10,634,881.34`) |
| `1794721519895` | `43,502,758.61` | `34,386,204.25` | `9,116,554.36` | Zero ledger transfers: `AV` (`-$1,964,215.98`) and `PnL` (`-$1,964,215.98`) moved identically; `AV - PnL` constant to the cent |

### Corroborating Checks (`0x7fdafde5...` & `0xdf9ea6ec...`)
- **`0x7fdafde5...`**: Between `t=1794633120106` and `t=1794692803177`, `AV` dropped `-$6.00M` (`$28,122,130.29 → $22,120,437.04`) while `PnL` rose `+$2.50M` (`$156,403,773.11 → $158,902,079.86`), corresponding to an exact **`-$8,500,000.00`** outflow (`AV - PnL`: `-$128,281,642.82 → -$136,781,642.82`). Its negative `AV - PnL` (`-$138.80M` at `t=1794721573434`) reflects `$158.59M` cumulative realized PnL largely withdrawn while keeping `$19.79M` perp equity.
- **`0xdf9ea6ec...`**: Across its last `5` `perpAllTime` points (`t=1793663880033` to `t=1794721565860`), zero external transfers occurred and `AV - PnL` stayed identically **`$5,205,961.86`** at all 5 timestamps despite `AV` moving from `$415,613.51` to `$891,552.80` to `$782,333.32`.

---

## 4. Conclusions & Edge Cases
1. **`§5.4 maxDrawdown` is empirically proven immune to deposits and withdrawals**: Across all tested transfer types (`deposit`, `withdraw`, `subAccountTransfer`, `internalTransfer`, `spotTransfer`, `accountClassTransfer`, `cStakingTransfer`, `vaultDeposit`, `send`), `perpAllTime.pnlHistory` tracks pure cumulative trading PnL while deposits/withdrawals shift only `AV(t)`. Using `(max_{s≤t} PnL_s - PnL_t) / AV_{argmax}` prevents both false drawdowns on withdrawals and false drawdown dilution on fresh deposits.
2. **HIP-3 Perp DEX Inclusion Edge Case**: Cross-checking against `docs/research/equity.md` shows `perpAllTime` aggregates main perp **plus** HIP-3 builder perp DEXes (e.g. `0xfc27136e...` holds `$23.10M` main perp + `$20.42M` in `xyz` + `$0.01` in `cash` = `$43.52M` matching `perpAllTime` AV; `0x7fdafde5...` holds `$11.20M` in `xyz`). `§5.4 maxDrawdown` uses `perpAllTime` as-is (`【規範】`), while `§6.5 reconstruct.py` should note that interpolated `perpAllTime.accountValueHistory` reflects combined perp equity (main + HIP-3) for addresses active on HIP-3 DEXes.

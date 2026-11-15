<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Task 0.12 — Empirical Storage Sizing, Compression & Parse Latency Benchmark

## 1. Benchmark Methodology & Empirical Inputs
Measured exact compact JSON (`separators=(",", ":")`), `gzip -9` disk size, compression ratio, and Python `gzip.decompress + json.loads` latency (`time.perf_counter()`) across **10 realistic `§3.3`–`§3.5` payloads** constructed from Phase 0 live captures:
- **Fills (`34,948` empirical normalized rows)**: Extracted from full 2,000-fill `userFillsByTime` responses across 20 addresses (`§3.4` 7-column schema `[time, addrId, dirCode, px, sz, notional, liq]` with `px` quantized to 6 sig figs).
- **Candles (`BTC` `5m` and `4h`)**: Live `candleSnapshot` responses converted to `§3.5` 6-column schema `[t, o, h, l, c, v]`.
- **Snapshots (`200` addrs × `28` coins)**: Top `28` main-perp coins from `meta["universe"]`, `accountValue` quantized to 4 sig figs, `mark` / `entryPx` to 6 sig figs (`§3.3`), with `144` 10-minute points + `1,200` `posChanges` per day and `720` hourly points + `36,000` `posChanges` per month.

---

## 2. Measured Payload Benchmarks (Phase 0.12)

| Payload | Shape / Scope | Raw Size (`B`) | Gzip Size (`B`) | Ratio | `gunzip + json.loads` |
|---|---|---:|---:|---:|---:|
| `snapshot_day` | 200 addrs, 28 coins, 144 pts, 1,200 `posChanges` | `389,371` (~380 KiB) | `32,493` (~31.7 KiB) | `11.98×` | `3.64 ms` |
| `snapshot_month` | 200 addrs, 28 coins, 720 hourly pts, 36,000 `posChanges` | `2,895,754` (~2.76 MiB) | `273,693` (~267 KiB) | `10.58×` | `32.19 ms` |
| `fills_BTC_1day` | 900 rows (`§3.4` 7-col) | `40,485` (~39.5 KiB) | `12,151` (~11.9 KiB) | `3.33×` | `0.58 ms` |
| `fills_6000_rows` | ~1 day, all 28 coins combined | `272,150` (~266 KiB) | `73,767` (~72.0 KiB) | `3.69×` | `5.04 ms` |
| `fills_BTC_1month` | 27,000 rows (`BTC` sharded monthly) | `1,237,200` (~1.18 MiB) | `326,266` (~319 KiB) | `3.79×` | `18.11 ms` |
| `fills_180k_rows` | ~1 month, all 28 coins combined | `8,291,822` (~7.91 MiB) | `2,124,484` (~2.03 MiB) | `3.90×` | **`125.88 ms`** |
| `candles_5m_1coin_1month` | 8,640 `5m` bars (`30d × 288`) | `493,752` (~482 KiB) | `149,219` (~146 KiB) | `3.31×` | `6.32 ms` |
| `overview_4h_1coin_1year` | 2,190 `4h` bars (`365d × 6`) | `131,477` (~128 KiB) | `46,641` (~45.5 KiB) | `2.82×` | `1.58 ms` |
| `snapshots/latest.json` | 200 addrs, 28 coins, 600 open positions | `17,452` (~17.0 KiB) | `3,828` (~3.74 KiB) | `4.56×` | `0.26 ms` |
| `whitelist.json` | 200 addrs (`§3.2` metadata + metrics) | `102,376` (~100 KiB) | `2,923` (~2.85 KiB)* | `35.02×`* | `0.68 ms` |

> *Note on `whitelist.json`:* The benchmark fixture used synthetic repeated `metrics` across the 200 rows (`35.02×` gzip); with realistic diverse per-address metrics, CDN gzip transfer is `~12–18 KB`, still negligible.

### Key Architectural Observations
1. **Why `§3.4` Shards Fills Per Coin**: Combining all 28 coins into a single monthly fills file (`180,000` rows, `8.29 MB` raw) requires **`125.88 ms`** just for `gunzip + json.loads`, violating the `< 100 ms` per-file parse budget (`§3.8`) before Dart typed-array conversion even starts. Sharding by coin (`fills/{coin}/months/YYYY-MM.json.gz`, e.g. `BTC` `27,000` rows = `1.24 MB` raw / `326 KB` gzip) cuts latency to **`18.11 ms`** (`<< 100 ms`) and avoids downloading unused coins.
2. **Snapshots (`§3.3`)**: Keyframe + change-log plus hourly `series` compaction keeps `12` months at **`~34.7 MB` raw / `~3.28 MB` gzip** (better than the v1 estimate of `~5–7 MB`). The hot 7-day `days/*.json` window occupies `~2.73 MB` raw on disk (`~227 KB` over CDN gzip).
3. **Fills Per-Row Scaling (`§3.4`)**: At `180k` rows, fills average **`~46.0 B/row` raw** (`46.07 B`) and **`~11.8 B/row` gzip** (`11.80 B`).
   - Hot `7d` across all 28 coins (`days/*.json`): `~1.91 MB` raw on disk (`~516 KB` over CDN gzip).
   - `1 year` ongoing (`2.16M` rows across 28 coins): `~99.5 MB` raw / **`~25.5 MB` gzip**.
   - **One-time bootstrap backfill**: `200` addresses × `<=10,000` raw fills divided by the Task 0.4 median aggregation ratio (`1.25×`) ≈ `~1.6M` aggregated rows = **`~73.7 MB` raw / `~18.9 MB` gzip**.
4. **Candles (`§3.5`)**:
   - `5m` archives (`28 coins × 12 months = 336 files`): `~165.9 MB` raw / **`~50.1 MB` gzip** (`336 × 149,219 B`, higher than v1's `30–35 MB` estimate).
   - `overview_4h` (`28 coins × 1 year`): `~3.68 MB` raw / **`~1.31 MB` gzip**.
   - One-time `backfill_15m_1h` (days 18–52 `15m` + days 53–208 `1h` = `7,104` bars/coin; conservative non-repeating estimate `~210 KB` gzip/coin across `28` coins): **`~5.9 MB` gzip**.

---

## 3. Total Year 1 Disk Footprint & Multi-Year Runway (`gh-pages/data/`)

| Category | Storage Format (`gh-pages/data/`) | Year 1 Disk Footprint | Ongoing / Year |
|---|---|---:|---:|
| Bootstrap fills backfill (`~1.6M` agg rows) | `fills/{coin}/months/*.json.gz` | `~18.9 MB` (`~73.7 MB` raw) | `0 MB` (one-time) |
| Bootstrap `backfill_15m_1h` (`28` coins × `~210 KB`) | `candles/{coin}/backfill_15m_1h.json.gz` | `~5.9 MB` | `0 MB` (one-time) |
| Monthly snapshots (`12` months) | `snapshots/months/YYYY-MM.json.gz` | `~3.3 MB` (`~3.28 MB` gz / `~34.7 MB` raw) | `~3.3 MB/yr` |
| Monthly fills (`28` coins × `12` months, `2.16M` rows) | `fills/{coin}/months/*.json.gz` | `~25.5 MB` (`~99.5 MB` raw) | `~25.5 MB/yr` |
| Monthly `5m` candles (`28` coins × `12` months, `336` files) | `candles/{coin}/5m/*.json.gz` | `~50.1 MB` (`~165.9 MB` raw) | `~50.1 MB/yr` |
| `overview_4h` (`28` coins × `2,190` bars/yr) | `candles/{coin}/overview_4h.json.gz` | `~1.3 MB` (`~1.31 MB` gz / `~3.68 MB` raw) | `~1.3 MB/yr` |
| Hot 7-day uncompressed JSON (`days/*.json`) | `snapshots/days/` (`2.73 MB`) + `fills/*/days/` (`1.91 MB`) | `~4.6 MB` (steady-state rolling window) | `0 MB` net growth |
| Metadata & latest snapshot | `manifest.json`, `whitelist.json`, `criteria.json`, `candidates.json`, `latest.json` | `< 0.3 MB` | `< 0.1 MB/yr` |
| **Total** | | **`~110 MB` (Year 1)** | **`~85 MB/year`*** |

> \*Ongoing base accumulation sums to `~80.2 MB/year` (`3.28 + 25.49 + 50.14 + 1.31`), budgeted at **`~85 MB/year`** including minor coin-universe growth and metadata updates.
> - **Runway to `800 MB` warning (`§3.3`)**: `(800 - 110) / 85 + 1 ≈ 9.1 years` (**~9 years** before approaching GitHub Pages' 1 GB limit).
> - **Cold-storage ADR trigger (`§3.8`)**: `~85 MB/year` is well below the `> 200 MB/year` threshold, so no cold-storage ADR is required before launch.

---

## 4. First-Screen CDN Wire Transfer Budget

Per Task 0.10 (`docs/research/pages-gzip.md`), GitHub Pages transparently compresses `.json` files (`content-encoding: gzip`) while serving `.json.gz` payloads verbatim (`content-type: application/gzip`) for client-side decompression. Initial view (`7D` on `BTC`) transfers:
- `manifest.json` + `whitelist.json` (`~12–18 KB` realistic CDN gz) + `criteria.json` + `snapshots/latest.json` (`~3.8 KB` gz): `~25 KB`
- `7d` snapshot `days/*.json` (`7 × 32,493 B`): `~227 KB`
- `7d` `BTC` fill `days/*.json` (`7 × 12,151 B`): `~85 KB`
- Current month `BTC` `5m` candles (`candles/BTC/5m/YYYY-MM.json.gz`): `~149 KB`
- **Total first-screen wire transfer**: **`~0.5 MB`** (well inside the **`< 1.5 MB`** budget in `§3.8`).

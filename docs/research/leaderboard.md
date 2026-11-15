<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Task 0.1 — Leaderboard Endpoint, Wire Schema, Refresh Frequency & Stage 1 Funnel

## 1. Endpoint & Capture Method
Captured two snapshots of the Hyperliquid Mainnet leaderboard ~1 hour apart using:
```bash
curl -sS -D headers.txt https://stats-data.hyperliquid.xyz/Mainnet/leaderboard -o leaderboard.json
```
The endpoint is backed by Amazon S3 behind CloudFront CDN (`X-Amz-Cf-Pop: LAX53-P4`, `Vary: Origin`, `x-amz-server-side-encryption: AES256`).

## 2. Snapshot Comparison & Refresh Frequency

| Metric | Snapshot #1 | Snapshot #2 | Delta / Notes |
|---|---:|---:|---|
| Fetch Time (`Date` header) | `Sun, 15 Nov 2026 04:40:16 GMT` | `Sun, 15 Nov 2026 05:38:43 GMT` | `+58m 27s` |
| S3 `Last-Modified` | `Sun, 15 Nov 2026 04:38:30 GMT` | `Sun, 15 Nov 2026 05:38:36 GMT` | **`+60m 06s` (`3,606s`)** |
| `ETag` | `"b824632759190fd0ffaf4617102f3976-5"` | `"9f4cb89d41152b17244a4ade641aac3a-5"` | New S3 multipart object |
| CloudFront `Age` / `X-Cache` | `2517` / `Hit from cloudfront` | `748` / `Hit from cloudfront` | Edge-cached hourly object |
| Payload Size (`Content-Length`) | `38,943,013` bytes (`~37.14 MiB`) | `38,933,465` bytes (`~37.13 MiB`) | `-9,548` bytes |
| Download Elapsed | `4.552s` (`~4.55s`) | `4.472s` (`~4.47s`) | Single HTTP GET |
| Row Count (`leaderboardRows`) | `47,662` | `47,649` | `-13` net rows |

### Verbatim Response Headers (Snapshot #1 & #2)
Snapshot #1 (`LAX53-P4`):
```http
Content-Type: application/json
Content-Length: 38943013
Server: AmazonS3
Date: Sun, 15 Nov 2026 04:40:16 GMT
Last-Modified: Sun, 15 Nov 2026 04:38:30 GMT
ETag: "b824632759190fd0ffaf4617102f3976-5"
x-amz-server-side-encryption: AES256
Accept-Ranges: bytes
X-Cache: Hit from cloudfront
Via: 1.1 0c32860274691581031a51698ea82be8.cloudfront.net (CloudFront)
X-Amz-Cf-Pop: LAX53-P4
Age: 2517
Vary: Origin
```

Snapshot #2 (`LAX53-P4`):
```http
Content-Type: application/json
Content-Length: 38933465
Server: AmazonS3
Date: Sun, 15 Nov 2026 05:38:43 GMT
Last-Modified: Sun, 15 Nov 2026 05:38:36 GMT
ETag: "9f4cb89d41152b17244a4ade641aac3a-5"
x-amz-server-side-encryption: AES256
Accept-Ranges: bytes
X-Cache: Hit from cloudfront
Via: 1.1 46fad0fa90a137a4d3e3f5f29cbccffc.cloudfront.net (CloudFront)
X-Amz-Cf-Pop: LAX53-P4
Age: 748
Vary: Origin
```

### Row-Level Diff (`Snapshot #1` -> `Snapshot #2`)
- **Added rows**: `+14`
- **Removed rows**: `-27`
- **Common rows**: `47,635`
  - Changed `accountValue`: `31,102` (`65.29%` of common rows)
  - Changed `windowPerformances`: `25,920` (`54.41%` of common rows)

**Conclusion on update frequency**: S3 `Last-Modified` advanced by **60 minutes 6 seconds** (`04:38:30 GMT` -> `05:38:36 GMT`), confirming an **hourly CDN-backed S3 refresh** (with CloudFront serving cached hits up to ~1 hour old, as shown by `Age: 2517`). Because leaderboard equity is updated hourly and lags real-time on-chain margin, Stage 1 uses `leaderboard.accountValue` solely as a free prefilter while Stage 2 (`§5.4`) computes real-time `equity` from `clearinghouseState` / `spotClearinghouseState`.

## 3. Wire Schema & Parsing Quirks
Top-level JSON structure:
```json
{
  "leaderboardRows": [
    {
      "ethAddress": "0x...",
      "accountValue": "101173078.3927030265",
      "windowPerformances": [
        ["day",     {"pnl": "...", "roi": "...", "vlm": "..."}],
        ["week",    {"pnl": "...", "roi": "...", "vlm": "..."}],
        ["month",   {"pnl": "...", "roi": "...", "vlm": "..."}],
        ["allTime", {"pnl": "...", "roi": "...", "vlm": "..."}]
      ],
      "prize": 0,
      "displayName": null
    }
  ]
}
```

### Verbatim Row Examples (from Snapshot #1)
Row `0` (`displayName: null`):
```json
{"ethAddress": "0xf5d81a135f756ca16544e53c20fc20643ec3ad53", "accountValue": "101173078.3927030265", "windowPerformances": [["day", {"pnl": "1861654.3461340002", "roi": "0.0187521556", "vlm": "1061064345.5499998331"}], ["week", {"pnl": "4679805.1174889999", "roi": "0.0433424446", "vlm": "11838811067.8999996185"}], ["month", {"pnl": "27423164.0314679965", "roi": "0.303130674", "vlm": "45379015319.9300079346"}], ["allTime", {"pnl": "25468391.9727029987", "roi": "0.204791576", "vlm": "228760989145.509979248"}]], "prize": 0, "displayName": null}
```

Row `10` (`displayName` populated):
```json
{"ethAddress": "0x7b7f72a28fe109fa703eeed7984f2a8a68fedee2", "accountValue": "7554000.6817859998", "windowPerformances": [["day", {"pnl": "-32078.73716", "roi": "-0.0088934287", "vlm": "579649888.0099999905"}], ["week", {"pnl": "118331.175979", "roi": "0.0342333442", "vlm": "1864110470.3600001335"}], ["month", {"pnl": "650514.792263", "roi": "0.2224411077", "vlm": "6218522988.6699991226"}], ["allTime", {"pnl": "-24970852.5382139981", "roi": "-0.7227217912", "vlm": "202196171474.5399780273"}]], "prize": 0, "displayName": "beatmaker1@proton.me"}
```

Verified structural observations across all `47,662` rows of Snapshot #1:
- **Exact key set**: Every row has strictly `{"ethAddress", "accountValue", "windowPerformances", "prize", "displayName"}` (`0` rows with extra/missing keys) and `47,662` unique `ethAddress` values.
- **Tuple-list encoding for `windowPerformances`**: Encoded as an array of 4 `[windowName, metricsDict]` pairs in exact order `("day", "week", "month", "allTime")` — **not** a JSON object. Pydantic models must deserialize this as `list[tuple[str, WindowMetrics]]` (or convert via validator).
- **Stringified decimals**: `accountValue`, `pnl`, `roi`, and `vlm` are decimal strings (arbitrary precision, negative allowed for `pnl`/`roi`), whereas `prize` is an integer (`0` across all `47,662` rows) and `displayName` is `str | null` (`1,481` non-null entries, `3.11%`).
- **Global ordering**: All `47,662` rows are sorted globally descending by `month.vlm`.
- **Zero / non-trading outliers**: `4,095` rows (`8.59%`) have `accountValue <= 0` (`min = 0.0`). At the upper tail, system/escrow-like row `0xa822a9ceb6d6cb5b565bd10098abcfa9cf18d748` reports `accountValue = "13744394847.9728622437"` (`$13.74B`) with `vlm = "0.0"` and `allTime.pnl = "-500.0"`, which is cleanly excluded by Stage 1's `allTime.pnl > 0` check.

## 4. Stage 1 Prefilter Funnel (`§5.1`)
Evaluating the `§5.1` Stage 1 criteria (`accountValue >= 1,000,000`, `allTime.pnl > 0`, `month.pnl > -0.2 * accountValue`, `month.vlm / accountValue <= 150`) across Snapshot #1 (`47,662` rows):

| Stage 1 Step / Condition | Count | Share of Total | Share of `>= $1M` | Notes |
|---|---:|---:|---:|---|
| Total `leaderboardRows` (Snapshot #1) | `47,662` | `100.00%` | — | Snapshot #2: `47,649` rows |
| Excluded by `accountValue < 1,000,000` | `45,284` | `95.01%` | — | — |
| Remaining with `accountValue >= $1M` | `2,378` | `4.99%` (~`5.0%`) | `100.00%` | Snapshot #2: `2,368` |
| Fail `allTime.pnl > 0` (among `>= $1M`) | `598` | `1.25%` | `25.15%` | `470` fail this rule only |
| Fail `month.pnl > -0.2 * accountValue` (among `>= $1M`) | `201` | `0.42%` | `8.45%` | `75` fail this rule only |
| Fail `month.vlm / accountValue <= 150` (among `>= $1M`) | `68` | `0.14%` | `2.86%` | `36` fail this rule only |
| Union failing `>= 1` post-`$1M` rule | `717` | `1.50%` | `30.15%` | Includes `136` overlapping multi-rule failures |
| **Pass Stage 1** | **`1,661`** | **`3.48%`** | **`69.85%`** | Snapshot #2: **`1,655`** pass |

**Pipeline sizing implication**: Stage 1 reduces the candidate pool from `47,662` to `~1,661` addresses (`~3.48%`, plus any `seed_addresses.txt` and qualifying `>= $1M` `subAccounts`), validating the `§5.1` Stage 2 quota budget (~140k–160k weight/day incremental).

## 5. Subaccount & Vault Presence in Leaderboard
Cross-referenced against [subaccounts.md](subaccounts.md) (Phase 0.8 survey probing top 200 Stage 1 candidates and top 50 leaderboard rows via `userRole` and `subAccounts`):
- **Subaccounts (`0 / 595` = `0.0%`)**: Although `98 / 194` (`50.5%`) of sampled `user` masters own `595` subAccounts (including `17` subAccounts with `clearinghouseState.accountValue >= $1,000,000`), **`0` of `595` subAccount addresses appear anywhere in the `47,662`-row `leaderboardRows`**. Hyperliquid aggregates/reports leaderboard rows at the master/vault level only. Stage 2 MUST enumerate `subAccounts(master)` to discover qualifying `>= $1M` subAccounts (`§5.1`).
- **Vaults (`2%–3%`)**: Vault addresses **do** appear directly in `leaderboardRows`:
  - Top 50 overall leaderboard: `1 / 50` (`2.0%`) is a vault (`0xc179e03922afe8fa9533d3f896338b9fb87ce0c8`, `av = "$3,439,222.051647"`, `displayName: null`, row `26`).
  - Top 200 Stage 1 candidates: `6 / 200` (`3.0%`) are vaults (e.g., `0xb0a55f13d22f66e6d495ac98113841b2326e9540` with `$30,000,000.0`, `0x1e37a337ed460039d1b15bd3bc489de789768d5e` with `$12,162,210.47`).
  - Therefore, Stage 2 step 1 (`userRole`, `60` weight, permanently cached) MUST reject `role ∈ {vault, agent, missing}` before fetching `portfolio`, `clearinghouseState`, fills, or funding.

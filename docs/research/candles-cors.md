<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Task 0.9 — `candleSnapshot` CORS Headers, 5,000-Candle Lookback Depth & Coin Naming Rules

## 1. CORS Verification (`POST https://api.hyperliquid.xyz/info`)
Probed from origin `https://ryanlin-infevo.github.io` using `curl -is`:

### Preflight `OPTIONS` Request
```bash
curl -is -X OPTIONS https://api.hyperliquid.xyz/info \
  -H "Origin: https://ryanlin-infevo.github.io" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type"
```
Verbatim response headers:
```http
HTTP/1.1 200 OK
Content-Length: 0
Connection: keep-alive
Server: nginx/1.22.1
Date: Sun, 15 Nov 2026 05:36:52 GMT
allow: POST
access-control-allow-origin: *
vary: origin
vary: access-control-request-method
vary: access-control-request-headers
access-control-allow-methods: *
access-control-allow-headers: *
```

### Actual `POST` Request
```bash
curl -is -X POST https://api.hyperliquid.xyz/info \
  -H "Content-Type: application/json" \
  -H "Origin: https://ryanlin-infevo.github.io" \
  -d '{"type":"candleSnapshot","req":{"coin":"BTC","interval":"5m","startTime":1794720000000,"endTime":1794721515513}}'
```
Verbatim response headers & excerpt:
```http
HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: 544
Connection: keep-alive
Server: nginx/1.22.1
Date: Sun, 15 Nov 2026 05:36:52 GMT
access-control-expose-headers: *
access-control-allow-origin: *
vary: origin
vary: access-control-request-method
vary: access-control-request-headers

[{"t":1794720000000,"T":1794720299999,"s":"BTC","i":"5m","o":"64981.0","c":"64956.0","h":"64981.0","l":"64922.0","v":"13.8201","n":602},...]
```
**Conclusion**: Browser Flutter Web (`CandleSource`) can call `candleSnapshot` directly from `https://ryanlin-infevo.github.io` without a proxy (`access-control-allow-origin: *`, `access-control-allow-methods: *`, `access-control-allow-headers: *`).

## 2. 5,000-Candle Lookback Depth (`BTC`, `startTime=0`, `endTime=1794721515513`)
Notice that `endTime` is **mandatory** in `candleSnapshot` (`req: {coin, interval, startTime, endTime}`), and the API enforces a rolling ~5,000-candle window (slightly >5,000 up to the current incomplete hour/day boundary):

| Interval | Returned Count | `first_t` (UTC ms) | `last_t` (UTC ms) | Span (days) | Notes |
|---|---:|---:|---:|---:|---|
| `1m` | 5,149 | 1794412620000 | 1794721500000 | 3.58 | ~3.5 days |
| `5m` | 5,030 | 1793212800000 | 1794721500000 | 17.46 | ~17.5 days (matches §3.5's 17-day `5m` window) |
| `15m` | 5,010 | 1790213400000 | 1794721500000 | 52.18 | ~52.2 days (fills days 18–52 in `backfill_15m_1h.json.gz`) |
| `1h` | 5,002 | 1776715200000 | 1794718800000 | 208.38 | ~208.4 days (fills days 53–208 and 1h reconstruction mark) |
| `4h` | 5,001 | 1722715200000 | 1794715200000 | 833.33 | ~833 days (>2.2 years; drives `overview_4h.json.gz` & >208d mark) |
| `1d` | 2,280 | 1597795200000 | 1794700800000 | 2,279.00 | Full exchange/index history |

Wire schema of each candle object:
`{"t": int_open_ms, "T": int_close_ms, "s": str_coin, "i": str_interval, "o": str_dec, "c": str_dec, "h": str_dec, "l": str_dec, "v": str_dec, "n": int_trades}`.

## 3. Coin Naming Rules (`meta`, `spotMeta`, `perpDexs`)
- **Main Perp DEX (`meta`)**: `235` total markets (`56` marked `"isDelisted": true` such as `MATIC`, `RNDR`, `FTM`, `MKR`, `FXS`; `9` marked `"onlyIsolated": true`).
  - Plain ticker names with no prefix or `/` (e.g., `"BTC"` `szDecimals=5`, `"ETH"` `szDecimals=4`, `"SOL"` `szDecimals=2`, `"HYPE"` `szDecimals=2`).
  - `1000x` unit tokens use lowercase `k` prefix (`7` markets): `kPEPE`, `kSHIB`, `kBONK`, `kLUNC`, `kFLOKI`, `kDOGS`, `kNEIRO`.
  - Delisted markets MUST still retain their `szDecimals` lookup from `meta["universe"]` when reconstructing historical fills/trades (`§5.3`).
- **HIP-3 Builder-Deployed Perp DEXs (`perpDexs`)**: `10` active builder dexes (`["xyz", "flx", "vntl", "hyna", "km", "abcd", "cash", "para", "mkts", "io"]` alongside `null` for main dex). Their coins contain a colon (`"xyz:GOLD"`). Excluded by `§5.2` main-perp filter (`":" not in coin`).
- **Spot (`spotMeta`)**: `516` tokens, `352` pairs. Canonical spot pair `"PURR/USDC"` contains `/`; non-canonical pairs use `"@<index>"` (e.g., `"@107"` for HYPE/USDC spot). Both are excluded by `§5.2` main-perp filter (`not coin.startswith("@") and "/" not in coin and coin != "PURR/USDC"`).
- Verified `candleSnapshot` returns valid candles for `"kPEPE"`, `"HYPE"`, `"xyz:GOLD"`, and `"@107"`, confirming the coin filter must be enforced in our pipeline rather than relying on `candleSnapshot` to reject non-main-perp tickers.

<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Proposal 0003 — Multi-Source Address Discovery & Capital Tier Cohorts

- **Proposal:** `0003` *(Revised per User Feedback 2026-11-19)*
- **Date:** `2026-11-19`
- **Status:** `Accepted (2026-11-19, Two-Stage Rollout)`
- **Affects:** [`docs/implementation_plan.md`](../implementation_plan.md) `§3.1`, `§5.1`, `§6`, `§7.1`, `§8.2`, `§9`

---

## 1. User Goal & Design Clarification

Expand HyperPod's **Address Sources** beyond the single Hyperliquid S3 leaderboard (`>= $1M` tier) so users can discover, filter, and switch among multiple **capital tiers** and **curated external address sources** (referencing CoinGlass Hyperliquid Whale Tracker ranges `4`–`8`, or other community/personal address lists) using HyperPod's strict Stage 2 quality scoring (`§5.5`–`§5.6`).

- **No contrarian / losing indicators (`反向指標`):** Every tracked cohort passes HyperPod's positive quality screening (`§5.5`) or belongs to an explicitly chosen user-imported source list; no losing-trader contrarian signals are fabricated.
- **No fake relabeling:** External source provenance (e.g., `coinglass_leviathan`, `coinglass_api`, `hyperliquid_leaderboard`) is kept distinct from objective on-chain capital tier classification (`leviathan`, `super_whale`, `whale`, `small_whale`, `sniper`).

---

## 2. Verified Source Feasibility & CoinGlass Constraints

### 2.1 CoinGlass Integration Reality Check (`2026-11-19`)
- **Web pages (`coinglass.com/zh-TW/hl/range/4..8`):** Gated behind client-side/anti-bot protection; cannot be scraped in CI (`AGENTS.md` #11 & anti-bot policy).
- **CoinGlass API V4 (`https://open-api-v4.coinglass.com`):**
  - Endpoints `/api/hyperliquid/whale-position` (open perp positions `>= $1M`, returning `data[].user`), `/api/hyperliquid/wallet/position`, and `/api/hyperliquid/whale-alert` expose live wallet lists via `CG-API-KEY` header.
  - **Paid-only:** Programmatic API V4 whale-position endpoints require a paid subscription (`Startup` / `Standard` tier or higher), not a free tier.
  - Therefore, Core HyperPod MUST work `100%` without paid keys (via free Hyperliquid S3 leaderboard + user-maintained source files), while supporting CoinGlass API V4 as an **optional opt-in adapter** when the user supplies `COINGLASS_API_KEY` via GitHub Actions Secrets (`AGENTS.md` #11: zero secrets committed in repo).

### 2.2 Live Hyperliquid Leaderboard Census Across All 5 Capital Tiers (`n = 48,199`)
Measured against `stats-data.hyperliquid.xyz/Mainnet/leaderboard` (`2026-11-19`):

| Tier Key | Display Label | Account Value Range | Total Rows | `allTime.pnl > 0` | Current Stage 1 Passes | CoinGlass Active Ref |
|---|---|---|---:|---:|---:|---:|
| `leviathan` | 巨無霸 | `` `$50M – ∞` `` | `40` | `33` | `29` | `75` |
| `super_whale` | 超級鯨魚 | `` `$1M – $50M` `` | `2,342` | `1,775` | `1,665` | `309` |
| `whale` | 鯨魚 | `` `$500K – $1M` `` | `1,843` | `1,331` | `1,251` | `335` |
| `small_whale` | 小鯨魚 | `` `$100K – $500K` `` | `9,101` | `6,697` | `6,299` | `1,609` |
| `sniper` | 狙擊者 | `` `$50K – $100K` `` | `1,538` | `886` | `640` | `1,460` |
| **Subtotal (`$50K+`)** | — | — | **`14,864`** | **`10,722`** | **`9,884`** | **`3,788`** |

### 2.3 Rate-Limit Math (`800 weight/min` CI Cap)
- Scoring all `9,884` `$50K+` Stage 1 accounts daily would take `~15–20 hours/day` (overflowing GitHub Actions `300m` daily job limit), and 10-minute fast-loop polling of `3,788+` wallets would take `12–30 min` per `10m` tick.
- **Solution:** Enforce configurable **per-tier quotas** (`config/pipeline.yaml`, e.g., `$50M+` uncapped, `$1M–$50M` up to 250, `$500K–$1M` / `$100K–$500K` / `$50K–$100K` top `60–80` per tier prioritized by Stage 2 score + external `config/sources/*.txt` membership, or a `30m–60m` secondary polling cadence for smaller tiers) so the 10-minute `fast` loop remains strictly `<= 550` wallets (`< 4 min`).

---

## 3. Concrete Multi-Source Architecture

1. **Source 1 — Hyperliquid S3 Leaderboard (Free, Stratified Capital Tiers):**
   - Native tiers segmented by verified account equity: `leviathan` (`$50M+`), `super_whale` (`$1M–$50M`), plus optional `whale` / `small_whale` / `sniper` (`$50K–$1M`) with configurable per-tier Stage 1 quotas.
2. **Source 2 — Named Local Source Files (`config/sources/*.txt`, Free):**
   - Generalizes `config/seed_addresses.txt` into named cohort files (e.g., `config/sources/coinglass_leviathan.txt`, `config/sources/coinglass_super_whale.txt`, `config/sources/custom_watchlist.txt`).
   - Any address pasted from CoinGlass's web tracker, Hyperdash, or community lists enters Stage 2 scoring cleanly with provenance preserved in `sources: ["source:coinglass_leviathan"]`!
3. **Source 3 — Optional CoinGlass API V4 Adapter (Opt-In Env Secret):**
   - If `COINGLASS_API_KEY` is configured in GitHub Secrets / local environment, `hyperpod discover` queries `GET /api/hyperliquid/whale-position` (`OpenAPI v4`) and merges returned `data[].user` addresses with tag `"coinglass_api"`. When unset, skipped silently (`0` cost, `0` key required in repo).
4. **Source 4 — Free Native Expansion (`subAccounts` & Vault/Leader Discovery):**
   - Already-implemented `subAccounts` expansion (covering non-leaderboard subaccounts `>= $1M`) plus future public vault depositor/leader discovery (`v1.1`).

---

## 4. Recommended Timing (Two-Stage Rollout)

| Stage | Target Timing | Concrete Deliverables | Cost / Risk to v1.0 |
|---|---|---|---|
| **Stage A — Non-Breaking Schema & UI Switcher Foundation** | **Phases 5–7** *(v1.0)* | 1. Add optional `tier` (`"leviathan" \| "super_whale" \| ...`) and `sources: list[str]` fields to [`schemas/whitelist.schema.json`](../../schemas/whitelist.schema.json) (`schemaVersion: 1` preserved per `§11.2`) + Python/Dart models.<br>2. Tag v1 addresses with exact `tier` (`leviathan` `$50M+` vs `super_whale` `$1M–$50M`) and `sources` (`"leaderboard"`, `"seed"`, `"subAccount"`).<br>3. Wire Phase 5 `app/lib/domain/entities.dart` cohort filter + Phase 7 **Address Source / Capital Tier Selector** UI (`§8.2`). | **`0` extra API weight**, `0` delay to Phase 4 (`28–35h` bootstrap) or Phase 8 v1.0 launch; prevents frontend rewrite in v1.1. |
| **Stage B — External Sources & `$50K–$1M` Tiers** | **Phase 9.1** *(v1.1, post-v1.0 launch)* | 1. Support `config/sources/*.txt` named multi-file discovery.<br>2. Enable `$500K–$1M`, `$100K–$500K`, `$50K–$100K` leaderboard tiers with per-tier quotas (keeping `fast` `<= 550` wallets or adding a `30m–60m` cadence loop).<br>3. Add optional `COINGLASS_API_KEY` (`/api/hyperliquid/whale-position`) adapter when user supplies a CoinGlass API key. | Zero disruption to Phase 8 7-day v1.0 verification. |

*(Alternative: defer both Stage A and Stage B completely to Phase 9.1 if you prefer keeping Phases 5–7 minimal and updating the frontend cohort controls later in v1.1.)*

---

## 5. Confirmation Requested from User

1. Confirm whether to proceed with the **Two-Stage Rollout** (**Stage A** `tier`/`sources` hooks + UI selector in **Phases 5–7**, **Stage B** `config/sources/*.txt` + `$50K–$1M` quotas + optional `COINGLASS_API_KEY` adapter in **Phase 9.1**) or defer both to **Phase 9.1**.
2. Confirm proceeding with **Phase 4 (`4.1`–`4.6`)**.

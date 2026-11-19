<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Proposal 0003 — Multi-Tier Capital Cohorts & Behavioral Archetypes (`CoinGlass`-Style Address Sources)

- **Proposal:** `0003`
- **Date:** `2026-11-19`
- **Status:** `Proposed — Awaiting User Timing & Scope Approval`
- **Affects:** [`docs/implementation_plan.md`](../implementation_plan.md) `§3.1`, `§5.1`, `§5.5`, `§6`, `§7.1`, `§8.2`, `§9`

---

## 1. User Request & Target Taxonomy

Support selecting different **Capital Tiers** and **Behavioral Archetypes** (comparable to CoinGlass Hyperliquid Whale Tracker ranges `4`–`8`) as selectable address sources / cohorts in HyperPod:

| Tier Key | Display Label (`zh-TW`) | Account Value Range (USD) | CoinGlass Active Ref (`2026-11`) | Target Archetypes Referenced by User |
|---|---|---|---:|---|
| `leviathan` | 巨無霸 | `` `$50M – ∞` `` | `75` | `超級印鈔機`、`爆倉達人` |
| `super_whale` | 超級鯨魚 | `` `$1M – $50M` `` | `309` | `扛單狂人`、`套利高手`、`聰明錢`、`超級印鈔機`、`爆倉達人` |
| `whale` | 鯨魚 | `` `$500K – $1M` `` | `335` | `聰明錢` / Contrarian / Arbitrageur tags |
| `small_whale` | 小鯨魚 | `` `$100K – $500K` `` | `1,609` | `聰明錢` / Contrarian tags |
| `sniper` | 狙擊者 | `` `$50K – $100K` `` | `1,460` | `合約小白`、`割肉俠` |

### Quantitative Native Definitions of Behavioral Archetypes
Rather than scraping CoinGlass (which violates `AGENTS.md` #11 / bot-check rules and creates fragile third-party dependencies), HyperPod can classify each evaluated address **natively** from existing Hyperliquid Info API telemetry using `config/pipeline.yaml`-parameterized rules:
- **Positive / Smart Money Cohorts (v1+):**
  - `smart_money` (**聰明錢**): Passes Stage 2 quality gates (`profitFactorRobust >= 1.5`, `maxDrawdown <= 0.45`, non-HFT, non-MM, `nTrades >= 20`).
  - `money_printer` (**超級印鈔機**): Top percentile cumulative realization & monthly PnL (`allTime.pnl > threshold` & `pnlMonth > 0` within tier).
  - `arbitrageur` (**套利高手**): `fundingShare > 0.5` or `hedgeRatio > 0.8` (delta-neutral / funding-capture accounts already identified in `§5.4` metrics!).
- **Contrarian / Risk Cohorts (`v1.1 / Phase 9.1`):**
  - `liquidation_king` (**爆倉達人**): High self-liquidation ratio/count (`liquidation.liquidatedUser == addr`) or severe equity drawdown (`maxDrawdown > 0.70`).
  - `bagholder` (**扛單狂人**): Extreme asymmetry between losing trade hold duration (`medianLossHoldMinutes >> medianWinHoldMinutes`) and deep intra-trade/account drawdowns without realizing early stops.
  - `panic_seller` (**割肉俠**): Low win rate paired with disproportionately short losing trade hold times / high loss count.
  - `perp_novice` (**合約小白**): Strictly negative `allTime.pnl < 0` and `profitFactor < 1.0` with high turnover or short survival span in `$50K–$500K` tiers.

---

## 2. Live Hyperliquid Leaderboard Census & Rate-Limit Feasibility

Running `LeaderboardClient.fetch()` across the full `48,199`-row Hyperliquid S3 leaderboard (`2026-11-19`) yields the exact address counts per CoinGlass tier:

| Tier (`accountValue`) | Total Leaderboard Rows | `allTime.pnl > 0` | `allTime.pnl <= 0` | Current Stage 1 Logic Passes |
|---|---:|---:|---:|---:|
| `leviathan` (`$50M+`) | `40` | `33` | `7` | `29` |
| `super_whale` (`$1M – $50M`) | `2,342` | `1,775` | `567` | `1,665` |
| `whale` (`$500K – $1M`) | `1,843` | `1,331` | `512` | `1,251` |
| `small_whale` (`$100K – $500K`) | `9,101` | `6,697` | `2,404` | `6,299` |
| `sniper` (`$50K – $100K`) | `1,538` | `886` | `652` | `640` |
| **Subtotal (`$50K+`)** | **`14,864`** | **`10,722`** | **`4,142`** | **`9,884`** |
| `< $50K` *(out of scope)* | `33,335` | `10,867` | `22,468` | `7,903` |

*(Why CoinGlass shows `3,788` wallets total across Ranges 4–8 while Hyperliquid Leaderboard shows `14,864` accounts `>= $50K`: CoinGlass's Whale Tracker lists wallets with **currently open perp positions** or active tracked cohorts, whereas Hyperliquid's leaderboard spans all funded historical accounts.)*

### Why Naive Full-Pool Polling Violates `800 weight/min`
- **v1 Scope (`$1M+`, positive-PnL):** `1,694` Stage 1 candidates (`29 + 1,665`) → ~`150k` daily weight (~`3 hours` in GitHub Actions `daily`), leaving fast loop (`<= 600` tracked wallets) comfortably inside **`1–2 min` per 10-min cron**.
- **Naive `$50K+` expansion (`14,864` candidates incl. losing contrarians):**
  - Initial / daily Stage 2 scoring of `14,864` wallets (~`60–80` weight/wallet min) = **`~900k–1.2M` weight = `18–25 hours/day`** at `800/min` (overflows daily `300m` GitHub Actions job limit!).
  - 10-minute `fast` loop polling `3,788` active wallets (`>= 2–4` weight + fills per wallet ≈ `10,000–25,000` weight/run) = **`12–31 minutes` per tick**, exceeding the `10m` cron window and `7m` workflow timeout!

### Required Architecture Controls for Safe Multi-Tier Scaling
1. **Stratified Per-Tier Cap (or Dual-Cadence Fast Loop):**
   - Keep `fast` (`*/10 * * * *`) strictly **`<= 550` wallets total** (fitting `< 4 min` at `800 weight/min`), using a configurable per-tier & per-archetype quota in `config/pipeline.yaml` (e.g., all qualifying `$50M+`, top `250` `$1M–$50M`, top `60–80` each from `$500K–$1M`, `$100K–$500K`, `$50K–$100K` ranked by score/open position value, plus contrarian top-K). Optionally add a separate `30m`/`60m` `pipeline-medium.yml` loop for `$50K–$1M` if higher coverage is desired.
2. **Strict Cohort Isolation (`§7.1` Safety Guardrail):**
   - Contrarian / losing cohorts (`liquidation_king`, `bagholder`, `panic_seller`, `perp_novice`, `arbitrageur`) **MUST NEVER mix into the default `Smart Money` `#4` Consensus Index or `#3`/`#5` Long/Short metrics** unless the user explicitly toggles those tiers/archetypes in the frontend Cohort Selector!
3. **Zero External Dependencies / Optional Manual Cohort Lists:**
   - Primary classification uses Hyperliquid's free Info API + Leaderboard directly (`AGENTS.md` #11 compliant).
   - Optionally support `config/cohorts/<name>.txt` (same format as `seed_addresses.txt`) so the user can paste specific addresses discovered on CoinGlass into named cohorts cleanly.

---

## 3. Recommended Timing (When to Build Each Piece)

Implementing all `$50K–$1M` tiers and contrarian classifiers right now in Phase 3/4 would slow down local bootstrap (`28–35h` → `>100h`), destabilize `§6.3` fast-loop latency budgets, and add unscheduled work ahead of Phase 8's 7-day stability goal. Conversely, building Phases 5–7 with zero `tier`/`archetypes` extensibility hooks would force a breaking frontend refactor later.

### Recommended Two-Stage Rollout

| Stage | When | What Changes | API / Schedule Cost |
|---|---|---|---|
| **Stage A — Zero-Cost Schema & UI Hooks (v1.0)** | **Phases 5–7** *(autonomous schema optional fields + UI controls; see `§11.2`)* | 1. Add optional non-breaking fields `tier` (`"leviathan" \| "super_whale" \| ...`) and `archetypes` (`list[str]`) to [`schemas/whitelist.schema.json`](../../schemas/whitelist.schema.json) (`schemaVersion: 1` preserved) + Python/Dart models.<br>2. Tag v1's `$1M+` addresses natively (`leviathan >= $50M`, `super_whale $1M–$50M`, `smart_money`, `money_printer`, `arbitrageur` candidates when tracked).<br>3. Design Phase 5 `app/lib/domain/` (`entities.dart` cohort filter `§7.1`) and Phase 7 UI control (`controls/` cohort picker + badge) to filter natively by `tier` and `archetypes`. | **`0` extra API weight**, `0` extra runtime; protects Phase 4 `28–35h` bootstrap & Phase 8 v1.0 release timeline. |
| **Stage B — Full `$50K–$1M` & Contrarian Expansion (v1.1)** | **Phase 9.1** *(immediately after Phase 8 v1.0 launch)* | 1. Enable `$500K–$1M`, `$100K–$500K`, `$50K–$100K` Stage 1 pipelines with per-tier caps in `config/pipeline.yaml`.<br>2. Add contrarian archetype classifiers (`liquidation_king`, `bagholder`, `panic_seller`, `perp_novice`) using separate contrarian scoring criteria (`allTime.pnl <= 0` allowed in isolated contrarian tracks) and `config/cohorts/*.txt` support.<br>3. Deploy stratified `fast` quota or `pipeline-medium.yml` (`30m–60m`) + isolated `cohorts/` UI preset chips (`聰明錢` vs `爆倉達人` / `扛單狂人` / `割肉俠` / `巨無霸`). | Bounded within `800 weight/min` CI limits via stratified quotas / dual cadence. |

### Alternative: Defer Completely to Phase 9.1 (v1.1)
Leave `whitelist.schema.json` and Phases 5–7 completely unchanged for v1.0, and add both the `tier`/`archetypes` schema fields and the multi-tier pipeline together in Phase 9.1. *(Simpler for Phases 5–7, but requires revisiting all 7 Phase 6–7 charts & `entities.dart` during v1.1.)*

---

## 4. Decision Requested from User

1. Confirm whether to adopt the **Recommended Two-Stage Rollout** (**Stage A** non-breaking `tier`/`archetypes` hooks in **Phases 5–7**; **Stage B** full `$50K–$1M` + contrarian archetypes in **Phase 9.1** right after v1.0) or defer everything to Phase 9.1.
2. Note: [`docs/implementation_plan.md`](../implementation_plan.md) and production code remain untouched until your confirmation (`§11.2`).

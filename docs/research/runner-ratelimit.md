<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Task 0.6 — GitHub Actions Runner Rate-Limit & Latency Probe

## Test Setup
- **Workflow**: Temporary `.github/workflows/temp-ratelimit-probe.yml` (executed on GitHub-hosted `ubuntu-latest` / `ubuntu-26.04` image `20261111.196.1`, Azure region `eastus2`, and removed immediately after capture).
- **Run ID**: `43681945367` (`headSha`: `0534310ddfa73454483e08a5db2e711de1cb9de7`, completed `2026-11-15T05:38:58Z`, conclusion `success`).
- **Target**: `POST https://api.hyperliquid.xyz/info` with payload `{"type": "clearinghouseState", "user": "0xfc27136e42af1732ddc9ce2605ea9bff1b959d9d"}` (`weight = 2` per request; 200 total requests = `400` weight).
- **Command used to retrieve verbatim logs**:
  ```bash
  gh run view 43681945367 --log
  ```

## Verbatim Runner Output
```text
probe	Probe 100x clearinghouseState (sequential and 4-way concurrent)	2026-11-15T05:38:58.5023425Z === Sequential 100x clearinghouseState ===
probe	Probe 100x clearinghouseState (sequential and 4-way concurrent)	2026-11-15T05:38:58.5024126Z Total requests: 100, wall time: 20.161s, req/s: 4.96
probe	Probe 100x clearinghouseState (sequential and 4-way concurrent)	2026-11-15T05:38:58.5024444Z Statuses: {'200': 100}
probe	Probe 100x clearinghouseState (sequential and 4-way concurrent)	2026-11-15T05:38:58.5024721Z Cf-Pops: {'IAD61-P5': 100}
probe	Probe 100x clearinghouseState (sequential and 4-way concurrent)	2026-11-15T05:38:58.5025076Z Latency ms: min=176.5, p50=188.8, p90=229.9, p95=249.7, p99=350.4, max=425.2, mean=201.6
probe	Probe 100x clearinghouseState (sequential and 4-way concurrent)	2026-11-15T05:38:58.5025503Z === Concurrent (4 workers) 100x clearinghouseState ===
probe	Probe 100x clearinghouseState (sequential and 4-way concurrent)	2026-11-15T05:38:58.5025859Z Total requests: 100, wall time: 5.115s, req/s: 19.55
probe	Probe 100x clearinghouseState (sequential and 4-way concurrent)	2026-11-15T05:38:58.5026583Z Statuses: {'200': 100}
probe	Probe 100x clearinghouseState (sequential and 4-way concurrent)	2026-11-15T05:38:58.5026847Z Cf-Pops: {'IAD61-P5': 100}
probe	Probe 100x clearinghouseState (sequential and 4-way concurrent)	2026-11-15T05:38:58.5027189Z Latency ms: min=176.5, p50=190.4, p90=235.6, p95=288.4, p99=333.1, max=377.3, mean=203.2
```

## Summary & Operational Conclusions
| Mode | Requests | Total Weight | Wall Time | Throughput | HTTP 200 | HTTP 429 | Cf-Pop | p50 / p95 / max Latency |
|---|---|---|---|---|---|---|---|---|
| Sequential (1 worker) | 100 | 200 | 20.161s | 4.96 req/s | 100 | 0 | `IAD61-P5` | 188.8 / 249.7 / 425.2 ms |
| Concurrent (4 workers) | 100 | 200 | 5.115s | 19.55 req/s | 100 | 0 | `IAD61-P5` | 190.4 / 288.4 / 377.3 ms |

1. **4-way concurrency is safe and effective**: Moving from sequential (`19.8 weight/s`) to 4 workers (`39.1 weight/s` for `clearinghouseState`) gave a `3.94×` speedup (`20.161s → 5.115s`) with zero `429` responses and nearly identical tail latency (`p50`: `188.8 ms` vs `190.4 ms`).
2. **Fast loop timing validation (§6.3)**: At `~203 ms` average round-trip with 4 workers, `200` `clearinghouseState` calls take `~10.2s` network time (plus `metaAndAssetCtxs` and ~40 incremental `userFillsByTime` calls), well within the `7`-minute `pipeline-fast.yml` timeout and `800 weight/min` CI rate-limit budget.
3. **Shared runner IP caution**: Although `0` `429`s occurred across 200 requests (`400` weight), Azure `eastus2` runners route through `IAD61-P5` where other GitHub Actions jobs can occasionally share egress IPs; keeping the `800 weight/min` budget (`2/3` of `1,200`) plus `2^k` exponential backoff + halving on consecutive `429`s (`§6.1`) remains essential.

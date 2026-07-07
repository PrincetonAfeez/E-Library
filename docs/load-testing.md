# Load & Capacity Testing

The core read path (catalog search) is the highest-traffic surface and the one to
size against. Two harnesses:

- **HTTP load** — `scripts/load/search_smoke.js` ([k6](https://k6.io)) drives the
  live endpoint at concurrency; run against staging (below).
- **Core-path capacity** — `scripts/load/bench_search.py` times the search query
  path (FTS + trigram + facets + serialization) directly against a seeded
  PostgreSQL DB, isolating the work that determines capacity from web-tier noise.

## Recorded results

### Core-path benchmark (2026-07-06)
`scripts/load/bench_search.py`, 200-work dataset, 500 requests, local single
PostgreSQL 18, locmem cache:

| Scenario | p50 | p95 | p99 |
|----------|-----|-----|-----|
| single-thread | 15.6 ms | 26.3 ms | 33.1 ms |
| concurrent ×8 | 70.5 ms | 140.6 ms | 174.5 ms |

Throughput ≈ 87 searches/s at 8 workers. **p95 is well under the 400 ms SLO**
(`docs/policies/slos.md`) even at 8× concurrency on a single local DB. Re-run
against a production-sized dataset + managed DB to size horizontally.

## Run
```bash
# Ramp to 50 virtual users against a staging instance (search -> click session):
BASE_URL=https://staging.example.test ORG=metro-library k6 run scripts/load/search_smoke.js
```
The script tags metrics per endpoint (`search_duration`, `detail_duration`) and
asserts p95 < 400 ms with < 0.1% failures.

> **Throttling:** the API rate-limits anonymous callers (`THROTTLE_ANON`, default
> 120/min). For a real load test, raise it on the staging instance for the load
> source — e.g. `THROTTLE_ANON=100000/minute` — otherwise you measure the rate
> limiter, not the app. Rates are env-configurable (`elibrary/settings.py`).

## Pass criteria (align with docs/policies/slos.md)
- p95 `/api/v1/catalog/search` < 400 ms at target concurrency.
- HTTP failure rate < 0.1%.
- No sustained growth in DB connections or memory (no leak/backlog).

## What to watch while running
- DB: slow queries (`SLOW_QUERY_MS`), connection count, CPU.
- App: p95/p99 latency, 5xx rate.
- Async: outbox lag and `dead_letter_backlog_high` (a load test that creates
  loans/holds exercises the notification outbox).

## Capacity notes
- Search is backed by a GIN-indexed `WorkSearchDocument` + trigram; it should scale
  read-mostly. Semantic search loads up to 1000 embeddings per call and is
  rate-limited for anonymous callers — load-test it separately if exposed.
- Record results (date, commit, VUs, p95, error rate) with each release that
  touches the read path.

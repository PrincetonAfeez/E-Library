# Load & Capacity Testing

The core read path (catalog search) is the highest-traffic surface and the one to
size against. A [k6](https://k6.io) smoke/load script lives at
`scripts/load/search_smoke.js`.

## Run
```bash
# Ramp to 50 virtual users against a staging instance:
BASE_URL=https://staging.example.test k6 run scripts/load/search_smoke.js
```

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

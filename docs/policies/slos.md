# Service Level Objectives

Measured over a rolling 30-day window. Breaching the error budget freezes
non-critical releases until recovered.

| Service | SLI | Objective |
|---------|-----|-----------|
| Catalog search (`/api/v1/catalog/search`) | p95 latency | < 400 ms |
| Catalog search | availability (non-5xx) | 99.9% |
| Circulation (borrow/return/hold) | p95 latency | < 600 ms |
| Circulation | success rate | 99.9% |
| Auth (login) | p95 latency | < 800 ms |
| Overall API | availability | 99.9% (≈43 min/month budget) |
| Async delivery (outbox → notify/webhook) | processed within | 5 min p99 |

## Measurement
- Latency/availability from the reverse proxy or APM; async lag from
  `OutboxEvent.created_at → processed_at`.
- Dashboards and alerts described in `../runbooks/monitoring.md`.

## Error budget policy
- < 50% budget consumed: normal.
- ≥ 50%: prioritize reliability work.
- Exhausted: freeze feature releases; only reliability/security fixes ship.

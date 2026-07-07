# Monitoring & Observability

## Health endpoints
- `GET /healthz` — liveness (process up). Exempt from HTTPS redirect.
- `GET /readyz` — readiness; checks DB and cache, returns component names only
  (never internal detail). 503 when a dependency is down.
- `GET /status/` — public component status page.

## Uptime monitoring (must be wired in prod)
- Point an external monitor (UptimeRobot / Pingdom / Better Stack / Grafana
  Synthetic) at `/readyz` on a 1-minute interval.
- Alert a human on 2 consecutive failures via the paging tool (see
  `incident-response.md`). This is an operational step, not code.

## Logs
- Structured JSON in deployed envs (`LOG_FORMAT=json`), one object per line with
  `request_id` for correlation. Set `LOG_FORMAT=text` for local readability.
- Ship stdout to your log aggregator (Loki/CloudWatch/Datadog). Query by
  `request_id` to trace a single request end-to-end.
- Slow SQL: set `SLOW_QUERY_MS=200` to log queries over the threshold.

## Error tracking
- Set `SENTRY_DSN` to enable Sentry (PII scrubbing on: `send_default_pii=False`).
  Without a DSN, errors go to logs only.

## Async / queue health
- `run_sweeps` emits `dlq_outbox_failed` / `dlq_webhook_failed` counts and logs
  `dead_letter_backlog_high` past threshold — alert on that log line.
- Outbox worker (`drain_outbox`) and scheduler run as separate containers; the
  scheduler self-serializes via a Postgres advisory lock so multiple instances
  can't double-run a sweep.

## Suggested alerts
- `/readyz` down 2m · Sentry error rate spike · `dead_letter_backlog_high`
  · DB connections/CPU/disk · p95 latency over SLO (see `../policies/slos.md`).

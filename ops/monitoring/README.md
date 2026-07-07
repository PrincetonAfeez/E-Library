# Monitoring options

Two ready paths for uptime alerting (see also `docs/runbooks/monitoring.md`):

## 1. Built-in monitor (simplest)
The `monitor` service in `docker-compose.yml` polls `/readyz` every 60s, logs
failures, and POSTs to `ALERT_WEBHOOK_URL` (Slack/Teams/generic) after 2
consecutive failures. For an external scheduler use `scripts/uptime_check.sh`
from cron.

## 2. Prometheus + Blackbox (production-grade)
Point `blackbox_exporter` at `/readyz`, scrape it with Prometheus, and load
`alert.rules.yml` into Alertmanager routed to your pager. Example scrape:

```yaml
scrape_configs:
  - job_name: elibrary-readyz
    metrics_path: /probe
    params: { module: [http_2xx] }
    static_configs:
      - targets: ["https://app.example.test/readyz/"]
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox-exporter:9115
```

Either way, alerts must page a human — wire the destination in your paging tool
per `docs/runbooks/incident-response.md`.

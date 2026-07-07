#!/usr/bin/env sh
# One-shot health check for cron/external schedulers. Exits non-zero on failure
# and (optionally) posts an alert to ALERT_WEBHOOK_URL.
#   HEALTH_URL=https://app.example.test/readyz/ ALERT_WEBHOOK_URL=... ./scripts/uptime_check.sh
set -eu

URL="${HEALTH_URL:-http://localhost:8000/readyz/}"
WEBHOOK="${ALERT_WEBHOOK_URL:-}"

if curl -fsS -m 10 "$URL" >/dev/null 2>&1; then
  echo "OK $URL"
  exit 0
fi

echo "DOWN $URL"
if [ -n "$WEBHOOK" ]; then
  curl -fsS -m 10 -X POST -H "Content-Type: application/json" \
    -d "{\"text\":\"E-Library DOWN: $URL\"}" "$WEBHOOK" || true
fi
exit 1

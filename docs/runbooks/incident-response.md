# Incident Response Runbook

## Severity
- **SEV1** — customer data exposure/loss, cross-tenant access, full outage, auth
  bypass. Page immediately.
- **SEV2** — core workflow broken for many tenants (checkout/holds/billing), partial
  outage. Page during business hours; escalate if unresolved in 1h.
- **SEV3** — degraded/non-core feature, elevated errors within SLO budget.

## On-call & escalation
- Primary on-call carries the pager 24/7 on a weekly rotation; secondary is backup.
- Escalation: primary → secondary (15 min no-ack) → engineering lead → founder.
- Maintain the rotation and contact tree in your paging tool (PagerDuty/Opsgenie).
  _This repo documents the process; the live rotation lives in the paging tool._

## Detection
- Alerts: uptime monitor on `/readyz`, Sentry error-rate spike, `run_sweeps`
  `dead_letter_backlog_high` warning, host/DB metrics. See `monitoring.md`.

## Response steps
1. **Acknowledge** the page; declare severity; open an incident channel.
2. **Communicate** — post to the status page (`/status/`) for SEV1/SEV2.
3. **Mitigate before root-cause** — roll back to the previous image tag
   (`git revert` / redeploy prior release), disable the offending path, or scale.
4. **Correlate** using the `X-Request-ID` / `request_id` log field.
5. **Recover** — if data is affected, follow `disaster-recovery.md`.
6. **Resolve & verify** `/readyz`, then update the status page.

## After the incident
- Blameless postmortem within 3 business days: timeline, impact, root cause,
  action items with owners. File action items as tracked issues.

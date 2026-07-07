# Compliance Control Mapping

Status legend: **impl** (implemented in code) · **process** (operational, outside
the repo) · **partial**.

## GDPR
| Requirement | Status | Where |
|-------------|--------|-------|
| Right to access / data portability | impl | `/account/export/`, `api/v1/account/export/` (`library/privacy.py`) |
| Right to erasure | impl | erasure/anonymization in `library/privacy.py`; returned-loan history anonymized by default |
| Data minimization | impl | patron identity dropped from history on return; `send_default_pii=False` (Sentry) |
| Lawful processing / consent | partial | Terms + Privacy shown at signup (`/terms/`, `/privacy/`); DPA templates = process |
| Breach notification | process | `runbooks/incident-response.md` (72h regulator notice = process) |
| Records of processing / subprocessors | impl(list) | `subprocessors.md` |
| Retention limits | impl | `prune_logs` + `data-retention.md` |

## SOC 2 (Trust Services Criteria) — readiness map
| Criterion | Status | Where |
|-----------|--------|-------|
| CC6 Logical access | impl | RBAC, scoped tokens, staff MFA, password policy |
| CC7 Monitoring | partial | health/readiness + structured logs + Sentry hook; SIEM/alerting = process |
| CC7 Change management | partial | CI + migrations + ADRs; approvals/segregation = process |
| A1 Availability | partial | backups + DR runbook + SLOs; tested-restore cadence = process |
| C1 Confidentiality | impl | TLS/HSTS, encrypted TOTP secrets, tenant isolation |
| PI1 Processing integrity | impl | transactional services, row locking, idempotent outbox |

## Not claimed
HIPAA and PCI are **out of scope**. Card data is never stored — only brand/last4
via the payment provider (`library/models.PaymentMethod`); no PAN touches the app.

## Evidence set (assemble for an audit — process)
Pen-test report, restore-drill log, access-review records, vendor DPAs, and the
CI/deploy history (now under version control).

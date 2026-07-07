# Data Retention & Residency

## Retention
| Data | Retention | Mechanism |
|------|-----------|-----------|
| Returned-loan patron identity | anonymized on return (immediate) | `services.return_loan` privacy scrub (unless patron opts into history) |
| Audit logs | configurable window | `manage.py prune_logs` (scheduled daily) |
| Search query logs | configurable window | `prune_logs --search-days` |
| Notification deliveries | configurable window | `prune_logs` |
| Domain events / outbox | pruned after processing + retention | `prune_logs` |
| Orphaned digital blobs | 24h grace then removed | `delivery.prune_orphan_blobs` (sweep) |
| DB backups | ≥30 days off-host; local 14 days | `scripts/backup.sh` (`RETENTION_DAYS`) |

Tune windows via the `prune_logs` arguments in the scheduler (`docker-compose.yml`).

## Residency
- Single-region by default (see README accepted limitations). All customer data
  lives in the configured Postgres region; backups ship to storage in the same
  region unless a customer contract requires otherwise.
- Multi-region / customer-pinned residency is not implemented; treat any such
  commitment as a per-contract operational decision.

## Deletion on account/tenant close
- Patron self-deletion follows the erasure path (`library/privacy.py`).
- Tenant offboarding: export the tenant's data, then delete the `Organization`
  (cascades to org-scoped rows). Confirm backups age out per the retention window.

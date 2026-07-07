# Disaster Recovery Runbook

## Targets
- **RPO (max data loss):** 15 minutes — achieved with continuous WAL archiving / a
  managed-Postgres PITR tier, or 24h with the daily logical dump below.
- **RTO (max downtime):** 1 hour for a full region-loss restore.

## What is backed up
- **PostgreSQL** — the system of record (catalog, tenants, circulation, billing,
  digital-content blobs in `library_storedblob`). This is the only stateful store
  that must be recovered; Redis is a cache and is safe to lose.

## Backups
Logical dumps (baseline):
```bash
DATABASE_URL=postgres://… ./scripts/backup.sh          # writes ./backups/elibrary-<ts>.dump
# or, with the demo stack:
docker compose --profile backup run --rm backup
```
- Schedule hourly/daily off the app host; ship dumps to durable, off-host,
  versioned object storage with its own retention (≥30 days).
- Production should additionally enable **PITR** (managed Postgres or WAL-G) to
  hit the 15-minute RPO; logical dumps alone give a 24h RPO.

## Restore (tested procedure)
```bash
DATABASE_URL=postgres://…/elibrary_restore ./scripts/restore.sh backups/elibrary-<ts>.dump
python manage.py migrate            # apply any newer migrations
python manage.py check
# smoke test: hit /readyz (200) and sign in as a seeded user
```

## Restore drill (must be run — an untested backup is not a backup)
- **Cadence:** monthly. Restore the latest dump into a scratch database, run
  `migrate` + `pytest -q` against it, confirm `/readyz` is green, and record the
  wall-clock restore time.
- Log each drill (date, dump timestamp, restore duration, pass/fail) in this file's
  drill log below.

## Region-loss playbook
1. Provision Postgres in the standby region; restore latest dump/PITR.
2. Deploy the app image (same tag) pointing `DATABASE_URL` at the restored DB.
3. Re-point DNS; verify `/readyz`, auth, and a borrow/return.
4. Announce on the status page; open an incident (see `incident-response.md`).

## Drill log
| Date | Dump timestamp | Restore duration | Result |
|------|----------------|------------------|--------|
| 2026-07-06 | 20260707T004618Z | 4s (empty schema + marker) | PASS — `scripts/backup.sh` → drop DB → `scripts/restore.sh`; marker row verified present after restore |

> Note: the first drill used a freshly migrated schema with a marker row to prove
> the backup/restore *path* end-to-end. Re-run monthly against a production-sized
> snapshot to validate the RTO target under realistic data volume.

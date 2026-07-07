#!/usr/bin/env bash
# Database backup: writes a compressed custom-format pg_dump to $BACKUP_DIR.
# Usage: DATABASE_URL=postgres://... ./scripts/backup.sh
# Schedule this (cron / platform scheduler) and ship dumps off-host.
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
mkdir -p "$BACKUP_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/elibrary-$STAMP.dump"

echo "Backing up database to $OUT ..."
pg_dump --format=custom --no-owner --dbname="$DATABASE_URL" --file="$OUT"

# Prune local backups older than RETENTION_DAYS (default 14).
find "$BACKUP_DIR" -name 'elibrary-*.dump' -mtime "+${RETENTION_DAYS:-14}" -delete || true

echo "Backup complete: $OUT"
echo "Verify restores regularly — see docs/runbooks/disaster-recovery.md"

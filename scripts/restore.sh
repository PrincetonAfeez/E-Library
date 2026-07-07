#!/usr/bin/env bash
# Restore a pg_dump produced by scripts/backup.sh.
# Usage: DATABASE_URL=postgres://... ./scripts/restore.sh <dumpfile>
# WARNING: --clean drops and recreates objects in the target database.
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"
FILE="${1:?usage: restore.sh <dumpfile>}"

echo "Restoring $FILE into the target database ..."
pg_restore --clean --if-exists --no-owner --dbname="$DATABASE_URL" "$FILE"

echo "Restore complete. Run 'python manage.py migrate' and smoke-test the app."

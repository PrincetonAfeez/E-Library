#!/usr/bin/env sh
# Run the same gates CI runs, locally. Requires the venv active and
# DATABASE_URL / CACHE_URL set (Postgres + Redis reachable) for the test step.
set -e

echo "==> ruff"
ruff check .

echo "==> migration drift"
python manage.py makemigrations --check --dry-run

echo "==> system check"
python manage.py check

echo "==> tests"
pytest -q

echo "All CI gates passed."

#!/usr/bin/env sh
# Run the same gates CI runs, locally. Requires the venv active and
# DATABASE_URL / CACHE_URL set (Postgres + Redis reachable) for the test step.
set -e

# Git hooks don't inherit an activated venv; prefer project tools when present.
if [ -d ".venv/bin" ]; then
  PATH="$(pwd)/.venv/bin:$PATH"
elif [ -d ".venv/Scripts" ]; then
  PATH="$(pwd)/.venv/Scripts:$PATH"
fi
export PATH

echo "==> ruff"
ruff check .

echo "==> bandit (SAST)"
bandit -c pyproject.toml -r library elibrary --severity-level medium --confidence-level medium -q

echo "==> pip-audit (dependency CVEs)"
pip-audit -r requirements.txt

echo "==> migration drift"
python manage.py makemigrations --check --dry-run

echo "==> system check"
python manage.py check

echo "==> OpenAPI schema"
python manage.py spectacular --file /tmp/schema.yml

echo "==> mypy"
mypy --follow-imports=skip library/crypto.py library/entitlements.py library/net.py library/mfa.py library/sso.py library/sip2.py

echo "==> tests"
pytest -q

echo "All CI gates passed."

# E-Library

Multi-tenant Django + HTMX library SaaS (ILS) based on `E-Library.md`.

## Continuous integration

The full pipeline (ruff → bandit SAST → pip-audit → migration-drift → system
check → pytest) runs two ways, from the same steps:

- **Locally, on every push** — `.githooks/pre-push` runs `scripts/ci.sh`
  (enable once: `git config core.hooksPath .githooks`). Run it anytime with
  `sh scripts/ci.sh`.
- **Hosted, on every merge** — `.github/workflows/ci.yml` runs on push/PR the
  moment the repo has a remote:
  ```bash
  gh repo create <org>/e-library --private --source=. --remote=origin --push
  ```
  Add a status badge once connected:
  `![CI](https://github.com/<org>/e-library/actions/workflows/ci.yml/badge.svg)`

The build includes a shared bibliographic spine (`Work`, `Edition`, `Author`, `Subject`), tenant-scoped holdings and circulation (`Organization`, `Branch`, `Copy`, `Loan`, `Hold`), PostgreSQL search documents, transactional circulation services, HTMX catalog/account/librarian pages, DRF APIs, an outbox worker, scheduled sweeps, seed data, and ADRs.

## Feature modules

Beyond core catalog + circulation, the app ships these subsystems (all runnable
offline — external providers use injectable no-op fallbacks unless configured):

- **Billing & subscriptions** — plans, simulated hosted checkout, cards on file, invoices, proration, dunning/auto-renewal, refunds, payment plans, fine amnesty, GL export, fund encumbrance.
- **Digital lending** — license-based ebook/audio loans + holds, an in-browser reader/player with signed short-lived content tokens and social-DRM watermarking, reading-progress sync.
- **Discovery** — Postgres FTS + trigram, autocomplete, did-you-mean, and local-embedding semantic search / "more like this".
- **Consortia** — union catalog + inter-library loan (ILL) lifecycle and floating collections.
- **Notifications** — multi-channel (email/SMS/push) via a transactional outbox, escalating cadences, per-category preferences, and one-click unsubscribe.
- **Staff workflows** — bulk copy ops, barcode inventory/stocktake, lost/claims-returned/damaged handling, acquisitions.
- **Analytics** — collection turnover, holds-to-copies purchase suggestions, circulation time series, BI export, audit-log viewer.
- **Enterprise** — org-scoped RBAC + scoped API tokens, staff TOTP MFA (optionally enforced per-org), SSO (OIDC), GDPR export/erasure, outbound signed webhooks, SIP2 self-check, i18n (en/es/fr), a public status page.

## Operations & policies

Runbooks and policies live in [`docs/`](docs/): disaster recovery (RTO/RPO,
backup/restore), incident response, monitoring, staging, SLOs, security,
compliance mapping, data retention/residency, subprocessors, API versioning,
caching, SSO, and load testing. ADRs are in [`docs/adrs/`](docs/adrs/).

Backups: `scripts/backup.sh` / `scripts/restore.sh` (or `docker compose --profile backup run --rm backup`). Monthly restore drill: `python scripts/restore_drill.py` (see `docs/runbooks/disaster-recovery.md`).

## Run with Docker

```bash
docker compose up --build
```

Then open `http://localhost:8000`.

Demo users:

```text
admin / demo12345
librarian / demo12345
patron / demo12345
```

## Local Development

PostgreSQL and Redis are expected. Copy `.env.example` to `.env` and adjust hostnames when running outside Docker:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
python manage.py migrate
python manage.py seed_demo --works 96
python manage.py runserver
```

Useful commands:

```bash
python manage.py rebuild_search_index
python manage.py drain_outbox --once
python manage.py run_sweeps
python manage.py issue_api_token patron --org metro-library --scope patron:read --scope circulation:write
```

API entry points:

```text
GET  /api/v1/catalog/search/?q=archive
GET  /api/v1/catalog/works/<slug>/
POST /api/v1/catalog/works/<slug>/borrow/
POST /api/v1/catalog/works/<slug>/hold/
GET  /api/docs/
```

## Architecture Notes

The code keeps web/API surfaces thin. Business mutations live in `library/services.py`; complex reads live in `library/selectors.py`; opaque cursors live in `library/pagination.py`; token auth is in `library/auth.py`.

The implementation is intentionally a runnable foundation, not a claim that every line of the enterprise brief is complete. The core concurrency-sensitive flows are represented: last-copy borrowing is row-locked, ready holds reserve a specific copy, returned/expired copies are reoffered under a per-work advisory lock on PostgreSQL, and returned loans anonymize patron identity by default.


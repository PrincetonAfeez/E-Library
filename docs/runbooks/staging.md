# Staging & Pre-Prod Verification

## Staging environment
Run a staging deploy that mirrors production configuration (same image tag,
`DEBUG=False`, real `SECRET_KEY`, managed Postgres + Redis, `SECURE_SSL_REDIRECT=True`)
against a non-production database. Promote the exact image that passed CI.

## Pre-prod verification checklist (gate before promoting to prod)
1. CI green on the commit (ruff, migration-drift check, `manage.py check`, pytest).
2. `python manage.py migrate` runs cleanly from the current prod schema.
3. `/readyz` returns 200; `/healthz` 200.
4. Smoke: sign up → borrow → return; place + fulfill a hold; a billing checkout.
5. A restore drill has passed within the last month (`disaster-recovery.md`).
6. Rollback plan confirmed: previous image tag is available and deployable.

## Promotion
Promote by re-pointing prod to the verified image tag. Roll back by redeploying
the prior tag; DB migrations follow expand/contract so the prior image stays
compatible across a single deploy.

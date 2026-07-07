# Security Policy & Review

## Controls in place (verifiable in code)
- **Transport:** `SECURE_SSL_REDIRECT` + HSTS (preload, subdomains) when not DEBUG;
  Secure/HttpOnly session & CSRF cookies.
- **Passwords:** Django PBKDF2 (per-user salt); self-service reset with single-use,
  time-limited tokens; per-IP rate limits on login and reset.
- **MFA:** TOTP (RFC 6238) for staff; secrets encrypted at rest (`library/mfa.py`);
  optionally enforced per-tenant via `Organization.require_staff_mfa` + middleware.
- **AuthZ:** server-side, org-scoped RBAC on every protected route
  (`library/permissions.py`); scoped API tokens (`library/auth.py`).
- **Tenant isolation:** all tenant data queried through org-scoped lookups; covered
  by cross-tenant tests (`test_round7`, `test_consortia`).
- **Headers/CSP:** `django-csp` policy; clickjacking + CORS allowlist.
- **Input:** DRF validation → 4xx; upload size + type limits; CSV-injection escaping
  on exports; opaque cursors.
- **Secrets:** injected via env; none in source; `.env` gitignored.

## Security review process
- **Automated SAST** — `bandit` runs in CI at medium+ severity/confidence over
  `library` + `elibrary` (config in `pyproject.toml`). Locally: `bandit -c
  pyproject.toml -r library elibrary --severity-level medium --confidence-level medium`.
- **Dependency CVEs** — `pip-audit -r requirements.txt` runs in CI; Dependabot
  (`.github/dependabot.yml`) opens update PRs weekly.
- Run the repo's `security-review` pass on the branch diff before merge.

### Last scan (2026-07-06)
`bandit`: 0 High / 0 Medium in application code (679 Low are test asserts,
excluded via `[tool.bandit]`). `pip-audit`: no known vulnerabilities.
Findings fixed in this pass:
- `search._stable_hash` MD5 marked `usedforsecurity=False` (non-crypto feature hash).
- Outbound URLs validated to http(s) before `urlopen` (`library/net.py`) — blocks
  `file:`/custom-scheme SSRF on tenant-configured webhook targets.
- MARCXML parsing hardened with `defusedxml` (blocks XXE / entity-expansion).
- SIP2 server now binds `127.0.0.1` by default (all-interfaces requires `--host`).

## Penetration testing (operational — cannot be satisfied in-repo)
- Commission an independent pen test before GA and annually thereafter, plus after
  major auth/tenant-model changes.
- Scope: authn/z, tenant isolation, IDOR on org-scoped resources, token/webhook
  surfaces, the signed digital-content URLs, and the admin site.
- Track findings to closure here; attach the report to the compliance evidence set.

## Reporting
Publish a `security.txt` / disclosure contact before GA and triage reports on a
documented SLA.

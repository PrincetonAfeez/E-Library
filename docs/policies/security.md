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
- Run the automated review on each change: `ruff` + the repo's `security-review`
  pass on the branch diff before merge.
- Dependency CVEs: Dependabot (`.github/dependabot.yml`) weekly; the pinned
  `requirements.txt` makes advisories actionable.

## Penetration testing (operational — cannot be satisfied in-repo)
- Commission an independent pen test before GA and annually thereafter, plus after
  major auth/tenant-model changes.
- Scope: authn/z, tenant isolation, IDOR on org-scoped resources, token/webhook
  surfaces, the signed digital-content URLs, and the admin site.
- Track findings to closure here; attach the report to the compliance evidence set.

## Reporting
Publish a `security.txt` / disclosure contact before GA and triage reports on a
documented SLA.

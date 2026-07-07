# API Versioning & Deprecation Policy

## Versioning
- The public REST API is URL-versioned: `/api/v1/...`.
- The OpenAPI schema is generated from code (`drf-spectacular`) at
  `/api/schema/` with interactive docs at `/api/docs/` — so docs cannot drift
  from the implementation.

## Compatibility rules (within a version)
Backward-compatible changes may ship without a version bump: adding endpoints,
adding optional request fields, adding response fields. Clients must ignore
unknown response fields.

## Breaking changes
Removing/renaming fields or endpoints, changing types, or tightening validation
require a **new version** (`/api/v2/`). Breaking changes never land in an existing
version.

## Deprecation
- Announce deprecation in release notes and the schema description.
- Send `Deprecation` and `Sunset` response headers on deprecated endpoints.
- Minimum **90-day** overlap where both versions run before removal.

## Authentication & limits
Every endpoint requires auth except the public catalog read/search surface. Tokens
are scoped (`library/auth.py`); rate limits return `429` (DRF throttles + per-IP
limits on the expensive search paths).

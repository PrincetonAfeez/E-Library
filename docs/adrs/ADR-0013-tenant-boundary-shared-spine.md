# ADR-0013: Tenant Boundary And Shared Spine

## Status

Accepted.

## Decision

`Organization` is the tenant boundary. Bibliographic records are platform-global; holdings, circulation, patrons, policies, audit, analytics, and imports are tenant-scoped.

## Consequences

Bibliographic authority control and deduplication happen once, while patron and inventory data remain organization-isolated.


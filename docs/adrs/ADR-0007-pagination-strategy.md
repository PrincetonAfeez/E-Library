# ADR-0007: Pagination Strategy

## Status

Accepted.

## Decision

The web UI supports shallow offset-style browsing while API and infinite-scroll flows use opaque signed cursors.

## Consequences

Human browsing remains simple. API cursors are tamper-resistant and query-bound; ranked search may still use bounded offset internally when the rank sort makes pure keyset pagination expensive.


# ADR-0017: Opaque Cursors

## Status

Accepted.

## Decision

API cursors are signed, opaque, and bound to the query and filters that produced them.

## Consequences

Malformed or tampered cursors fail cleanly. Ranked search may carry rank or page state inside the signed payload.


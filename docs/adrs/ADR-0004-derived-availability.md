# ADR-0004: Derived Availability

## Status

Accepted.

## Decision

Availability is derived from copy state and active circulation records, not from a hand-maintained counter.

## Consequences

There is one source of truth per copy. Search and facet paths may cache derived counts, but mutation services must update copy state transactionally.


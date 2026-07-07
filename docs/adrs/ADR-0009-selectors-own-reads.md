# ADR-0009: Selectors Own Reads

## Status

Accepted.

## Decision

Complex catalog, account, and staff reads live in selector functions.

## Consequences

Views stay thin and performance-sensitive queries have a single home for tuning, tests, and query-count review.


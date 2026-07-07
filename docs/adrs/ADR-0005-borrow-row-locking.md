# ADR-0005: Borrow Row Locking

## Status

Accepted.

## Decision

Borrowing uses database transactions and row locks on the selected copy to prevent last-copy races.

## Consequences

Concurrent borrow attempts serialize at the row that matters. The code must run against PostgreSQL in production and in meaningful concurrency tests.


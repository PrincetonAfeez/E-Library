# ADR-0010: Domain Events And Outbox

## Status

Accepted.

## Decision

Domain events and outbox events are written in the same transaction as state changes. A worker drains the outbox.

## Consequences

Notifications and operational side effects become retryable and observable without running inside the web request process.


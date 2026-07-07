# ADR-0006: Ready Holds Reserve A Copy

## Status

Accepted.

## Decision

A hold only reserves a specific `Copy` when it becomes ready. Waiting holds point to a `Work`.

## Consequences

The hold queue stays flexible while inventory is unavailable; once ready, the patron has a concrete copy with an expiry window.


# ADR-0008: Domain Services Own Mutations

## Status

Accepted.

## Decision

Circulation mutations live in service functions rather than views, serializers, templates, or admin actions.

## Consequences

Web and API entry points reuse the same transaction boundaries, locking strategy, business checks, audit writes, and domain events.


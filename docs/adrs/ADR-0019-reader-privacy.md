# ADR-0019: Reader Privacy

## Status

Accepted.

## Decision

Returned loans anonymize the patron-copy link by default. Patrons can opt in to retained history; erasure tombstones identity while preserving anonymized audit facts.

## Consequences

The system treats reading history as sensitive data without sacrificing circulation statistics or operational auditability.


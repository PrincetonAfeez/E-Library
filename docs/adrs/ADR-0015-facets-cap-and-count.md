# ADR-0015: Facets And Cap-And-Count

## Status

Accepted.

## Decision

Facet counts should be computed with bounded query count and result totals use cap-and-count labels instead of expensive exact totals at scale.

## Consequences

The UI can stay responsive on large catalogs while still telling patrons whether they have enough results to continue browsing.


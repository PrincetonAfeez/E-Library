# ADR-0014: Work-Grain Search

## Status

Accepted.

## Decision

Search indexing, ranking, holding, and pagination operate at the `Work` grain through `WorkSearchDocument`.

## Consequences

Multiple editions collapse to one patron-facing result. Exact edition signals such as ISBN still contribute to the Work-level search document.


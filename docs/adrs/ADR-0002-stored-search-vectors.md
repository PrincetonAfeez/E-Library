# ADR-0002: Stored Search Vectors

## Status

Accepted.

## Decision

Search vectors are stored and indexed in `WorkSearchDocument`; requests do not assemble vectors across related tables.

## Consequences

Search reads stay fast and predictable. Writes that affect title, edition, author, or subject text must enqueue or run reindexing.


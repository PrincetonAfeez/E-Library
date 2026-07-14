# ADR-0001: PostgreSQL Full-Text Search
 
## Status

Accepted.

## Decision

Catalog discovery uses PostgreSQL full-text search and `pg_trgm` rather than Elasticsearch, OpenSearch, or another external search service.

## Consequences

Search, faceting, and circulation truth remain in one transactional database. The tradeoff is that ranking, fuzzy matching, and facet performance must be designed carefully with stored vectors, GIN indexes, and measured query plans.


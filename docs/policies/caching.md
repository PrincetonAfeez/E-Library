# Caching Strategy

Cache backend: Redis (`CACHE_URL`). The limiter fails **open** if Redis is down
so a cache outage degrades protection rather than taking the site down.

## What is cached and how it is invalidated
| Data | Location | Invalidation |
|------|----------|--------------|
| Search facet counts | `selectors.get_facets_for_query` | Time-based, 30s TTL (short by design; facets tolerate mild staleness) |
| Rate-limit counters | `ratelimit` / `api._search_rate_limited` | Window expiry (TTL = window seconds) |
| Readiness probe key | `/readyz` | 5s TTL |

## Principles
- **TTL-first:** cached values are short-lived and self-expiring; we do not rely
  on explicit purge for correctness.
- **Source of truth is Postgres:** caches are derived and safe to flush anytime
  (`cache.clear()`), e.g. after a bulk catalog import.
- **Search documents** are not cached in Redis; they are denormalized into
  `WorkSearchDocument` and rebuilt on write (`rebuild_work_search_document`) — a
  write-through invalidation, not a TTL.

## When adding a new cache
Prefer a TTL. If you cache something that must be correct on write, invalidate it
in the owning `services.py` mutation, not from the read path.

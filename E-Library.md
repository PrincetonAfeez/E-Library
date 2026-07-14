E-Library Enterprise Capstone: Catalog, Search, Pagination, Filtering + Circulation

**One-line:** A production-ready Django + HTMX e-library system: a fast, searchable, faceted, paginated catalog backed by PostgreSQL full-text search, plus a correct multi-copy circulation engine with branch-aware inventory, holds, renewals, overdue tracking, notifications, librarian operations, support tooling, and a first-class API.

**Register:** Mastery of Python + system architecture. Enterprise-grade, production-ready, go-to-market-grade.

**Stack constraint:** Entire app in the Python ecosystem. Web layer is Django + HTMX. API layer is Django REST Framework. Database is PostgreSQL. Search lives inside PostgreSQL using full-text search and `pg_trgm`; no Elasticsearch/OpenSearch. A shared cache (Redis or DB-backed) serves facet/search caching and throttle state. A worker + scheduler process drains the outbox and runs time-based sweeps. No Channels/WebSockets. No fines/payments.

> **Changes in v3 (architecture pass).** Scope is *not* reduced. v2 was the strongest draft in the series — the search spine and the circulation crux are both sound and largely untouched — so this revision makes two decisions v2 left open and deepens the two cruxes.
> - **Decided — tenancy grain:** `Organization` is a true tenant boundary, and the build uses a **shared bibliographic spine** (Work/Edition/Author/Subject are platform-global, deduped and authority-controlled once) with **tenant-scoped holdings** (Copy, circulation, holds, patrons, policies). Search filters holdings by org at query time (§3.1a, ADR-0013).
> - **Decided — search grain:** patrons search/rank/hold at the **Work** grain, so search is indexed on a denormalized **Work-level search row**, not per-Edition; results collapse editions to works before ranking and paging (§5.1, §5.4, ADR-0014).
> - **Deepened — facets at scale:** single-pass aggregation (`FILTER`/`GROUPING SETS`), not N count queries; cap-and-count instead of exact totals (§5.3, ADR-0015).
> - **Deepened — hold-queue contention:** the contended row on return/reoffer is the **head of the Work's hold queue**, serialized with a per-`work_id` advisory lock (or `select_for_update` on the FIFO head), not the Loan/Copy locks alone (§6.2, §6.3, ADR-0016).
> - **Added:** cache tier + opaque/validated cursors + ranked-keyset caveat (§5.4, §5.6); worker/scheduler + `SKIP LOCKED` outbox draining (§16.2); API auth scheme (token/session/anonymous) (§12.0); reader-privacy loan-history minimization + erasure-vs-audit (§15.3); branch-aware hold fulfillment/transfer (§6.3); branch-timezone-aware overdue/due-soon (§6.5); author-rename fan-out reindex + search consistency model (§5.1); ADRs 0013–0019.

---

## 0. Product Thesis

The e-library is not a toy CRUD catalog. It is a production-shaped system with two technical cruxes and one operational product layer:

1. **Read crux — fast discovery at scale.** Patrons must search, filter, facet, and paginate a large catalog quickly. The system must demonstrate real database-backed search design: stored search documents, GIN indexes, trigram fuzzy matching, relevance ranking, live facet counts, offset pagination for shallow human browsing, and cursor/keyset pagination for API and infinite scroll.
2. **Write crux — correct lending under concurrency.** Patrons must never double-borrow the final copy. Holds must be FIFO. Ready holds must reserve a specific copy. Overdue, returns, renewals, and librarian overrides must be transactional and audited.
3. **Operational product layer — software a library can run daily.** Branches, librarian dashboards, catalog governance, import/export, patron notifications, audit trails, support tools, search analytics, and performance targets turn the project from a capstone demo into a go-to-market product.

The core promise: **a patron can reliably find a book, and the library can reliably know who has every copy, where it is, and what should happen next.**

---

## 1. Locked Decisions

| # | Area | Decision |
|---|------|----------|
| 1 | Scope | Catalog/discovery + circulation/lending + operational library workflows |
| 2 | Search engine | PostgreSQL full-text search with GIN-indexed stored search vectors + `pg_trgm` fuzzy matching; no external search service |
| 3 | Bibliographic model | `Work → Edition → Copy`, so title identity, publication metadata, and physical inventory do not blur together |
| 4 | Locations | Branch/location support is launch scope, even if the first deployment uses one branch |
| 5 | Copies | Multiple copies per edition; availability derived from individual copy state, never a hand-maintained counter |
| 6 | Holds | FIFO holds are in scope; ready holds reserve a specific copy via `assigned_copy` |
| 7 | Renewals | In scope with explicit policy rules; blocked when waiting holds exist |
| 8 | Overdue | Due dates and overdue tracking in scope; no fines or payments |
| 9 | Roles | Patron, Librarian, Branch Manager, Admin, and Support; server-side permissions everywhere |
| 10 | Filtering | Faceted filtering with live counts |
| 11 | Pagination | Offset pagination for shallow web browse; cursor/keyset pagination for API and HTMX infinite scroll |
| 12 | Search UX | Live, debounced search-as-you-type using HTMX |
| 13 | API | Public read/search API plus authenticated circulation and librarian APIs under `/api/v1/` |
| 14 | API framework | Django REST Framework with OpenAPI schema, serializers, throttling, permissions, and stable error envelope |
| 15 | Architecture | Explicit service layer, selector/query layer, domain events, outbox, and audit log |
| 16 | Imports | CSV import/export with staging, validation, deduplication, and reindexing |
| 17 | Notifications | Patron email notifications for holds, due dates, overdue loans, returns, and account flows |
| 18 | Search analytics | Query logging, zero-result tracking, latency tracking, and relevance-tuning reports |
| 19 | Operations | Librarian dashboard and support console are in scope |
| 20 | Production quality | PostgreSQL tests, concurrency tests, query-count/performance tests, lint/type checks, CI, health checks, observability |
| 21 | Security | Object-level permissions, staff audit, rate limits, secure uploads, PII-safe logging, HTTPS/cookies/CSRF |
| 22 | Tenancy grain | `Organization` is a tenant boundary. Bibliographic data (Work/Edition/Author/Subject) is a **shared platform-global spine**; holdings (Copy), circulation, holds, patrons, and policies are **tenant-scoped**. Search filters holdings by org at query time. |
| 23 | Search grain | Indexing, ranking, and pagination operate at the **Work** grain via a denormalized Work-level search row; edition matches collapse to one Work result. |
| 24 | Facet computation | Facet counts come from a **single-pass** aggregation (`FILTER`/`GROUPING SETS`), constant in query count; result totals use **cap-and-count**/estimates, not exact `count(*)` over large sets. |
| 25 | Hold-queue serialization | Assigning a copy to the next waiting hold (on return or reoffer) is serialized per `work_id` (advisory lock or FIFO-head `select_for_update`); Loan/Copy locks alone do not cover the contended queue head. |
| 26 | Cache tier | A shared cache holds facet/search result caches (keyed on normalized query+filter+sort+page+tenant) and throttle state; never per-process memory. Anonymous catalog API responses carry ETags. |
| 27 | Cursors | API cursors are opaque, encoded, and server-validated; ranked-search cursors are query-bound and carry the rank value (with the ranked-keyset cost acknowledged). |
| 28 | API auth | Public read API is anonymous + throttled; programmatic patron/librarian clients use token/key auth (scoped, rotatable, audited); the browser uses session auth. CSRF posture follows the auth mode. |
| 29 | Reader privacy | Loan history retention is a deliberate decision: anonymize the patron↔copy link after return by default, with opt-in lifetime history; patron erasure tombstones the patron while preserving anonymized circulation counts and the append-only audit. |
| 30 | Branch time | Due-date, due-soon, and overdue boundaries are computed in the relevant branch timezone, not a server-global clock. |

---

## 2. Actors, Roles & Permissions

| Role | Can do | Cannot do |
|------|--------|-----------|
| **Anonymous Visitor** | Browse public catalog; use public search/filter/facet API | Borrow, hold, see patron data, manage catalog |
| **Patron** | Browse/search/filter; borrow available copies; return own loans where policy allows; place/cancel holds; renew eligible loans; view own loans/holds/notifications | Manage catalog/copies; see others' data; bypass holds; override policy |
| **Librarian** | Manage circulation at assigned branch; check out/check in copies; manage holds; view patron circulation records; add/edit copies; run reports | Global admin actions outside assigned branch unless permissioned |
| **Branch Manager** | Librarian abilities plus branch policy configuration, staff queue oversight, branch reports, branch-level imports | Global system configuration unless Admin |
| **Admin** | Full catalog, branch, user, policy, import/export, support, and system configuration access | — |
| **Support** | Read-only diagnostic access, support console, search/circulation inspection, limited repair actions with reason and audit | Silent mutation; patron data export unless explicitly permissioned |

**Permission rules:**
- All permissions are enforced in services/selectors and API/web endpoints, not only in templates.
- Patron-owned resources are object-scoped: a patron can access only their own loans, holds, profile, and notification preferences.
- Branch staff can be scoped to one or more branches.
- Support impersonation or repair actions require a reason, time limit, and audit entry.

---

## 3. Core Data Model

### 3.1 Organization and branch model

- **Organization** — library system/account: name, slug, default timezone, default policies, support settings.
- **Branch** — FK Organization, name, address, timezone, active flag, pickup rules, loan policies, notification sender details.
- **ShelfLocation** — FK Branch, code/name, floor/room/section/shelf, public label, staff notes.

### 3.1a Tenancy model and data grain

`Organization` is a true tenant boundary, and the build splits data into two grains:

- **Shared platform-global (not tenant-owned):** `Work`, `Edition`, `Author`, `Subject/Genre`, and the Work-level search row. There is one canonical bibliographic record per title across the whole platform — deduped once, authority-controlled once, indexed once. This is the union-catalog model real library SaaS uses; it makes search, dedup, and authority control dramatically better than per-org copies of the same book.
- **Tenant-scoped (org-owned, carry `organization_id`):** `Branch`, `ShelfLocation`, `Copy`, `Loan`, `Hold`, `Renewal`, `CopyMovement`, `PatronProfile`, `StaffMembership`, policies, imports, notifications, search analytics, audit, and outbox.

Consequences that the rest of the scope honors:
- **Search** runs against the shared Work index but **availability/holdings facets filter by `organization_id`** at query time — a patron only ever sees their org's copies, holds, and availability, even though the bibliographic match is global.
- **Tenant isolation** for org-scoped models uses tenant-aware managers/querysets (deny-by-default) and a cross-org isolation test suite, in the spirit of the platform-grade isolation expected of multi-tenant SaaS. Shared bibliographic reads are intentionally cross-org; *holdings and patron data are never cross-org*.
- **Catalog governance** (merge/split, authority control) is a platform-level privileged operation, audited, because it affects every tenant's holdings hanging off a shared record.

(ADR-0013 records this; if a deployment instead needs fully isolated per-org catalogs, that is a different product and a different ADR — the scope commits to the shared spine.)

### 3.2 Bibliographic catalog

- **Work** — title-level concept: canonical title, subtitle, normalized title, summary, authors M2M, subjects/genres M2M, public status, slug, internal notes.
- **Edition** — publishable manifestation of a Work: FK Work, ISBN-10, ISBN-13, publisher, publication year, language, format, edition statement, description, cover image, public status, stored `search_document`, stored `search_vector`.
- **Author** — name, normalized name, sort name, aliases, authority identifier where available.
- **Subject/Genre** — name, slug, parent subject, public/private flag.
- **Collection** — curated public grouping: featured works, staff picks, new arrivals, themed collections.

### 3.3 Inventory and circulation

- **Copy** — FK Edition, FK Branch, optional FK ShelfLocation, barcode/identifier, acquisition date, condition, visibility, status: `available` / `loaned` / `on_hold` / `in_transit` / `lost` / `retired` / `repair`.
- **Loan** — FK Copy, FK Patron, borrowed_at, due_at, returned_at, status: `active` / `returned` / `overdue` / `lost`.
- **Hold** — FK Work, FK Patron, preferred branch, optional assigned Copy, created_at, ready_at, expires_at, status: `waiting` / `ready` / `fulfilled` / `expired` / `cancelled`.
- **Renewal** — FK Loan, renewed_by, old_due_at, new_due_at, reason/source, created_at.
- **CopyMovement** — copy location/state audit: branch transfer, shelving, repair, lost, retired, restored.
- **LibrarianOverride** — explicit override of policy or circulation state with actor, reason, before/after.

### 3.4 Users and patron profile

- **User** — Django auth user.
- **PatronProfile** — FK User, library card number, home branch, contact preferences, max loans/holds override, status: active / blocked / archived.
- **StaffMembership** — FK User, FK Organization, optional Branch scope, role, permissions.

### 3.5 Imports, notifications, analytics, and audit

- **CatalogImportBatch** — source file, uploaded_by, status, row counts, validation summary, committed_at, rolled_back_at.
- **CatalogImportRow** — FK batch, row payload, parsed fields, validation errors, matched existing records, commit result.
- **NotificationTemplate** — key, channel, subject/body, active flag.
- **NotificationDelivery** — recipient, template, related entity, status, provider reference, attempts, sent_at, failed_at.
- **SearchQueryLog** — query, filters, result count, selected result where available, latency, user/session hash, created_at.
- **DomainEvent** — append-only event stream: event_type, aggregate_type, aggregate_id, payload, actor/source, created_at.
- **AuditLog** — append-only user/system action log: actor, source, entity, before/after, reason, request_id, IP/device metadata where available.
- **OutboxEvent** — reliable post-commit side effects: event_type, payload, status, attempts, next_attempt_at, processed_at.

---

## 4. Database Guarantees and Indexes

The database must protect truth. Application validation improves UX but does not replace database constraints.

### 4.1 Required constraints

- `Copy.barcode` unique per organization or globally, depending on configuration.
- Only one active/overdue loan per copy at a time.
- A patron cannot have duplicate active/ready/waiting holds for the same Work.
- A patron cannot borrow the same Work twice at the same time unless a librarian override explicitly allows it.
- A ready hold must have `assigned_copy`.
- An assigned ready hold's copy must be `on_hold`.
- A fulfilled hold must link to a loan.
- A returned loan must have `returned_at`.
- A retired/lost/repair copy cannot be borrowed.
- Search vectors must be populated for published editions before public search visibility.
- Catalog slugs unique within the relevant scope.

### 4.2 Required indexes

- GIN index on `Edition.search_vector`.
- GIN/GiST trigram indexes on normalized title, author names, ISBN, and `search_document` where appropriate.
- `Copy(edition_id, branch_id, status)`.
- `Loan(patron_id, status)`.
- `Loan(copy_id, status)`.
- `Loan(due_at, status)` for overdue sweep.
- `Hold(work_id, status, created_at)` for FIFO hold assignment.
- `Hold(patron_id, status)`.
- Partial indexes for active loans, overdue loans, waiting holds, ready holds, available copies, and public editions.
- Search analytics indexes by query timestamp, result count, and latency buckets.

### 4.3 Transactional invariants

- Borrow, return, hold fulfillment, hold expiry, renewal, copy retirement, and librarian override run in `transaction.atomic()`.
- Borrowing uses row locks (`select_for_update(skip_locked=True)` or equivalent) to prevent last-copy races.
- **Hold-queue head is serialized per `work_id`.** Assigning a returned/expiring copy to "the next waiting hold" is a contended operation: two copies of the same Work returned concurrently at two branches can both read the same FIFO head and double-assign or skip a hold. Loan/Copy locks do not cover this. The assignment critical section takes a **Postgres advisory lock keyed on `work_id`** (or `select_for_update` on the FIFO-ordered head `Hold` row) so "pick the next waiting hold and mark it ready" happens one-at-a-time per Work.
- Returning a copy and assigning it to the next waiting hold happen in the same transaction (under the queue serialization above).
- Expiring a ready hold and offering the copy to the next hold is idempotent, safe to retry, and uses the same per-`work_id` serialization.
- All staff overrides write audit entries and domain events in the same transaction as the state change.

---

## 5. Read Crux — Search, Filtering, Facets, and Pagination

### 5.1 PostgreSQL full-text search

- Search uses a stored, GIN-indexed `tsvector` on a **denormalized Work-level search row** (one row per Work), so ranking and pagination operate at the grain patrons actually search and hold. Edition-specific terms (ISBN, format, publisher) are folded into the Work search row; an exact-ISBN lookup still resolves to the specific Edition (see §5.2).
- The row carries both a stored `search_document` (assembled text) and the derived `search_vector`. Two fields are deliberate: a Postgres `GENERATED ALWAYS AS … STORED` column **cannot reference other tables**, and authors/subjects are M2M, so the document text is assembled in a reindex path (Work + Editions + author names + subject names) and the vector is derived from it.
- Search vector weights:
  - A: Work title, subtitle, normalized title, exact ISBN.
  - B: Author names, aliases, subjects/genres.
  - C: Publisher, series/collection, edition metadata.
  - D: Description/summary.
- Search reads must never compute vectors across every row at request time.
- **Search is eventually consistent**, with an explicit set of triggering changes and a reconciliation backstop:
  - Direct edits to a Work or its Editions reindex that Work.
  - M2M author/subject changes reindex the affected Work via `m2m_changed`/trigger.
  - **An Author rename (or merge) is a fan-out:** every Work referencing that author is stale and must be reindexed — a fan-out the per-row M2M hook does not catch, so author/subject mutations enqueue reindex jobs for all dependent Works.
  - `rebuild_search_index` is the reconciliation sweep that repairs any drift after imports, author changes, or repair.

### 5.2 Relevance and fuzzy matching

- Use `SearchRank` for relevance ordering.
- Use `pg_trgm` and `TrigramSimilarity` for typo tolerance and near-match suggestions.
- Exact ISBN/barcode searches bypass fuzzy ranking and return direct matches.
- Search supports basic synonyms configured by librarians/admins.
- Zero-result searches offer suggestions where possible.

### 5.3 Faceted filtering with live counts

Facets:
- Subject/genre.
- Author.
- Language.
- Format.
- Branch availability.
- Publication year range.
- New arrivals.
- Collection.

Facet counts:
- Reflect the current query/filter set.
- Usually exclude their own dimension from the constraint so users can see alternative values.
- Are computed in a **single pass** over the matched set using conditional aggregates (`FILTER (WHERE …)`) or `GROUPING SETS`, **not one COUNT query per facet** — query count is constant in the number of facets, asserted by query-count tests.
- Filter holdings/availability facets by `organization_id` (per §3.1a) so counts reflect the patron's org.
- Use **cap-and-count / estimates for result totals** rather than exact `count(*)` over large matched sets (e.g. `count(*)` over a `LIMIT 1000` subquery, surfaced as "about N results"); exact totals only when the set is small. This is what makes the < 500ms facet target reachable.
- Cache facet results in the shared cache keyed on the normalized query+filter+sort+tenant; invalidate on reindex.
- Degrade gracefully for very expensive combinations by showing capped/top-N counts if needed.

### 5.4 Pagination strategy

- **Offset pagination** for web browse with page numbers and shallow exploration.
- **Cursor/keyset pagination** for public API and HTMX infinite scroll.
- Because results are at the **Work grain** (§5.1), edition matches are collapsed to one Work before ranking and paging — the denormalized Work search row makes this a flat keyset over Works rather than a `DISTINCT ON` over editions at query time.
- Cursor ordering must be stable and deterministic: `(rank, title_sort, work_id)` for search and `(created_at, work_id)` for browse feeds.
- **Cursors are opaque, encoded, and server-validated** (base64 of the ordering tuple, integrity-checked); a tampered or malformed cursor yields a clean 400, never an error or leak.
- **Ranked keyset has real cost (acknowledged in ADR-0014/0017):** `ts_rank` is a query-dependent float, so a ranked cursor is valid only for that exact query, must carry the rank value, and must continue against the identical rank expression; float ties push work onto the `(title_sort, work_id)` tiebreaker. The stable `(created_at, work_id)` browse feed is true keyset; for ranked results, bounded offset is an acceptable fallback where keyset-on-rank is not worth the complexity.
- Deep offset browsing is intentionally avoided for API and infinite scroll.

### 5.5 Search analytics and relevance tuning

Track:
- Query string.
- Filters/facets used.
- Result count.
- Latency.
- Result clicked/borrowed/held when available.
- Zero-result queries.
- Trigram fallback usage.
- Slow queries.

Admin tools:
- Search analytics dashboard.
- Zero-result report.
- Popular queries report.
- Synonym management.
- Boost-rule management.
- Slow-search inspection with query plan notes.

### 5.6 Read caching

Discovery is read-dominant, so a shared cache tier is part of the design, not an afterthought:

- Facet-count sets and search result pages are cached keyed on the **normalized** query + filter + sort + page (+ `organization_id`), so equivalent requests reuse work; cross-tenant keys never collide.
- Cache entries are invalidated on reindex of any contributing Work.
- The anonymous public catalog/search API sends **ETags / cache headers** so it is CDN- and client-cacheable.
- The same shared cache backs DRF/throttle state (§15.2) — throttling is never per-process memory.

---

## 6. Write Crux — Circulation Under Concurrency

### 6.1 Borrowing

Borrowing a Work:
1. Validate patron eligibility: active account, branch policy, max loans, no duplicate active loan for same Work unless override.
2. Check holds queue: if waiting/ready holds exist, only a patron with the correct ready hold may borrow the assigned copy.
3. In a transaction, select an eligible available copy using row locking.
4. Mark copy `loaned`.
5. Create Loan with due date.
6. Emit `LoanCreated` domain event.
7. Write audit log.

Last-copy race requirement:
- A test with many simultaneous borrow attempts against one available copy produces exactly one successful loan and zero double-loans.

### 6.2 Returning

Returning a Copy:
1. Lock the active/overdue Loan and Copy.
2. Mark Loan returned.
3. Under the per-`work_id` hold-queue serialization (§4.3), if waiting holds exist for the Work, assign the returned copy to the next eligible hold, set Hold `ready`, set Copy `on_hold`, and set pickup expiration. Branch eligibility follows §6.3.
4. If no waiting holds exist, set Copy `available`.
5. Emit domain events and queue notifications.

### 6.3 Holds

Hold rules:
- Patrons place holds against Works, not specific Editions, unless a librarian-configured policy narrows eligible editions/formats.
- Holds are FIFO by `created_at` with deterministic tie-breaker `id`.
- A patron cannot place a hold on a Work they already have on active loan.
- A patron cannot place duplicate active holds for the same Work.
- Ready holds reserve a specific copy and prevent other patrons from borrowing around the queue.
- **Branch-aware fulfillment:** a copy returned at Branch X may satisfy a hold whose preferred pickup is Branch Y by transitioning the copy to `in_transit` and recording a `CopyMovement` transfer, when branch policy permits transfers; if transfers are disabled, only a same-branch waiting hold is offered the copy and the queue is scanned for the first branch-eligible hold. The matching rule is explicit policy, not implicit.
- Ready holds expire after the pickup window and reoffer the copy to the next waiting hold under the same per-`work_id` serialization.

### 6.4 Renewals

Renewal rules:
- Renewal allowed only when no waiting holds exist for the Work.
- Renewal limit is configurable per branch/organization.
- Renewal blocked if the copy is lost, retired, under repair, or if patron is blocked.
- Renewal can be blocked when the loan is already overdue, depending on policy.
- Renewal writes a `Renewal` row, updates due date, emits event, and sends confirmation notification.

### 6.5 Overdue

- A scheduled sweep flags active loans past `due_at` as overdue. Due-date, due-soon, and overdue boundaries are evaluated in the **branch timezone** (local midnight), not a server-global clock, so a loan "due today" is correct per branch.
- No fines/payments are in scope.
- Overdue state appears in patron account, librarian dashboard, and notification workflows.
- Overdue sweep is idempotent and safe to rerun.

---

## 7. Catalog Governance

Production catalog management requires controlled data quality.

### 7.1 Catalog statuses

- Work status: draft / published / suppressed / archived.
- Edition status: draft / published / suppressed / archived.
- Copy status controls circulation; catalog public status controls visibility.

### 7.2 Duplicate management

- Detect possible duplicate Works by normalized title + author similarity.
- Detect duplicate Editions by ISBN and metadata similarity.
- Provide librarian merge/split workflows.
- Merge/split operations are audited and reversible where practical.

### 7.3 Metadata quality

- ISBN normalization and validation.
- Author name normalization and sort-name generation.
- Slug generation and collision handling.
- Cover image validation and replacement history.
- Staff-only internal catalog notes.
- Public visibility rules for suppressed records and retired/lost copies.

---

## 8. Import, Export, and Reindexing

### 8.1 CSV import pipeline

- Librarian uploads CSV for Works, Editions, Authors, Subjects, Copies, and branch/shelf locations.
- Import creates a `CatalogImportBatch` and `CatalogImportRow` records.
- Rows are parsed and validated before commit.
- Validation report shows errors, warnings, duplicate matches, and proposed changes.
- Commit applies valid rows transactionally in batches.
- Import can be cancelled before commit.
- Import writes domain events and audit entries.
- Import completion triggers or queues search reindexing for affected records.

### 8.2 Export pipeline

Exports:
- Catalog records.
- Copy inventory by branch/status.
- Active loans.
- Overdue loans.
- Hold queues.
- Search analytics summary.
- Audit reports for staff actions.

Exports are permission-gated and logged.

### 8.3 Reindexing

- `rebuild_search_index` command rebuilds all search documents/vectors.
- `reindex_work` and `reindex_edition` commands support targeted repair.
- Reindex jobs log counts, duration, failures, and query-plan checks for key search queries.

---

## 9. Patron Experience

### 9.1 Public catalog

- Search bar with debounced HTMX live search.
- Filter sidebar with live facet counts.
- Page-number browse for shallow exploration.
- Infinite-scroll/cursor experience for live-search result feed.
- Work detail page showing editions, branches, availability, hold/borrow actions, related works, subjects, and author links.
- Clear availability states: available now, available at branch, on hold, all copies loaned, not currently circulating.

### 9.2 Patron account

- Current loans.
- Due dates and overdue indicators.
- Renewal actions where eligible.
- Hold queue position.
- Ready holds and pickup expiration.
- Loan history.
- Notification preferences.
- Saved searches or reading list, optional but recommended.

### 9.3 Accessibility and mobile quality

- Mobile-first responsive layout.
- Keyboard-operable search, filters, and lending actions.
- Accessible labels and semantic headings.
- Visible focus states.
- Reduced-motion support for HTMX swaps.
- Screen-reader-friendly result counts and filter changes.

---

## 10. Librarian Operations Dashboard

The staff experience is a first-class product surface, not just Django admin.

Dashboard modules:
- Loans due today.
- Overdue loans.
- Holds ready for pickup.
- Holds expiring soon.
- Waiting holds by Work.
- Copies needing shelving.
- Lost/retired/repair copies.
- Recently returned copies.
- Long hold queues.
- Search queries with zero results.
- Recently imported catalog records.
- Circulation activity by day/week/month.

Operational flows:
- Check out copy to patron.
- Check in copy.
- Force-return with reason.
- Cancel hold with reason.
- Mark copy lost/repair/retired.
- Move copy between branch/shelf locations.
- Inspect patron circulation record.
- Inspect copy history.
- Resolve duplicate Work/Edition candidates.

All staff actions are permission-gated and audited.

---

## 11. Notifications

Notifications are driven by domain events and sent through an outbox-backed delivery pipeline.

### 11.1 Notification types

- Account creation / email verification.
- Password reset.
- Hold placed.
- Hold ready for pickup.
- Hold expiring soon.
- Hold expired.
- Loan borrowed confirmation.
- Loan due soon.
- Loan overdue.
- Loan renewed.
- Return confirmed.
- Librarian override or cancellation notice where patron-facing.

### 11.2 Delivery requirements

- Email is launch channel.
- Templates are editable by Admin within safe constraints.
- Notification preferences stored per patron.
- Delivery attempts are logged.
- Failed sends retry with bounded backoff.
- Notifications are never sent before the transaction that caused them commits.

---

## 12. API Layer — DB + API + Web

The API is first-class, versioned, documented, and uses the same services/selectors as the web layer.

### 12.0 Authentication model

Three access tiers under one authorization model (all routing through the same services/selectors, §13):

- **Public read API** (catalog/search/facets): anonymous, throttled per client (IP + normalized fingerprint), ETag-cacheable.
- **Programmatic patron/librarian clients:** token/key auth via an API credential that is org-scoped and role-scoped, rotatable, per-key throttled, and audited on use. The request's org/role derives from the credential, never a parameter.
- **Browser:** session auth for the HTMX/web surface.

CSRF posture follows the auth mode: session writes require CSRF; token requests carry no ambient cookie and are CSRF-exempt by design. Tenant scoping (§3.1a) and object-level permissions apply identically across tiers.

### 12.1 Public catalog/search API

Routes:

```text
GET /api/v1/catalog/works/
GET /api/v1/catalog/works/{id}/
GET /api/v1/catalog/editions/
GET /api/v1/catalog/editions/{id}/
GET /api/v1/catalog/search/
GET /api/v1/catalog/facets/
GET /api/v1/catalog/authors/
GET /api/v1/catalog/subjects/
GET /api/v1/catalog/collections/
```

Requirements:
- Anonymous access.
- Rate-limited.
- Cursor-paginated.
- Stable ordering.
- OpenAPI documented.
- Same search/facet selectors as web.

### 12.2 Authenticated patron API

Routes:

```text
GET  /api/v1/account/me/
GET  /api/v1/circulation/loans/
POST /api/v1/circulation/loans/{id}/renew/
POST /api/v1/circulation/loans/{id}/return/
GET  /api/v1/circulation/holds/
POST /api/v1/circulation/holds/
POST /api/v1/circulation/holds/{id}/cancel/
GET  /api/v1/notifications/
PATCH /api/v1/notification-preferences/
```

### 12.3 Librarian API

Routes:

```text
GET  /api/v1/librarian/dashboard/
GET  /api/v1/librarian/patrons/{id}/circulation/
POST /api/v1/librarian/checkout/
POST /api/v1/librarian/checkin/
POST /api/v1/librarian/copies/{id}/retire/
POST /api/v1/librarian/copies/{id}/move/
GET  /api/v1/librarian/imports/
POST /api/v1/librarian/imports/
POST /api/v1/librarian/imports/{id}/commit/
GET  /api/v1/librarian/reports/overdue/
GET  /api/v1/librarian/reports/holds/
```

### 12.4 API standards

- Versioned namespace: `/api/v1/`.
- OpenAPI schema generated and included in CI.
- Stable JSON error envelope:

```json
{
  "code": "hold_queue_conflict",
  "message": "This work has patrons ahead of you in the hold queue.",
  "fields": {},
  "request_id": "..."
}
```

- 400 validation errors.
- 401 unauthenticated.
- 403 unauthorized.
- 404 not found.
- 409 conflict for circulation races and policy violations.
- 429 throttled.
- `X-Request-ID` included in responses and logs.

---

## 13. Django + Python System Architecture

The codebase must demonstrate senior-level Python and Django architecture.

### 13.1 Django apps

- `accounts` — auth, patron profile, staff memberships, permission helpers.
- `organizations` — organizations, branches, shelf locations, policies.
- `catalog` — Work, Edition, Author, Subject, Collection, Copy, catalog governance.
- `search` — search documents, selectors, ranking, facets, analytics, reindex commands.
- `circulation` — Loan, Hold, Renewal, circulation services and sweeps.
- `notifications` — templates, delivery log, notification preferences, outbox consumers.
- `imports` — import batches, row staging, validation, commit/export workflows.
- `audit` — DomainEvent, AuditLog, entity history.
- `support` — support console, diagnostics, repair tooling.
- `api` — DRF serializers, views, permissions, throttles, schema.
- `web` — Django views, forms, HTMX partials, page composition.

### 13.2 Service layer rule

Views, DRF views, forms, templates, admin actions, and scheduled tasks must not directly perform complex business mutations.

Core services:
- `borrow_work`
- `return_copy`
- `place_hold`
- `cancel_hold`
- `expire_ready_holds`
- `renew_loan`
- `flag_overdue_loans`
- `retire_copy`
- `move_copy`
- `merge_works`
- `commit_catalog_import`
- `send_due_soon_notifications`

Each service owns:
- Transaction boundary.
- Locking strategy.
- Business invariant checks.
- Domain event emission.
- Audit logging.
- Clear typed input/output objects where practical.

### 13.3 Selector/query rule

Complex reads live in selectors, not views/templates.

Selectors:
- `search_catalog`
- `get_facets_for_query`
- `get_work_detail`
- `get_patron_loans`
- `get_patron_holds`
- `get_librarian_dashboard`
- `get_overdue_report`
- `get_hold_queue`
- `get_copy_history`

Web and API layers must reuse these selectors.

### 13.4 Domain events and outbox

- Domain events are appended in the same transaction as state changes.
- Outbox events are processed after commit for notifications, analytics, and async side effects.
- Outbox processing is idempotent, retryable, observable, and safe to replay.

---

## 14. HTMX Web Layer

HTMX is the first-class web interaction model. No SPA framework.

Primary HTMX interactions:
- Debounced catalog search.
- Facet filter updates.
- Infinite-scroll result loading.
- Borrow/hold/renew/cancel buttons with partial updates.
- Patron account loan/hold table refreshes.
- Librarian dashboard queue updates.
- Check-in/check-out flows.
- Import validation preview.
- Duplicate resolution forms.

Rules:
- Every HTMX mutation returns a focused partial and, where needed, out-of-band updates for counts/badges.
- Full-page fallbacks exist for primary workflows.
- Validation errors render inline without losing user input.
- Permission failures render safely and do not leak object existence beyond allowed scope.
- HTMX partials have tests.

Recommended template structure:

```text
templates/
  catalog/
    search.html
    work_detail.html
    partials/
      _search_results.html
      _facet_panel.html
      _availability_badge.html
      _work_card.html
  circulation/
    account.html
    partials/
      _loan_table.html
      _hold_table.html
      _renew_button.html
  librarian/
    dashboard.html
    partials/
      _overdue_queue.html
      _ready_holds.html
      _checkin_form.html
      _copy_history.html
```

---

## 15. Security, Privacy, and Compliance Readiness

### 15.1 Authentication and authorization

- Django auth with secure password reset.
- Optional email verification for patron accounts.
- Staff accounts must support MFA where deployment environment supports it.
- Role and object permissions enforced server-side.
- Object-level tests for patron loans, holds, notifications, and librarian branch scope.
- Support access is time-boxed, reasoned, and audited.

### 15.2 Web/API security

- `DEBUG=False` in production.
- `ALLOWED_HOSTS` configured.
- Secrets from environment/secret manager; no secrets committed.
- HTTPS-only, secure cookies, HSTS, CSRF.
- Rate limiting on anonymous search, login, password reset, holds, renewals, and librarian writes.
- CORS locked down for API.
- CSP for web UI.
- Upload validation for cover images: content type, extension, size, filename/path sanitization.

### 15.3 Privacy and logging

- PII redacted in logs by default.
- Search analytics avoid storing raw sensitive patron identity; use user/session hash where possible.
- Data export and deletion procedures documented.
- Staff viewing sensitive patron records is audited.
- Audit records are append-only at the application layer.
- **Reader privacy (deliberate decision).** What a patron has borrowed is among the most sensitive data a library holds. The default posture **anonymizes the patron↔copy link after a loan is returned** (severing the historical association), with **opt-in lifetime history** for patrons who want a reading list. This is a privacy-by-design choice, stated explicitly rather than defaulting to indefinite retention.
- **Erasure vs. append-only audit.** A patron erasure request **tombstones/anonymizes the patron** (identity fields severed or hashed) while preserving anonymized circulation counts and the append-only audit as anonymized facts. Active loans/holds must be resolved before erasure completes. Export, retention-based anonymization, and erasure are distinct, individually documented paths.

---

## 16. Production Scaffolding and Operations

### 16.1 Infrastructure

- PostgreSQL for dev/prod; SQLite unsupported.
- `pg_trgm` extension enabled via migration.
- Shared cache (Redis or DB-backed) for facet/search caching and throttle state; never per-process local memory.
- Object storage for cover images in production.
- Dockerfile with slim base, non-root user, collected static, Gunicorn.
- `docker-compose.yml` for local web + PostgreSQL + cache + **outbox worker** + **scheduler**.
- Env-driven settings with `.env.example` committed and `.env` ignored.
- Static files through WhiteNoise or host-native static support.
- Real domain and TLS in deployed environment.

### 16.2 Background/scheduled work

Scheduled jobs:
- Hold-expiry sweep.
- Overdue flagging.
- Due-soon notifications.
- Hold-expiring-soon notifications.
- Search analytics rollups.
- Import commit/reindex jobs where needed.
- Slow-query snapshot/report job where useful.

A cron'd management command is acceptable when job volume is low. Celery/Celery Beat is acceptable if notifications/imports/reindexing justify workers.

Regardless of mechanism:
- A **worker process** drains the outbox by claiming rows with `SELECT … FOR UPDATE SKIP LOCKED`; handlers are idempotent and safe to replay (the outbox carries `attempts`/`next_attempt_at` for bounded backoff).
- A **scheduler** (cron-style) fires the time-based sweeps (hold-expiry, overdue flagging, due-soon, analytics rollups) on a cadence; sweeps are idempotent.
- Neither runs inside the Gunicorn web process; both appear in compose and production.

### 16.3 Observability

- Structured JSON logs with request ID, user ID where safe, branch ID, job ID, service name, and latency.
- Error tracking via Sentry or equivalent.
- `/healthz` endpoint checks app and database connectivity.
- Metrics for search latency, facet latency, borrow success/failure, hold placement, hold expiry, overdue count, notification delivery, import failures, and outbox backlog.
- Slow-query monitoring for search/facets/circulation hot paths.

### 16.4 Support console

Support tools:
- Patron lookup.
- Loan history.
- Hold queue inspection.
- Copy history.
- Search document inspection.
- Reindex Work/Edition.
- View notification delivery history.
- View import batch details.
- View domain events and audit timeline.
- Repair stuck hold/copy state with reason.
- Force-return with reason.
- Cancel hold with reason.

Support tooling must never allow silent mutation.

---

## 17. Performance Targets

Performance is part of scope, not a later optimization.

Required targets against the large seed dataset:

| Area | Target |
|------|--------|
| Catalog search p95 server response | < 300ms |
| Facet query p95 server response | < 500ms |
| HTMX live-search partial p95 | < 250ms |
| API cursor page p95 | < 250ms |
| Work detail page p95 | < 300ms |
| Patron account page p95 | < 300ms |
| Borrow transaction p95 | < 200ms excluding network overhead |
| Return transaction p95 | < 200ms excluding network overhead |
| Concurrent last-copy test | At least 20 simultaneous borrow attempts, exactly one success for one copy |
| Search/facet query count | Bounded and documented per page |

Required evidence:
- `EXPLAIN ANALYZE` snapshots for main search, facet, cursor pagination, availability, borrow, and hold queue queries.
- Test fixtures or seed data large enough to make performance meaningful.
- Query-count tests for critical views/selectors.

---

## 18. Testing Strategy

### 18.1 Automated tests

Required coverage:
- Unit tests for service-layer business rules.
- Real PostgreSQL tests for search, FTS, trigram fuzzy, row locks, and constraints.
- Concurrency tests for last-copy borrow race.
- Hold queue FIFO tests.
- Ready hold assigned-copy tests.
- Hold expiry and reoffer tests.
- Renewal allowed/blocked tests.
- Overdue sweep tests.
- Permission tests for Patron/Librarian/Branch Manager/Admin/Support.
- Object-level access tests for patron data.
- API contract tests for response shapes and error codes.
- HTMX partial rendering tests.
- Import validation/commit tests.
- Search-vector maintenance tests for M2M author/subject changes.
- Work-grain collapse: a multi-edition Work appears once in ranked results; ranking/pagination stable.
- Single-pass facet aggregation: facet query count is constant in number of facets; cap-and-count totals.
- Author-rename fan-out reindexes all dependent Works; `rebuild_search_index` reconciles drift.
- Concurrent returns of two copies of one Work cannot double-assign or skip the hold-queue head.
- Opaque cursor: tampered/malformed cursor yields a clean 400, no leak.
- Tenant isolation: holdings/circulation/patron data never cross-org; bibliographic reads are shared.
- API auth tiers: anonymous public read throttled; token key scoped/rotatable/revocable/audited; session for browser.
- Reader privacy: patron↔copy link anonymized after return (default); erasure tombstones identity while audit survives.
- Branch-timezone overdue/due-soon boundary correctness.
- Query-count/performance smoke tests.

### 18.2 Golden fixtures

Maintain fixtures for:
- Large searchable catalog.
- One-copy last-copy race.
- Multi-copy Work across branches.
- Work with waiting holds.
- Ready hold with assigned copy.
- Expired hold reoffering to next patron.
- Renewal blocked by waiting hold.
- Overdue loan.
- Duplicate Work/Edition import.
- Zero-result search and fuzzy suggestion.

### 18.3 CI gates

CI must run:
- Formatting/linting.
- Type checking where practical.
- Migrations check.
- Tests against PostgreSQL, not SQLite.
- OpenAPI schema generation check.
- Security/dependency scans.
- Basic accessibility smoke tests for core pages.

---

## 19. Architecture Decision Records

The repo should include ADRs documenting the major choices.

Required ADRs:

```text
ADR-0001: PostgreSQL full-text search is used instead of an external search service.
ADR-0002: Search vectors are stored and indexed, not computed per request.
ADR-0003: Work → Edition → Copy separates catalog identity from physical inventory.
ADR-0004: Availability is derived from copy state, not a mutable counter.
ADR-0005: Borrowing uses row locking to prevent last-copy races.
ADR-0006: Holds reserve a specific copy only when ready.
ADR-0007: Offset pagination is used for shallow human browse; cursor pagination for API/infinite scroll.
ADR-0008: Domain services own circulation mutations.
ADR-0009: Selectors own catalog/search/facet queries.
ADR-0010: Domain events and outbox drive audit, notifications, analytics, and support visibility.
ADR-0011: Branch support is launch-scope to avoid single-location assumptions.
ADR-0012: Django + HTMX is the primary web UX; DRF is the API surface.
ADR-0013: Organization is a tenant boundary; bibliographic data is a shared platform-global spine and holdings/circulation/patrons are tenant-scoped (union-catalog model).
ADR-0014: Search is indexed/ranked/paginated at the Work grain via a denormalized Work search row; edition matches collapse to one Work.
ADR-0015: Facet counts use single-pass conditional aggregation; result totals use cap-and-count/estimates, not exact counts over large sets.
ADR-0016: Hold-queue assignment is serialized per work_id (advisory lock / FIFO-head lock); Loan/Copy locks do not cover the contended queue head.
ADR-0017: API cursors are opaque and validated; ranked-search keyset is query-bound and its cost is acknowledged, with bounded offset an accepted fallback for ranked results.
ADR-0018: The API has three auth tiers — anonymous throttled public read, token/key for programmatic clients, session for the browser; CSRF posture follows the auth mode.
ADR-0019: Reader privacy is by design — the patron↔copy link is anonymized after return by default (opt-in lifetime history); patron erasure tombstones identity while preserving anonymized audit.
```

---

## 20. Tech Stack

- Python 3.x, Django latest stable.
- Django REST Framework.
- HTMX for live search, facets, infinite scroll, account actions, and staff workflows.
- PostgreSQL + `pg_trgm`.
- `django.contrib.postgres.search` (`SearchVector`, `SearchRank`, `SearchVectorField`, `TrigramSimilarity`).
- `django-environ` for configuration.
- `drf-spectacular` or equivalent for OpenAPI.
- Rate limiting/throttling library and/or DRF throttles.
- `django-csp` and `django-cors-headers` where needed.
- Object storage via `django-storages` in production.
- pytest, pytest-django, factory_boy/model_bakery.
- Ruff, Black/isort where preferred, mypy or pyright, django-stubs where practical.
- Sentry or equivalent error tracking.
- Gunicorn + WhiteNoise.
- Docker + docker-compose.
- Optional Celery/Celery Beat if notification/import/reindex workload justifies workers.

---

## 21. Definition of Done

1. Public catalog search returns relevance-ranked results from a stored, GIN-indexed PostgreSQL search vector at the Work grain (edition matches collapse to one Work).
2. Fuzzy search handles typo-tolerant queries using `pg_trgm`.
3. M2M author/subject updates and author renames correctly refresh the affected Works' search documents/vectors (fan-out reindex).
4. Faceted filters show live counts computed in a single pass that compose correctly with query and filter state; result totals use cap-and-count.
5. Offset pagination exists for shallow web browse; opaque, validated cursor/keyset pagination exists for API and HTMX infinite scroll.
6. Debounced HTMX search-as-you-type works with filters and result partials, served through the read cache.
7. Branch-aware availability is accurate across copies, branches, holds, and loans, filtered by organization.
8. The last-copy race is proven: concurrent borrow test succeeds once and only once.
9. Holds are FIFO; ready holds reserve a specific copy; the hold-queue head is serialized per work_id so concurrent returns cannot double-assign or skip a hold; expired ready holds reoffer to the next patron; branch-aware fulfillment/transfer is policy-driven.
10. Renewals work and are blocked when policy or waiting holds require it.
11. Overdue sweep flags overdue loans in the branch timezone and is idempotent.
12. Librarian dashboard supports daily operations: due, overdue, ready holds, expiring holds, copy history, and circulation queues.
13. Catalog governance supports statuses, duplicate detection, merge/split, ISBN normalization, and audited changes; bibliographic governance is a platform-level audited operation.
14. CSV import/export supports staged validation, commit, audit, and reindexing.
15. Patron notifications are event/outbox-driven, drained by a worker (`SKIP LOCKED`, idempotent), and logged.
16. Search analytics track query, filters, result count, latency, zero-result searches, and selected results where available.
17. Public API and authenticated circulation/librarian APIs are versioned under `/api/v1/`, OpenAPI-documented, throttled, and tested; auth tiers are anonymous (public read), token/key (programmatic), and session (browser).
18. Web and API layers reuse the same services and selectors; API cannot bypass business rules.
19. Tenancy holds: bibliographic data is shared, holdings/circulation/patron data are organization-scoped and never cross-org, proven by an isolation test suite.
20. Object-level permissions prevent patrons from accessing others' loans/holds/notifications.
21. Reader privacy holds: the patron↔copy link is anonymized after return by default; patron erasure tombstones identity while preserving anonymized audit.
22. Staff/support actions are audited with actor, reason where applicable, request ID, before/after, and timestamp.
23. Support console can inspect and repair stuck circulation/search states with audited reasons.
24. Production posture is verifiable: `DEBUG=False`, env secrets, HTTPS/cookies/CSRF, rate limits in a shared cache, upload validation, PII-safe logging, Sentry, `/healthz`.
25. CI runs tests against PostgreSQL, not SQLite; includes lint/type checks, migration checks, OpenAPI check, and security/dependency scans.
26. Performance targets are measured against a large seed dataset with `EXPLAIN ANALYZE` notes for critical queries.
27. `docker-compose up` runs the app with PostgreSQL, cache, worker, scheduler, seed data, migrations, and working web/API flows.

---

## 22. Suggested Build Order

1. Django + PostgreSQL + shared cache via docker-compose; enable `pg_trgm`; env-driven settings; `.env.example`.
2. Tenancy + Organization/Branch/ShelfLocation foundation: tenant-aware managers for org-scoped models, shared-spine vs tenant-scoped split (§3.1a), cross-org isolation test fixture, branch assumptions correct from the start.
3. Catalog model: Work, Edition, Author, Subject, Collection, Copy, statuses, constraints, and indexes; bibliographic data as shared spine, Copy as tenant-scoped.
4. Large seed dataset with tens of thousands of records and realistic authors/subjects/branches/copies.
5. Basic catalog browse with offset pagination.
6. Denormalized **Work-level search row**, document assembly, GIN-indexed vector, reindex command, and ranked FTS search at the Work grain.
7. Search-document maintenance: M2M author/subject hooks plus author-rename fan-out reindex; eventual-consistency model with `rebuild_search_index` backstop.
8. Trigram fuzzy matching and zero-result suggestions.
9. Faceted filtering with single-pass conditional aggregation, cap-and-count totals, org-filtered availability facets, and query-count tests.
10. HTMX search, filter partials, opaque/validated cursor infinite scroll, and read caching.
11. Auth, PatronProfile, StaffMembership, roles, branch-scoped permissions.
12. Circulation services: borrow, return, derived availability, audit/events.
13. Last-copy concurrency test; make it pass with row locking.
14. Holds: FIFO, ready assigned copy, pickup window, expiry and reoffer — with per-`work_id` queue serialization and a concurrent-returns test proving no double-assign/skip; branch-aware fulfillment/transfer.
15. Renewals and branch-timezone-aware overdue sweep.
16. Patron account pages: loans, holds, renewals, notification preferences.
17. Librarian dashboard and operational workflows.
18. Domain events, outbox worker (`SKIP LOCKED`) + scheduler, notifications, and delivery logs.
19. Catalog governance: statuses, duplicate detection, merge/split, ISBN validation (platform-level, audited).
20. Import/export pipeline with staged validation and reindexing.
21. DRF API: public (anonymous, ETag) catalog/search/facets, token/key patron circulation, librarian routes, session for browser, OpenAPI, throttling, stable errors.
22. Search analytics and admin tuning reports.
23. Support console with audited diagnostics/repairs.
24. Security pass: CSRF, HTTPS/cookies, CORS/CSP, rate limits, upload validation, PII logging, object permissions.
25. Observability: structured logs, Sentry, `/healthz`, metrics, slow-query checks.
26. CI/quality pass: PostgreSQL tests, lint, type checks, migration checks, OpenAPI checks, security scans.
27. Deployment pass: managed PostgreSQL, TLS, static/media storage, backup/restore notes, full Definition of Done walkthrough.

---

## 23. Non-Goals and Boundaries

- No fines/payments.
- No external search service.
- No Channels/WebSockets.
- No full MARC21 cataloging system unless deliberately added later.
- No inter-library loan network integration in this scope.
- No mobile app; responsive Django + HTMX web is the product surface.
- No recommendation engine beyond basic related works/collections and search analytics.

The test for future features: does it deepen catalog discovery, circulation correctness, library operations, or patron trust? If not, it does not belong in this build.

---

**End of revised scope.**

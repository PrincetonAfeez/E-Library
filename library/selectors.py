from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank, TrigramSimilarity
from django.core.cache import cache
from django.db import connection
from django.db.models import Count, Exists, F, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce

from .models import (
    Branch,
    Copy,
    CopyStatus,
    Edition,
    Hold,
    HoldStatus,
    Loan,
    LoanStatus,
    OutboxEvent,
    OutboxStatus,
    PublicStatus,
    SearchQueryLog,
    Subject,
    Work,
)
from .pagination import CursorError, decode_cursor, encode_cursor


@dataclass
class CatalogSearchPage:
    results: list[Work]
    facets: dict
    page: int
    per_page: int
    has_next: bool
    next_cursor: str | None
    result_count_label: str
    latency_ms: int
    did_you_mean: str | None = None


def base_visible_works(organization):
    return (
        Work.objects.filter(
            public_status=PublicStatus.PUBLISHED,
            editions__public_status=PublicStatus.PUBLISHED,
            editions__copies__organization=organization,
            editions__copies__public_visible=True,
        )
        .distinct()
        .prefetch_related("authors", "subjects", "editions")
    )


def _copy_exists(organization, work_ref="pk", **copy_filters):
    """Exists() subquery over a single copy row, so branch/status/visibility
    constraints all apply to the *same* copy instead of matching different
    copies via independent multi-valued joins."""
    return Exists(
        Copy.objects.filter(
            edition__work=OuterRef(work_ref),
            organization=organization,
            public_visible=True,
            **copy_filters,
        )
    )


def apply_catalog_filters(qs, organization, filters: dict):
    branch_slug = filters.get("branch")
    subject_slug = filters.get("subject")
    availability = filters.get("availability")

    if branch_slug:
        qs = qs.filter(_copy_exists(organization, branch__slug=branch_slug))
    if subject_slug:
        qs = qs.filter(subjects__slug=subject_slug)
    if availability == "available":
        qs = qs.filter(_copy_exists(organization, status=CopyStatus.AVAILABLE))
    elif availability == "held":
        qs = qs.filter(_copy_exists(organization, status=CopyStatus.ON_HOLD))
    return qs.distinct()


def apply_search(qs, query: str):
    query = (query or "").strip()
    if not query:
        return qs.order_by("canonical_title", "id")
    if connection.vendor == "postgresql":
        search_query = SearchQuery(
            query, config=settings.SEARCH_CONFIG, search_type="websearch"
        )
        return (
            qs.annotate(
                rank=SearchRank(F("search_row__search_vector"), search_query),
                similarity=TrigramSimilarity("normalized_title", query),
            )
            .filter(Q(search_row__search_vector=search_query) | Q(similarity__gt=0.15))
            .order_by("-rank", "-similarity", "canonical_title", "id")
        )
    return (
        qs.filter(
            Q(canonical_title__icontains=query)
            | Q(subtitle__icontains=query)
            | Q(summary__icontains=query)
            | Q(authors__name__icontains=query)
            | Q(subjects__name__icontains=query)
            | Q(editions__isbn_13__icontains=query)
        )
        .distinct()
        .order_by("canonical_title", "id")
    )


def get_facets_for_query(organization, query: str, filters: dict) -> dict:
    cache_key = f"facets:{organization.pk}:{query}:{sorted(filters.items())}"
    try:
        cached = cache.get(cache_key)
    except Exception:
        # A cache backend outage must not take down catalog search.
        cached = None
    if cached:
        return cached

    qs = apply_search(base_visible_works(organization), query)
    qs = apply_catalog_filters(
        qs, organization, {k: v for k, v in filters.items() if k != "branch"}
    )
    work_ids = qs.values("id")
    branches = list(
        Branch.objects.filter(
            organization=organization,
            copies__edition__work__in=work_ids,
            copies__public_visible=True,
        )
        .values("name", "slug")
        .annotate(count=Count("copies__edition__work", distinct=True))
        .order_by("name")
    )
    subjects = list(
        Subject.objects.filter(public=True, works__in=work_ids)
        .values("name", "slug")
        .annotate(count=Count("works", distinct=True))
        .order_by("name")[:16]
    )
    statuses = list(
        Copy.objects.filter(
            organization=organization, edition__work__in=work_ids, public_visible=True
        )
        .values("status")
        .annotate(count=Count("edition__work", distinct=True))
        .order_by("status")
    )
    facets = {"branches": branches, "subjects": subjects, "statuses": statuses}
    try:
        cache.set(cache_key, facets, 30)
    except Exception:
        pass
    return facets


def search_catalog(
    *,
    organization,
    query: str = "",
    filters: dict | None = None,
    page: int = 1,
    per_page: int = 12,
    cursor: str | None = None,
    log: bool = True,
    requester_hash: str = "",
) -> CatalogSearchPage:
    started = perf_counter()
    filters = filters or {}
    if cursor:
        payload = decode_cursor(cursor)
        cursor_query = payload.get("query", "")
        cursor_filters = payload.get("filters", {})
        if cursor_query != query or cursor_filters != filters:
            raise CursorError("Cursor does not match this query.")
        page = int(payload.get("page", page))

    qs = apply_catalog_filters(base_visible_works(organization), organization, filters)
    qs = apply_search(qs, query)

    page = max(1, int(page or 1))
    per_page = min(50, max(1, int(per_page or 12)))
    # Offset pagination is O(offset); cap depth so a crafted/paginated request
    # can't force an unbounded scan. (See ADR-0017 — deep results are truncated
    # rather than served via keyset.)
    max_offset = 1000
    if (page - 1) * per_page > max_offset:
        raise CursorError("Result depth limit reached; refine your search.")
    offset = (page - 1) * per_page
    rows = list(qs[offset : offset + per_page + 1])
    has_next = len(rows) > per_page
    results = rows[:per_page]
    next_cursor = (
        encode_cursor({"query": query, "filters": filters, "page": page + 1}) if has_next else None
    )

    count_cap = 501
    capped_count = qs.values("id")[:count_cap].count()
    result_count_label = f"{count_cap - 1}+" if capped_count >= count_cap else str(capped_count)
    facets = get_facets_for_query(organization, query, filters)
    # Offer a spelling correction only when a real query returned little/nothing.
    suggestion = None
    if query.strip() and capped_count <= 2 and not cursor:
        from .search import did_you_mean

        suggestion = did_you_mean(organization, query)
    latency_ms = int((perf_counter() - started) * 1000)
    # Only log committed searches: skip empty queries and cursor-paged follow-ups
    # so a debounced live-search session doesn't write a row per keystroke.
    if log and query.strip() and not cursor:
        SearchQueryLog.objects.create(
            organization=organization,
            query=query,
            filters=filters,
            result_count=min(capped_count, count_cap),
            latency_ms=latency_ms,
            user_or_session_hash=requester_hash,
        )
    return CatalogSearchPage(
        results=results,
        facets=facets,
        page=page,
        per_page=per_page,
        has_next=has_next,
        next_cursor=next_cursor,
        result_count_label=result_count_label,
        latency_ms=latency_ms,
        did_you_mean=suggestion,
    )


def availability_for_work(organization, work: Work) -> dict:
    counts = (
        Copy.objects.filter(organization=organization, edition__work=work, public_visible=True)
        .values("status")
        .annotate(count=Count("id"))
    )
    by_status = {row["status"]: row["count"] for row in counts}
    return _availability_from_status_counts(by_status)


def _availability_from_status_counts(by_status: dict) -> dict:
    available = by_status.get(CopyStatus.AVAILABLE, 0)
    loaned = by_status.get(CopyStatus.LOANED, 0)
    on_hold = by_status.get(CopyStatus.ON_HOLD, 0)
    return {
        "available": available,
        "loaned": loaned,
        "on_hold": on_hold,
        # Total of the buckets we actually surface, so the numbers reconcile;
        # hidden states (lost/retired/in_transit/repair) are excluded.
        "total": available + loaned + on_hold,
    }


def availability_map_for_works(organization, work_ids) -> dict:
    """Availability for many works in a single grouped query (avoids the
    per-work N+1 in the catalog list serializer)."""
    work_ids = list(work_ids)
    if not work_ids:
        return {}
    rows = (
        Copy.objects.filter(
            organization=organization, edition__work__in=work_ids, public_visible=True
        )
        .values("edition__work", "status")
        .annotate(count=Count("id"))
    )
    by_work: dict[int, dict] = {}
    for row in rows:
        by_work.setdefault(row["edition__work"], {})[row["status"]] = row["count"]
    return {wid: _availability_from_status_counts(by_work.get(wid, {})) for wid in work_ids}


def get_work_detail(organization, slug: str) -> Work:
    # Restrict prefetched editions/copies to this organization's public rows so
    # the detail page cannot leak other tenants' or non-public copies (and the
    # branch pickers only offer valid branches).
    edition_qs = Edition.objects.filter(public_status=PublicStatus.PUBLISHED)
    copy_qs = Copy.objects.filter(
        organization=organization, public_visible=True
    ).select_related("branch", "shelf_location")
    return (
        Work.objects.filter(
            public_status=PublicStatus.PUBLISHED,
            slug=slug,
            editions__public_status=PublicStatus.PUBLISHED,
            editions__copies__organization=organization,
            editions__copies__public_visible=True,
        )
        .distinct()
        .prefetch_related(
            "authors",
            "subjects",
            Prefetch("editions", queryset=edition_qs),
            Prefetch("editions__copies", queryset=copy_qs),
        )
        .get()
    )


def get_patron_loans(patron):
    return (
        Loan.objects.filter(
            organization=patron.organization,
            patron=patron,
            status__in=[LoanStatus.ACTIVE, LoanStatus.OVERDUE],
        )
        .select_related("copy", "copy__edition", "copy__edition__work", "copy__branch")
        .order_by("due_at")
    )


def get_patron_holds(patron):
    # Queue position = number of same-work active holds placed earlier, + 1.
    ahead = (
        Hold.objects.filter(
            organization=OuterRef("organization"),
            work=OuterRef("work"),
            status__in=[HoldStatus.WAITING, HoldStatus.READY],
            created_at__lt=OuterRef("created_at"),
        )
        .order_by()
        .values("work")
        .annotate(n=Count("*"))
        .values("n")
    )
    return (
        Hold.objects.filter(
            organization=patron.organization,
            patron=patron,
            status__in=[HoldStatus.WAITING, HoldStatus.READY],
        )
        .select_related("work", "preferred_branch", "assigned_copy")
        .annotate(queue_position=Coalesce(Subquery(ahead), 0) + 1)
        .order_by("created_at")
    )


def get_librarian_dashboard(organization, branch=None) -> dict:
    loan_qs = Loan.objects.filter(organization=organization).select_related(
        "copy", "copy__edition__work", "patron", "copy__branch"
    )
    hold_qs = Hold.objects.filter(organization=organization).select_related(
        "work", "patron", "preferred_branch", "assigned_copy"
    )
    if branch is not None:
        loan_qs = loan_qs.filter(copy__branch=branch)
        hold_qs = hold_qs.filter(preferred_branch=branch)
    return {
        "overdue_loans": loan_qs.filter(status=LoanStatus.OVERDUE).order_by("due_at")[:25],
        "due_today": loan_qs.filter(status=LoanStatus.ACTIVE).order_by("due_at")[:25],
        "ready_holds": hold_qs.filter(status=HoldStatus.READY).order_by("expires_at")[:25],
        "waiting_holds": hold_qs.filter(status=HoldStatus.WAITING).order_by("created_at")[:25],
        "failed_outbox_events": OutboxEvent.objects.filter(
            organization=organization, status=OutboxStatus.FAILED
        ).count(),
    }

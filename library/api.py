import csv
import hashlib

from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    inline_serializer,
)
from rest_framework import permissions, status
from rest_framework import serializers as drf_serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from .imports import (
    commit_import,
    parse_rows_from_csv,
    rollback_import,
    stage_import,
    validate_import,
)
from .models import (
    Branch,
    CatalogImportBatch,
    Copy,
    CopyStatus,
    Fee,
    FeeStatus,
    Hold,
    HoldStatus,
    Loan,
    LoanStatus,
    PatronProfile,
    PublicStatus,
    Work,
)
from .pagination import CursorError
from .permissions import (
    HasStaffPermission,
    IsAuthenticatedPatron,
    TokenHasScope,
    staff_branch_ids_for_org,
    user_can_act_on_branch,
)
from .selectors import (
    availability_map_for_works,
    get_librarian_dashboard,
    get_patron_holds,
    get_patron_loans,
    search_catalog,
)
from .serializers import HoldSerializer, LoanSerializer, WorkListSerializer
from .services import (
    DomainError,
    borrow_work,
    cancel_hold,
    move_copy,
    patron_balance_cents,
    place_hold,
    record_payment,
    renew_loan,
    retire_copy,
    return_loan,
    staff_checkin,
    waive_fee,
)
from .tenancy import get_current_organization

MAX_IMPORT_UPLOAD_BYTES = 5 * 1024 * 1024


def _reject_upload(upload, allowed_exts: tuple[str, ...]):
    """Return an error Response if the upload is too large or the wrong type.

    Extension is the practical guard (browser content-types are unreliable/
    spoofable); the parser then validates structure. Returns None if acceptable.
    """
    if upload.size and upload.size > MAX_IMPORT_UPLOAD_BYTES:
        return Response(
            {"error": {"code": "file_too_large", "message": "Import file exceeds 5 MB."}},
            status=400,
        )
    name = (getattr(upload, "name", "") or "").lower()
    if not name.endswith(allowed_exts):
        return Response(
            {"error": {
                "code": "unsupported_type",
                "message": f"Unsupported file type; expected one of {', '.join(allowed_exts)}.",
            }},
            status=400,
        )
    return None

# Characters that spreadsheet apps interpret as the start of a formula.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    """Neutralize CSV/formula injection: a cell that starts with a formula
    trigger is prefixed with an apostrophe so Excel/Sheets treats it as text."""
    text = "" if value is None else str(value)
    if text and text[0] in _CSV_FORMULA_PREFIXES:
        return "'" + text
    return text


def _import_batch_dict(batch, *, include_rows=False):
    data = {
        "id": batch.pk,
        "status": batch.status,
        "row_count": batch.row_count,
        "error_count": batch.error_count,
        "validation_summary": batch.validation_summary,
        "committed_at": batch.committed_at,
        "rolled_back_at": batch.rolled_back_at,
        "created_at": batch.created_at,
    }
    if include_rows:
        data["rows"] = [
            {
                "row_number": row.row_number,
                "validation_errors": row.validation_errors,
                "matched_existing": row.matched_existing,
            }
            for row in batch.rows.order_by("row_number")
        ]
    return data

ErrorSerializer = inline_serializer(
    name="ErrorEnvelope",
    fields={
        "error": drf_serializers.DictField(),
    },
)

CatalogSearchResponseSerializer = inline_serializer(
    name="CatalogSearchResponse",
    fields={
        "data": WorkListSerializer(many=True),
        "facets": drf_serializers.DictField(),
        "page": drf_serializers.IntegerField(),
        "has_next": drf_serializers.BooleanField(),
        "next_cursor": drf_serializers.CharField(allow_null=True),
        "result_count": drf_serializers.CharField(),
        "latency_ms": drf_serializers.IntegerField(),
    },
)

AccountResponseSerializer = inline_serializer(
    name="AccountResponse",
    fields={
        "loans": LoanSerializer(many=True),
        "holds": HoldSerializer(many=True),
    },
)

LibrarianDashboardResponseSerializer = inline_serializer(
    name="LibrarianDashboardResponse",
    fields={
        "overdue_loans": LoanSerializer(many=True),
        "ready_holds": HoldSerializer(many=True),
        "waiting_holds": HoldSerializer(many=True),
    },
)

BranchRequestSerializer = inline_serializer(
    name="BranchRequest",
    fields={"branch": drf_serializers.CharField(required=False, allow_blank=True)},
)


def api_organization(request):
    return getattr(request, "organization", None) or get_current_organization(request)


def _search_rate_limited(request) -> bool:
    """Throttle expensive public search endpoints per-IP (semantic/NL/suggest).

    Only anonymous callers are limited, mirroring the HTML catalog search, so
    signed-in patrons browsing are never blocked.
    """
    from .ratelimit import is_rate_limited

    if request.user and request.user.is_authenticated:
        return False
    return is_rate_limited(request, scope="api_search", limit=60, window=60)


def _api_requester_hash(request) -> str:
    if request.user and request.user.is_authenticated:
        base = f"user:{request.user.pk}"
    else:
        session_key = getattr(getattr(request, "session", None), "session_key", None)
        if not session_key:
            return ""
        base = f"session:{session_key}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


class CatalogSearchAPI(APIView):
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        parameters=[
            OpenApiParameter("q", str, OpenApiParameter.QUERY),
            OpenApiParameter("branch", str, OpenApiParameter.QUERY),
            OpenApiParameter("subject", str, OpenApiParameter.QUERY),
            OpenApiParameter("availability", str, OpenApiParameter.QUERY),
            OpenApiParameter("cursor", str, OpenApiParameter.QUERY),
            OpenApiParameter("page", int, OpenApiParameter.QUERY),
            OpenApiParameter("per_page", int, OpenApiParameter.QUERY),
        ],
        responses={200: CatalogSearchResponseSerializer, 400: ErrorSerializer},
    )
    def get(self, request):
        organization = api_organization(request)
        if organization is None:
            return Response(
                {"error": {"code": "no_organization", "message": "No active organization."}},
                status=404,
            )
        filters = {
            key: value
            for key, value in {
                "branch": request.GET.get("branch"),
                "subject": request.GET.get("subject"),
                "availability": request.GET.get("availability"),
            }.items()
            if value
        }
        try:
            page = search_catalog(
                organization=organization,
                query=request.GET.get("q", ""),
                filters=filters,
                page=int(request.GET.get("page", 1)),
                per_page=int(request.GET.get("per_page", 20)),
                cursor=request.GET.get("cursor"),
                requester_hash=_api_requester_hash(request),
            )
        except CursorError as exc:
            return Response({"error": {"code": "invalid_cursor", "message": str(exc)}}, status=400)
        availability_map = availability_map_for_works(
            organization, [work.id for work in page.results]
        )
        serializer = WorkListSerializer(
            page.results,
            many=True,
            context={"organization": organization, "availability_map": availability_map},
        )
        return Response(
            {
                "data": serializer.data,
                "facets": page.facets,
                "page": page.page,
                "has_next": page.has_next,
                "next_cursor": page.next_cursor,
                "result_count": page.result_count_label,
                "latency_ms": page.latency_ms,
                "did_you_mean": page.did_you_mean,
            }
        )


class SearchSuggestAPI(APIView):
    """Typeahead autocomplete over visible titles and authors."""

    permission_classes = [permissions.AllowAny]

    @extend_schema(
        parameters=[OpenApiParameter("q", str, OpenApiParameter.QUERY)],
        responses={200: OpenApiResponse(description="Suggestions")},
    )
    def get(self, request):
        from . import search

        if _search_rate_limited(request):
            return Response({"error": {"code": "rate_limited"}}, status=429)
        organization = api_organization(request)
        if organization is None:
            return Response({"data": []})
        suggestions = search.autocomplete(organization, request.GET.get("q", ""))
        return Response({"data": suggestions})


class SemanticSearchAPI(APIView):
    """Rank the catalog by semantic closeness to the query (local embedding)."""

    permission_classes = [permissions.AllowAny]

    @extend_schema(
        parameters=[
            OpenApiParameter("q", str, OpenApiParameter.QUERY),
            OpenApiParameter("limit", int, OpenApiParameter.QUERY),
        ],
        responses={200: CatalogSearchResponseSerializer},
    )
    def get(self, request):
        from . import search

        if _search_rate_limited(request):
            return Response({"error": {"code": "rate_limited"}}, status=429)
        organization = api_organization(request)
        if organization is None:
            return Response(
                {"error": {"code": "no_organization", "message": "No active organization."}},
                status=404,
            )
        try:
            limit = min(50, max(1, int(request.GET.get("limit", 20))))
        except (TypeError, ValueError):
            limit = 20
        works = search.semantic_search(organization, request.GET.get("q", ""), limit=limit)
        availability_map = availability_map_for_works(organization, [w.id for w in works])
        serializer = WorkListSerializer(
            works,
            many=True,
            context={"organization": organization, "availability_map": availability_map},
        )
        return Response({"data": serializer.data, "count": len(works)})


class WorkDetailAPI(APIView):
    permission_classes = [permissions.AllowAny]

    @extend_schema(responses={200: WorkListSerializer, 404: ErrorSerializer})
    def get(self, request, slug):
        organization = api_organization(request)
        if organization is None:
            return Response(
                {"error": {"code": "no_organization", "message": "No active organization."}},
                status=404,
            )
        work = get_object_or_404(
            Work,
            slug=slug,
            public_status=PublicStatus.PUBLISHED,
            editions__copies__organization=organization,
        )
        serializer = WorkListSerializer(work, context={"organization": organization})
        return Response({"data": serializer.data})


class AccountAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "patron:read"

    @extend_schema(responses={200: LibrarianDashboardResponseSerializer})
    def get(self, request):
        patron = request.user.patron_profile
        return Response(
            {
                "loans": LoanSerializer(get_patron_loans(patron), many=True).data,
                "holds": HoldSerializer(get_patron_holds(patron), many=True).data,
            }
        )


class BorrowAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    @extend_schema(
        request=BranchRequestSerializer,
        responses={201: LoanSerializer, 409: ErrorSerializer},
    )
    def post(self, request, slug):
        patron = request.user.patron_profile
        work = get_object_or_404(Work, slug=slug, public_status=PublicStatus.PUBLISHED)
        branch = None
        if request.data.get("branch"):
            branch = get_object_or_404(
                Branch, organization=patron.organization, slug=request.data["branch"]
            )
        try:
            loan = borrow_work(
                patron=patron, work=work, branch=branch, actor=request.user, source="api"
            )
        except DomainError as exc:
            return Response({"error": {"code": "borrow_blocked", "message": str(exc)}}, status=409)
        return Response({"data": LoanSerializer(loan).data}, status=status.HTTP_201_CREATED)


class PlaceHoldAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    @extend_schema(
        request=BranchRequestSerializer,
        responses={201: HoldSerializer, 409: ErrorSerializer},
    )
    def post(self, request, slug):
        patron = request.user.patron_profile
        work = get_object_or_404(Work, slug=slug, public_status=PublicStatus.PUBLISHED)
        branch = None
        if request.data.get("branch"):
            branch = get_object_or_404(
                Branch, organization=patron.organization, slug=request.data["branch"]
            )
        try:
            hold = place_hold(
                patron=patron, work=work, preferred_branch=branch, actor=request.user, source="api"
            )
        except DomainError as exc:
            return Response({"error": {"code": "hold_blocked", "message": str(exc)}}, status=409)
        return Response({"data": HoldSerializer(hold).data}, status=status.HTTP_201_CREATED)


class RenewLoanAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    @extend_schema(request=None, responses={200: LoanSerializer, 409: ErrorSerializer})
    def post(self, request, pk):
        patron = request.user.patron_profile
        loan = get_object_or_404(Loan, pk=pk, organization=patron.organization, patron=patron)
        try:
            renew_loan(loan=loan, actor=request.user, source="api")
        except DomainError as exc:
            return Response({"error": {"code": "renewal_blocked", "message": str(exc)}}, status=409)
        loan.refresh_from_db()
        return Response({"data": LoanSerializer(loan).data})


class ReturnLoanAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    @extend_schema(request=None, responses={204: OpenApiResponse(description="Returned")})
    def post(self, request, pk):
        patron = request.user.patron_profile
        loan = get_object_or_404(Loan, pk=pk, organization=patron.organization, patron=patron)
        try:
            return_loan(loan=loan, actor=request.user, source="api")
        except DomainError as exc:
            return Response({"error": {"code": "return_blocked", "message": str(exc)}}, status=409)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CancelHoldAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    @extend_schema(request=None, responses={204: OpenApiResponse(description="Cancelled")})
    def post(self, request, pk):
        patron = request.user.patron_profile
        hold = get_object_or_404(Hold, pk=pk, organization=patron.organization, patron=patron)
        try:
            cancel_hold(hold=hold, actor=request.user, source="api")
        except DomainError as exc:
            return Response({"error": {"code": "cancel_blocked", "message": str(exc)}}, status=409)
        return Response(status=status.HTTP_204_NO_CONTENT)


class LibrarianDashboardAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:read"
    required_staff_permission = "reports"

    @extend_schema(responses={200: AccountResponseSerializer})
    def get(self, request):
        organization = api_organization(request)
        if organization is None:
            return Response(
                {"error": {"code": "no_organization", "message": "No active organization."}},
                status=404,
            )
        dashboard = get_librarian_dashboard(organization)
        return Response(
            {
                "overdue_loans": LoanSerializer(dashboard["overdue_loans"], many=True).data,
                "ready_holds": HoldSerializer(dashboard["ready_holds"], many=True).data,
                "waiting_holds": HoldSerializer(dashboard["waiting_holds"], many=True).data,
            }
        )


class LibrarianImportsAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "imports"

    def _organization(self, request):
        return api_organization(request)

    @extend_schema(responses={200: OpenApiResponse(description="Import batches")})
    def get(self, request):
        organization = self._organization(request)
        if organization is None:
            return Response(
                {"error": {"code": "no_organization", "message": "No active organization."}},
                status=404,
            )
        batches = CatalogImportBatch.objects.filter(organization=organization)[:50]
        return Response({"data": [_import_batch_dict(batch) for batch in batches]})

    @extend_schema(
        request=None,
        responses={201: OpenApiResponse(description="Staged and validated batch")},
    )
    def post(self, request):
        organization = self._organization(request)
        if organization is None:
            return Response(
                {"error": {"code": "no_organization", "message": "No active organization."}},
                status=404,
            )
        rows = None
        if request.FILES.get("file"):
            upload = request.FILES["file"]
            rejected = _reject_upload(upload, (".csv", ".txt"))
            if rejected is not None:
                return rejected
            rows = parse_rows_from_csv(upload.read())
        elif isinstance(request.data, dict) and request.data.get("rows") is not None:
            rows = request.data["rows"]
        elif isinstance(request.data, dict) and request.data.get("csv"):
            rows = parse_rows_from_csv(request.data["csv"])
        if not rows:
            return Response(
                {"error": {"code": "no_rows", "message": "Provide a CSV file, csv text, or rows."}},
                status=400,
            )
        try:
            batch = stage_import(organization=organization, rows=rows, uploaded_by=request.user)
            validate_import(batch=batch)
        except DomainError as exc:
            return Response({"error": {"code": "invalid_rows", "message": str(exc)}}, status=400)
        batch.refresh_from_db()
        return Response(
            {"data": _import_batch_dict(batch, include_rows=True)},
            status=status.HTTP_201_CREATED,
        )


class LibrarianImportCommitAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "imports"

    @extend_schema(request=None, responses={200: OpenApiResponse(description="Committed batch")})
    def post(self, request, pk):
        organization = api_organization(request)
        batch = get_object_or_404(CatalogImportBatch, pk=pk, organization=organization)
        try:
            commit_import(batch=batch, actor=request.user)
        except DomainError as exc:
            return Response(
                {"error": {"code": "commit_blocked", "message": str(exc)}}, status=409
            )
        batch.refresh_from_db()
        return Response({"data": _import_batch_dict(batch)})


class LibrarianImportRollbackAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "imports"

    @extend_schema(request=None, responses={200: OpenApiResponse(description="Rolled back batch")})
    def post(self, request, pk):
        organization = api_organization(request)
        batch = get_object_or_404(CatalogImportBatch, pk=pk, organization=organization)
        reason = ""
        if isinstance(request.data, dict):
            reason = request.data.get("reason", "")
        try:
            rollback_import(batch=batch, actor=request.user, reason=reason)
        except DomainError as exc:
            return Response(
                {"error": {"code": "rollback_blocked", "message": str(exc)}}, status=409
            )
        batch.refresh_from_db()
        return Response({"data": _import_batch_dict(batch)})


def _require_branch(request, organization, branch_id, code):
    """Return an error Response if the staff member may not act on the branch."""
    if not user_can_act_on_branch(request.user, organization, branch_id):
        return Response(
            {"error": {"code": code, "message": "Not permitted for this branch."}}, status=403
        )
    return None


class StaffCheckoutAPI(APIView):
    """Desk checkout on behalf of a patron (card number), staff override optional."""

    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "circulation"

    def post(self, request):
        organization = api_organization(request)
        data = request.data if isinstance(request.data, dict) else {}
        patron = get_object_or_404(
            PatronProfile, organization=organization, library_card_number=data.get("card_number")
        )
        work = get_object_or_404(
            Work, slug=data.get("work_slug"), public_status=PublicStatus.PUBLISHED
        )
        branch = None
        if data.get("branch"):
            branch = get_object_or_404(Branch, organization=organization, slug=data["branch"])
            denied = _require_branch(request, organization, branch.id, "branch_forbidden")
            if denied:
                return denied
        else:
            # A branch-scoped staff member must check out at a branch they manage;
            # default to it when there's exactly one, else require an explicit choice
            # (otherwise borrow_work could pick a copy at a branch they can't touch).
            allowed = staff_branch_ids_for_org(request.user, organization)
            if allowed is not None:
                if len(allowed) == 1:
                    branch = Branch.objects.get(pk=next(iter(allowed)))
                else:
                    return Response(
                        {
                            "error": {
                                "code": "branch_required",
                                "message": "Specify a branch you manage for this checkout.",
                            }
                        },
                        status=400,
                    )
        try:
            loan = borrow_work(
                patron=patron,
                work=work,
                branch=branch,
                actor=request.user,
                source="staff",
                override_reason=data.get("override_reason", ""),
            )
        except DomainError as exc:
            return Response({"error": {"code": "checkout_blocked", "message": str(exc)}}, status=409)
        return Response({"data": LoanSerializer(loan).data}, status=status.HTTP_201_CREATED)


class StaffCheckinAPI(APIView):
    """Desk check-in by barcode: receives an in-transit copy or returns its loan."""

    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "circulation"

    def post(self, request):
        organization = api_organization(request)
        data = request.data if isinstance(request.data, dict) else {}
        copy = get_object_or_404(Copy, organization=organization, barcode=data.get("barcode"))
        # An in-transit copy is received at its *destination*, but its record still
        # shows the source branch; authorize against either so the receiving-branch
        # staff can check it in.
        allowed_branch_ids = {copy.branch_id}
        if copy.status == CopyStatus.IN_TRANSIT:
            transit_hold = (
                Hold.objects.filter(assigned_copy=copy, status=HoldStatus.WAITING)
                .order_by("created_at", "id")
                .first()
            )
            if transit_hold is not None:
                allowed_branch_ids.add(transit_hold.preferred_branch_id)
        if not any(
            user_can_act_on_branch(request.user, organization, bid) for bid in allowed_branch_ids
        ):
            return Response(
                {"error": {"code": "branch_forbidden", "message": "Not permitted for this branch."}},
                status=403,
            )
        try:
            result = staff_checkin(copy=copy, actor=request.user)
        except DomainError as exc:
            return Response({"error": {"code": "checkin_blocked", "message": str(exc)}}, status=409)
        outcome = "hold_ready" if isinstance(result, Hold) else "returned" if result else "none"
        return Response({"data": {"barcode": copy.barcode, "outcome": outcome}})


class CopyRetireAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "copies"

    def post(self, request):
        organization = api_organization(request)
        data = request.data if isinstance(request.data, dict) else {}
        copy = get_object_or_404(Copy, organization=organization, barcode=data.get("barcode"))
        denied = _require_branch(request, organization, copy.branch_id, "branch_forbidden")
        if denied:
            return denied
        try:
            retire_copy(copy=copy, actor=request.user, reason=data.get("reason", ""))
        except DomainError as exc:
            return Response({"error": {"code": "retire_blocked", "message": str(exc)}}, status=409)
        return Response({"data": {"barcode": copy.barcode, "status": "retired"}})


class CopyMoveAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "copies"

    def post(self, request):
        organization = api_organization(request)
        data = request.data if isinstance(request.data, dict) else {}
        copy = get_object_or_404(Copy, organization=organization, barcode=data.get("barcode"))
        to_branch = get_object_or_404(
            Branch, organization=organization, slug=data.get("to_branch")
        )
        for branch_id in (copy.branch_id, to_branch.id):
            denied = _require_branch(request, organization, branch_id, "branch_forbidden")
            if denied:
                return denied
        try:
            move_copy(copy=copy, to_branch=to_branch, actor=request.user, reason=data.get("reason", ""))
        except DomainError as exc:
            return Response({"error": {"code": "move_blocked", "message": str(exc)}}, status=409)
        return Response({"data": {"barcode": copy.barcode, "branch": to_branch.slug}})


class _CSVEcho:
    """A file-like object whose write() returns the value, for streaming CSV."""

    def write(self, value):
        return value


class LibrarianExportAPI(APIView):
    """Permission-gated, streamed CSV exports (loans, overdue, holds, inventory)."""

    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:read"
    required_staff_permission = "reports"

    def get(self, request):
        organization = api_organization(request)
        if organization is None:
            return Response(
                {"error": {"code": "no_organization", "message": "No active organization."}},
                status=404,
            )
        export_type = request.GET.get("type", "loans")
        exporters = {
            "loans": self._loans,
            "overdue": self._overdue,
            "holds": self._holds,
            "inventory": self._inventory,
        }
        exporter = exporters.get(export_type)
        if exporter is None:
            return Response(
                {"error": {"code": "unknown_export", "message": "Unknown export type."}}, status=400
            )
        writer = csv.writer(_CSVEcho())

        def stream():
            for row in exporter(organization):
                yield writer.writerow([_csv_safe(cell) for cell in row])

        response = StreamingHttpResponse(stream(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{export_type}.csv"'
        return response

    def _loans(self, organization):
        yield ["barcode", "title", "branch", "borrowed_at", "due_at", "status"]
        for loan in (
            Loan.objects.filter(
                organization=organization, status__in=[LoanStatus.ACTIVE, LoanStatus.OVERDUE]
            )
            .select_related("copy", "copy__edition__work", "copy__branch")
            .iterator()
        ):
            yield [
                loan.copy.barcode,
                loan.copy.edition.work.canonical_title,
                loan.copy.branch.name,
                loan.borrowed_at.isoformat(),
                loan.due_at.isoformat(),
                loan.status,
            ]

    def _overdue(self, organization):
        yield ["barcode", "title", "branch", "due_at"]
        for loan in (
            Loan.objects.filter(organization=organization, status=LoanStatus.OVERDUE)
            .select_related("copy", "copy__edition__work", "copy__branch")
            .iterator()
        ):
            yield [
                loan.copy.barcode,
                loan.copy.edition.work.canonical_title,
                loan.copy.branch.name,
                loan.due_at.isoformat(),
            ]

    def _holds(self, organization):
        yield ["title", "pickup_branch", "status", "created_at", "expires_at"]
        for hold in (
            Hold.objects.filter(organization=organization)
            .exclude(status__in=["fulfilled", "cancelled", "expired"])
            .select_related("work", "preferred_branch")
            .iterator()
        ):
            yield [
                hold.work.canonical_title,
                hold.preferred_branch.name,
                hold.status,
                hold.created_at.isoformat(),
                hold.expires_at.isoformat() if hold.expires_at else "",
            ]

    def _inventory(self, organization):
        yield ["barcode", "title", "branch", "status", "condition"]
        for copy in (
            Copy.objects.filter(organization=organization)
            .select_related("edition__work", "branch")
            .iterator()
        ):
            yield [
                copy.barcode,
                copy.edition.work.canonical_title,
                copy.branch.name,
                copy.status,
                copy.condition,
            ]


def _fee_dict(fee):
    return {
        "id": fee.pk,
        "type": fee.fee_type,
        "amount_cents": fee.amount_cents,
        "paid_cents": fee.paid_cents,
        "balance_cents": fee.balance_cents,
        "status": fee.status,
        "description": fee.description,
        "created_at": fee.created_at,
    }


class AccountFeesAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "patron:read"

    @extend_schema(responses={200: OpenApiResponse(description="Outstanding fees and balance")})
    def get(self, request):
        patron = request.user.patron_profile
        fees = (
            Fee.objects.filter(patron=patron)
            .exclude(status=FeeStatus.WAIVED)
            .order_by("-created_at")
        )
        return Response(
            {"balance_cents": patron_balance_cents(patron), "fees": [_fee_dict(f) for f in fees]}
        )


class PayFeesAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    @extend_schema(request=None, responses={201: OpenApiResponse(description="Payment recorded")})
    def post(self, request):
        patron = request.user.patron_profile
        data = request.data if isinstance(request.data, dict) else {}
        try:
            amount = int(data.get("amount_cents") or 0)
        except (TypeError, ValueError):
            return Response(
                {"error": {"code": "bad_amount", "message": "amount_cents must be an integer."}},
                status=400,
            )
        try:
            payment = record_payment(
                patron=patron,
                amount_cents=amount,
                method=data.get("method", "online"),
                reference=data.get("reference", ""),
                actor=request.user,
            )
        except DomainError as exc:
            return Response({"error": {"code": "payment_failed", "message": str(exc)}}, status=400)
        return Response(
            {"data": {"payment_id": payment.pk, "balance_cents": patron_balance_cents(patron)}},
            status=status.HTTP_201_CREATED,
        )


class WaiveFeeAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "circulation"

    @extend_schema(request=None, responses={200: OpenApiResponse(description="Fee waived")})
    def post(self, request, pk):
        organization = api_organization(request)
        fee = get_object_or_404(Fee, pk=pk, organization=organization)
        reason = request.data.get("reason", "") if isinstance(request.data, dict) else ""
        waive_fee(fee=fee, actor=request.user, reason=reason)
        fee.refresh_from_db()
        return Response({"data": _fee_dict(fee)})


class BillingAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:read"
    required_staff_permission = "billing"

    @extend_schema(responses={200: OpenApiResponse(description="Billing overview")})
    def get(self, request):
        from . import billing

        organization = api_organization(request)
        if organization is None:
            return Response(
                {"error": {"code": "no_organization", "message": "No active organization."}},
                status=404,
            )
        overview = billing.billing_overview(organization)
        sub = overview["subscription"]
        return Response(
            {
                "subscription": None
                if sub is None
                else {
                    "plan": sub.plan.slug,
                    "status": sub.status,
                    "trial_ends_at": sub.trial_ends_at,
                    "current_period_end": sub.current_period_end,
                    "dunning_attempts": sub.dunning_attempts,
                    "grace_until": sub.grace_until,
                },
                "usage": overview["usage"],
                "limits": overview["limits"],
                "payment_methods": [
                    {
                        "id": pm.pk,
                        "brand": pm.brand,
                        "last4": pm.last4,
                        "exp_month": pm.exp_month,
                        "exp_year": pm.exp_year,
                        "is_default": pm.is_default,
                    }
                    for pm in overview["payment_methods"]
                ],
                "invoices": [
                    {
                        "id": inv.pk,
                        "amount_cents": inv.amount_cents,
                        "status": inv.status,
                        "description": inv.description,
                        "period_end": inv.period_end,
                        "paid_at": inv.paid_at,
                    }
                    for inv in overview["invoices"]
                ],
            }
        )


class ChangePlanAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "billing"

    @extend_schema(request=None, responses={200: OpenApiResponse(description="Plan changed")})
    def post(self, request):
        from . import billing
        from .models import Plan

        organization = api_organization(request)
        data = request.data if isinstance(request.data, dict) else {}
        plan = get_object_or_404(Plan, slug=data.get("plan"), active=True)
        subscription = billing.get_subscription(organization)
        try:
            if subscription is None:
                subscription = billing.subscribe(
                    organization=organization, plan=plan, actor=request.user
                )
            else:
                subscription = billing.change_plan(
                    subscription=subscription, new_plan=plan, actor=request.user
                )
        except billing.BillingError as exc:
            return Response({"error": {"code": "billing_blocked", "message": str(exc)}}, status=409)
        return Response({"data": {"plan": subscription.plan.slug, "status": subscription.status}})


class CancelSubscriptionAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "billing"

    @extend_schema(request=None, responses={200: OpenApiResponse(description="Canceled")})
    def post(self, request):
        from . import billing

        organization = api_organization(request)
        subscription = billing.get_subscription(organization)
        if subscription is None:
            return Response(
                {"error": {"code": "no_subscription", "message": "No subscription to cancel."}},
                status=404,
            )
        billing.cancel_subscription(subscription=subscription, actor=request.user)
        return Response({"data": {"status": subscription.status}})


class CheckoutAPI(APIView):
    """Open a simulated hosted-checkout session for a plan."""

    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "billing"

    @extend_schema(request=None, responses={201: OpenApiResponse(description="Checkout opened")})
    def post(self, request):
        from . import billing
        from .models import Plan

        organization = api_organization(request)
        data = request.data if isinstance(request.data, dict) else {}
        plan = get_object_or_404(Plan, slug=data.get("plan"), active=True)
        session = billing.create_checkout(
            organization=organization, plan=plan, actor=request.user
        )
        return Response(
            {
                "data": {
                    "token": session.token,
                    "plan": plan.slug,
                    "amount_cents": plan.price_cents,
                    "complete_url": f"/api/v1/billing/checkout/{session.token}/complete/",
                }
            },
            status=201,
        )


class CheckoutCompleteAPI(APIView):
    """Complete a checkout session by supplying a (simulated) card."""

    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "billing"

    @extend_schema(request=None, responses={200: OpenApiResponse(description="Subscription active")})
    def post(self, request, token):
        from . import billing
        from .models import CheckoutSession

        organization = api_organization(request)
        session = get_object_or_404(
            CheckoutSession, token=token, organization=organization
        )
        data = request.data if isinstance(request.data, dict) else {}
        try:
            subscription = billing.complete_checkout(
                session=session,
                brand=data.get("brand", "visa"),
                last4=str(data.get("last4", "4242")),
                exp_month=int(data.get("exp_month", 12)),
                exp_year=int(data.get("exp_year", 2030)),
                actor=request.user,
            )
        except billing.BillingError as exc:
            return Response(
                {"error": {"code": "payment_declined", "message": str(exc)}}, status=402
            )
        except (TypeError, ValueError):
            return Response(
                {"error": {"code": "invalid_card", "message": "Invalid card details."}}, status=400
            )
        return Response(
            {"data": {"plan": subscription.plan.slug, "status": subscription.status}}
        )


class PaymentMethodAPI(APIView):
    """Add a (simulated) card on file for the tenant."""

    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "billing"

    @extend_schema(request=None, responses={201: OpenApiResponse(description="Card stored")})
    def post(self, request):
        from . import billing

        organization = api_organization(request)
        data = request.data if isinstance(request.data, dict) else {}
        try:
            method = billing.add_payment_method(
                organization=organization,
                brand=data.get("brand", "visa"),
                last4=str(data.get("last4", "4242")),
                exp_month=int(data.get("exp_month", 12)),
                exp_year=int(data.get("exp_year", 2030)),
                make_default=bool(data.get("make_default", True)),
                actor=request.user,
            )
        except (TypeError, ValueError):
            return Response(
                {"error": {"code": "invalid_card", "message": "Invalid card details."}}, status=400
            )
        return Response(
            {"data": {"id": method.pk, "brand": method.brand, "last4": method.last4}}, status=201
        )


class StripeWebhookAPI(APIView):
    """Payment-provider webhook receiver. Signature is verified when configured."""

    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []

    def post(self, request):
        from django.conf import settings

        from . import billing

        secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "")
        event = request.data if isinstance(request.data, dict) else {}
        if secret:
            try:  # pragma: no cover - only when Stripe is configured
                import stripe

                event = stripe.Webhook.construct_event(
                    request.body, request.META.get("HTTP_STRIPE_SIGNATURE", ""), secret
                )
            except Exception:
                return Response({"error": {"code": "bad_signature"}}, status=400)
        handled = billing.handle_gateway_event(dict(event))
        return Response({"handled": handled})


class LibrarianMarcImportAPI(APIView):
    """Stage + validate a MARC upload (binary .mrc or MARCXML) into a batch."""

    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "imports"

    def post(self, request):
        from .imports import import_marc

        organization = api_organization(request)
        if organization is None:
            return Response(
                {"error": {"code": "no_organization", "message": "No active organization."}},
                status=404,
            )
        upload = request.FILES.get("file")
        if upload is not None:
            rejected = _reject_upload(upload, (".mrc", ".marc", ".xml", ".marcxml"))
            if rejected is not None:
                return rejected
            content = upload.read()
        elif isinstance(request.data, dict) and request.data.get("marc"):
            content = request.data["marc"]
        else:
            return Response(
                {"error": {"code": "no_marc", "message": "Provide a MARC file or marc content."}},
                status=400,
            )
        try:
            batch = import_marc(organization=organization, content=content, uploaded_by=request.user)
        except DomainError as exc:
            return Response({"error": {"code": "invalid_marc", "message": str(exc)}}, status=400)
        batch.refresh_from_db()
        return Response(
            {"data": _import_batch_dict(batch, include_rows=True)}, status=status.HTTP_201_CREATED
        )


class MarcExportAPI(APIView):
    """Export the organization's editions as MARC (binary or MARCXML)."""

    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:read"
    required_staff_permission = "reports"

    def get(self, request):
        from .marc import edition_to_marc_record, to_iso2709, to_marcxml
        from .models import Edition

        organization = api_organization(request)
        if organization is None:
            return Response(
                {"error": {"code": "no_organization", "message": "No active organization."}},
                status=404,
            )
        editions = (
            Edition.objects.filter(copies__organization=organization)
            .select_related("work")
            .prefetch_related("work__authors", "work__subjects")
            .distinct()[:5000]
        )
        records = [edition_to_marc_record(e) for e in editions]
        # NB: use "fmt" (not "format") — DRF reserves ?format= for renderer negotiation.
        fmt = request.GET.get("fmt", "xml")
        if fmt == "marc":
            body, content_type, filename = to_iso2709(records), "application/marc", "catalog.mrc"
        else:
            body, content_type, filename = to_marcxml(records), "application/xml", "catalog.xml"
        response = HttpResponse(body, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class EditionEnrichAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "catalog"

    def post(self, request, pk):
        from .enrichment import enrich_edition
        from .models import Edition

        organization = api_organization(request)
        edition = get_object_or_404(
            Edition, pk=pk, copies__organization=organization
        )
        changed = enrich_edition(edition=edition, actor=request.user)
        return Response({"data": {"edition_id": edition.pk, "enriched": changed}})


class LibrarianReportsAPI(APIView):
    """Analytics reports (JSON). ?type=circulation|popular|collection|overdue|
    holds|fines|branches|search  &  ?days=30 (or ?start=&end= ISO datetimes)."""

    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:read"
    required_staff_permission = "reports"

    def get(self, request):
        from datetime import timedelta

        from django.utils import timezone
        from django.utils.dateparse import parse_datetime

        from . import reporting

        organization = api_organization(request)
        if organization is None:
            return Response(
                {"error": {"code": "no_organization", "message": "No active organization."}},
                status=404,
            )
        report_type = request.GET.get("type", "circulation")
        try:
            days = max(1, min(730, int(request.GET.get("days", 30))))
        except (TypeError, ValueError):
            days = 30
        end = parse_datetime(request.GET.get("end", "")) or timezone.now()
        start = parse_datetime(request.GET.get("start", "")) or (end - timedelta(days=days))
        data = reporting.build_report(report_type, organization, start, end)
        if data is None:
            return Response(
                {"error": {"code": "unknown_report", "message": "Unknown report type."}}, status=400
            )
        return Response(
            {"type": report_type, "start": start, "end": end, "data": data}
        )


class CirculationPoliciesAPI(APIView):
    """Read the organization's circulation matrix (patron_type × material_type)."""

    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:read"
    required_staff_permission = "reports"

    def get(self, request):
        from .models import CirculationPolicy, MaterialType, PatronType

        organization = api_organization(request)
        if organization is None:
            return Response(
                {"error": {"code": "no_organization", "message": "No active organization."}},
                status=404,
            )
        policies_qs = CirculationPolicy.objects.filter(organization=organization).select_related(
            "patron_type", "material_type"
        )
        return Response(
            {
                "patron_types": [
                    {"code": pt.code, "name": pt.name, "max_loans": pt.max_loans, "max_holds": pt.max_holds}
                    for pt in PatronType.objects.filter(organization=organization)
                ],
                "material_types": [
                    {"code": mt.code, "name": mt.name}
                    for mt in MaterialType.objects.filter(organization=organization)
                ],
                "matrix": [
                    {
                        "patron_type": p.patron_type.code if p.patron_type else None,
                        "material_type": p.material_type.code if p.material_type else None,
                        "loan_days": p.loan_days,
                        "max_renewals": p.max_renewals,
                        "hold_pickup_days": p.hold_pickup_days,
                        "holdable": p.holdable,
                        "daily_overdue_cents": p.daily_overdue_cents,
                        "max_overdue_cents": p.max_overdue_cents,
                    }
                    for p in policies_qs
                ],
            }
        )


def _digital_loan_dict(loan):
    return {
        "id": loan.pk,
        "title": loan.license.edition.work.canonical_title,
        "started_at": loan.started_at,
        "expires_at": loan.expires_at,
        "status": loan.status,
        "access_token": loan.access_token if loan.status == "active" else None,
    }


def _digital_hold_dict(hold):
    return {
        "id": hold.pk,
        "title": hold.edition.work.canonical_title,
        "status": hold.status,
        "ready_at": hold.ready_at,
        "expires_at": hold.expires_at,
    }


class BorrowDigitalAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    def post(self, request, pk):
        from . import digital
        from .entitlements import EntitlementError
        from .models import Edition

        patron = request.user.patron_profile
        edition = get_object_or_404(Edition, pk=pk)
        try:
            loan = digital.borrow_digital(patron=patron, edition=edition, actor=request.user, source="api")
        except EntitlementError as exc:
            return Response({"error": {"code": "not_entitled", "message": str(exc)}}, status=403)
        except DomainError as exc:
            return Response({"error": {"code": "borrow_blocked", "message": str(exc)}}, status=409)
        return Response({"data": _digital_loan_dict(loan)}, status=status.HTTP_201_CREATED)


class ReturnDigitalAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    def post(self, request, pk):
        from . import digital
        from .models import DigitalLoan

        patron = request.user.patron_profile
        loan = get_object_or_404(DigitalLoan, pk=pk, organization=patron.organization, patron=patron)
        try:
            digital.return_digital(loan=loan, actor=request.user, source="api")
        except DomainError as exc:
            return Response({"error": {"code": "return_blocked", "message": str(exc)}}, status=409)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PlaceDigitalHoldAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    def post(self, request, pk):
        from . import digital
        from .entitlements import EntitlementError
        from .models import Edition

        patron = request.user.patron_profile
        edition = get_object_or_404(Edition, pk=pk)
        try:
            hold = digital.place_digital_hold(patron=patron, edition=edition, actor=request.user, source="api")
        except EntitlementError as exc:
            return Response({"error": {"code": "not_entitled", "message": str(exc)}}, status=403)
        except DomainError as exc:
            return Response({"error": {"code": "hold_blocked", "message": str(exc)}}, status=409)
        return Response({"data": _digital_hold_dict(hold)}, status=status.HTTP_201_CREATED)


class CancelDigitalHoldAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    def post(self, request, pk):
        from . import digital
        from .models import DigitalHold

        patron = request.user.patron_profile
        hold = get_object_or_404(DigitalHold, pk=pk, organization=patron.organization, patron=patron)
        try:
            digital.cancel_digital_hold(hold=hold, actor=request.user, source="api")
        except DomainError as exc:
            return Response({"error": {"code": "cancel_blocked", "message": str(exc)}}, status=409)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AccessDigitalAPI(APIView):
    """Gated 'reader/player' access: validates the patron's active loan."""

    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "patron:read"

    def get(self, request, pk):
        from . import digital
        from .models import DigitalLoan

        patron = request.user.patron_profile
        loan = get_object_or_404(DigitalLoan, pk=pk, organization=patron.organization, patron=patron)
        try:
            info = digital.access_content(access_token=loan.access_token)
        except DomainError as exc:
            return Response({"error": {"code": "access_denied", "message": str(exc)}}, status=403)
        return Response({"data": info})


class DigitalReaderAPI(APIView):
    """Return a secure reading manifest (chapters/media + signed content tokens)."""

    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "patron:read"

    def get(self, request, pk):
        from . import delivery
        from .models import DigitalLoan

        patron = request.user.patron_profile
        loan = get_object_or_404(DigitalLoan, pk=pk, organization=patron.organization, patron=patron)
        try:
            manifest = delivery.access_manifest(access_token=loan.access_token)
        except DomainError as exc:
            return Response({"error": {"code": "access_denied", "message": str(exc)}}, status=403)
        return Response({"data": manifest})


class DigitalProgressAPI(APIView):
    """Sync a patron's reading/listening position for a loan (cross-device bookmarks)."""

    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    def post(self, request, pk):
        from . import delivery
        from .models import DigitalLoan, DigitalLoanStatus

        patron = request.user.patron_profile
        loan = get_object_or_404(
            DigitalLoan, pk=pk, organization=patron.organization, patron=patron,
            status=DigitalLoanStatus.ACTIVE,
        )
        data = request.data if isinstance(request.data, dict) else {}
        try:
            progress = delivery.save_progress(
                loan, locator=str(data.get("locator", "")), percent=float(data.get("percent", 0))
            )
        except (TypeError, ValueError):
            return Response(
                {"error": {"code": "invalid_progress", "message": "Invalid progress."}}, status=400
            )
        return Response({"data": {"locator": progress.locator, "percent": progress.percent}})


class DigitalAccountAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "patron:read"

    def get(self, request):
        from .models import DigitalHold, DigitalHoldStatus, DigitalLoan, DigitalLoanStatus

        patron = request.user.patron_profile
        loans = (
            DigitalLoan.objects.filter(patron=patron, status=DigitalLoanStatus.ACTIVE)
            .select_related("license__edition__work")
            .order_by("expires_at")
        )
        holds = (
            DigitalHold.objects.filter(
                patron=patron, status__in=[DigitalHoldStatus.WAITING, DigitalHoldStatus.READY]
            )
            .select_related("edition__work")
            .order_by("created_at")
        )
        return Response(
            {
                "loans": [_digital_loan_dict(loan) for loan in loans],
                "holds": [_digital_hold_dict(hold) for hold in holds],
            }
        )


def _po_dict(po):
    return {
        "id": po.pk,
        "vendor": po.vendor.code,
        "fund": po.fund.code,
        "status": po.status,
        "ordered_at": po.ordered_at,
        "received_at": po.received_at,
        "lines": [
            {
                "id": ln.pk,
                "edition_id": ln.edition_id,
                "title": ln.title_text,
                "branch": ln.branch.slug,
                "quantity": ln.quantity,
                "received": ln.received_quantity,
                "unit_cost_cents": ln.unit_cost_cents,
            }
            for ln in po.lines.select_related("branch").all()
        ],
    }


class AcquisitionOrdersAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "acquisitions"

    def get(self, request):
        from .models import PurchaseOrder

        organization = api_organization(request)
        if organization is None:
            return Response({"error": {"code": "no_organization", "message": "None."}}, status=404)
        pos = PurchaseOrder.objects.filter(organization=organization).select_related(
            "vendor", "fund"
        )[:50]
        return Response({"data": [_po_dict(po) for po in pos]})

    def post(self, request):
        from . import acquisitions
        from .models import Fund, Vendor

        organization = api_organization(request)
        data = request.data if isinstance(request.data, dict) else {}
        vendor = get_object_or_404(Vendor, organization=organization, code=data.get("vendor"))
        fund = get_object_or_404(Fund, organization=organization, code=data.get("fund"))
        po = acquisitions.create_purchase_order(
            organization=organization, vendor=vendor, fund=fund, created_by=request.user
        )
        return Response({"data": _po_dict(po)}, status=status.HTTP_201_CREATED)


class AcquisitionLinesAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "acquisitions"

    def post(self, request, pk):
        from . import acquisitions
        from .models import Branch, Edition, PurchaseOrder

        organization = api_organization(request)
        po = get_object_or_404(PurchaseOrder, pk=pk, organization=organization)
        data = request.data if isinstance(request.data, dict) else {}
        branch = get_object_or_404(Branch, organization=organization, slug=data.get("branch"))
        edition = None
        if data.get("edition_id"):
            edition = get_object_or_404(Edition, pk=data["edition_id"])
        try:
            acquisitions.add_line(
                purchase_order=po,
                edition=edition,
                title_text=data.get("title", ""),
                branch=branch,
                quantity=int(data.get("quantity", 1)),
                unit_cost_cents=int(data.get("unit_cost_cents", 0)),
            )
        except DomainError as exc:
            return Response({"error": {"code": "line_blocked", "message": str(exc)}}, status=409)
        po.refresh_from_db()
        return Response({"data": _po_dict(po)}, status=status.HTTP_201_CREATED)


class AcquisitionPlaceAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "acquisitions"

    def post(self, request, pk):
        from . import acquisitions
        from .models import PurchaseOrder

        organization = api_organization(request)
        po = get_object_or_404(PurchaseOrder, pk=pk, organization=organization)
        try:
            acquisitions.place_order(purchase_order=po, actor=request.user)
        except DomainError as exc:
            return Response({"error": {"code": "place_blocked", "message": str(exc)}}, status=409)
        po.refresh_from_db()
        return Response({"data": _po_dict(po)})


class AcquisitionReceiveAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "acquisitions"

    def post(self, request, pk):
        from . import acquisitions
        from .models import PurchaseOrderLine

        organization = api_organization(request)
        line = get_object_or_404(
            PurchaseOrderLine, pk=pk, purchase_order__organization=organization
        )
        data = request.data if isinstance(request.data, dict) else {}
        try:
            acquisitions.receive_line(
                line=line, quantity=int(data.get("quantity", line.outstanding)), actor=request.user
            )
        except DomainError as exc:
            return Response({"error": {"code": "receive_blocked", "message": str(exc)}}, status=409)
        return Response({"data": _po_dict(line.purchase_order)})


class WorkReviewsAPI(APIView):
    """Public list of reviews + rating for a work; patrons POST their review."""

    permission_classes = [permissions.AllowAny]

    def get(self, request, slug):
        from . import social
        from .models import Work

        work = get_object_or_404(Work, slug=slug, public_status=PublicStatus.PUBLISHED)
        return Response(
            {
                "rating": social.work_rating(work),
                "reviews": [
                    {
                        "by": r.patron.user.get_username(),
                        "rating": r.rating,
                        "body": r.body,
                        "created_at": r.created_at,
                    }
                    for r in social.work_reviews(work)
                ],
            }
        )

    def post(self, request, slug):
        from . import social
        from .models import Work

        if not (request.user.is_authenticated and hasattr(request.user, "patron_profile")):
            return Response({"error": {"code": "forbidden"}}, status=403)
        work = get_object_or_404(Work, slug=slug, public_status=PublicStatus.PUBLISHED)
        data = request.data if isinstance(request.data, dict) else {}
        try:
            review = social.submit_review(
                patron=request.user.patron_profile,
                work=work,
                rating=int(data.get("rating", 0)),
                body=data.get("body", ""),
            )
        except (DomainError, ValueError, TypeError) as exc:
            return Response({"error": {"code": "invalid_review", "message": str(exc)}}, status=400)
        return Response({"data": {"id": review.pk, "rating": review.rating}}, status=201)


class WorkRecommendationsAPI(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, slug):
        from . import social
        from .models import Work

        organization = api_organization(request)
        if organization is None:
            return Response({"error": {"code": "no_organization"}}, status=404)
        work = get_object_or_404(Work, slug=slug, public_status=PublicStatus.PUBLISHED)
        return Response({"data": social.recommendations_for_work(organization, work)})


class ReadingListsAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "patron:read"

    def get(self, request):
        patron = request.user.patron_profile
        lists = patron.reading_lists.prefetch_related("works")
        return Response(
            {
                "data": [
                    {
                        "id": rl.pk,
                        "name": rl.name,
                        "public": rl.public,
                        "works": [{"slug": w.slug, "title": w.canonical_title} for w in rl.works.all()],
                    }
                    for rl in lists
                ]
            }
        )

    def post(self, request):
        from . import social

        patron = request.user.patron_profile
        data = request.data if isinstance(request.data, dict) else {}
        try:
            rl = social.create_reading_list(
                patron=patron, name=data.get("name", ""), public=bool(data.get("public"))
            )
        except DomainError as exc:
            return Response({"error": {"code": "invalid_list", "message": str(exc)}}, status=400)
        return Response({"data": {"id": rl.pk, "name": rl.name}}, status=201)


class ReadingListItemsAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    def post(self, request, pk):
        from . import social
        from .models import ReadingList, Work

        patron = request.user.patron_profile
        rl = get_object_or_404(ReadingList, pk=pk, patron=patron)
        work = get_object_or_404(Work, slug=(request.data or {}).get("work_slug"))
        social.add_to_list(reading_list=rl, work=work)
        return Response({"data": {"id": rl.pk, "work_count": rl.works.count()}})


class AccountExportAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "patron:read"

    def get(self, request):
        from . import privacy

        data = privacy.export_patron_data(request.user.patron_profile)
        response = Response(data)
        response["Content-Disposition"] = 'attachment; filename="my-library-data.json"'
        return response


class AccountEraseAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    def post(self, request):
        from . import privacy

        data = request.data if isinstance(request.data, dict) else {}
        if not data.get("confirm"):
            return Response(
                {"error": {"code": "confirm_required", "message": "Set confirm=true to erase."}},
                status=400,
            )
        privacy.erase_patron(patron=request.user.patron_profile, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class WebhookEndpointsAPI(APIView):
    """Manage tenant webhook endpoints (admin only)."""

    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "webhooks"

    def get(self, request):
        from .models import WebhookEndpoint

        organization = api_organization(request)
        if organization is None:
            return Response({"error": {"code": "no_organization"}}, status=404)
        eps = WebhookEndpoint.objects.filter(organization=organization)
        return Response(
            {
                "data": [
                    {
                        "id": e.pk,
                        "url": e.url,
                        "event_types": e.event_types or ["*"],
                        "active": e.active,
                    }
                    for e in eps
                ]
            }
        )

    def post(self, request):
        import secrets as _secrets

        from .models import WebhookEndpoint

        organization = api_organization(request)
        data = request.data if isinstance(request.data, dict) else {}
        url = data.get("url")
        if not url:
            return Response({"error": {"code": "url_required"}}, status=400)
        endpoint = WebhookEndpoint.objects.create(
            organization=organization,
            url=url,
            secret=data.get("secret") or _secrets.token_urlsafe(24),
            event_types=data.get("event_types") or ["*"],
        )
        return Response(
            {"data": {"id": endpoint.pk, "url": endpoint.url, "secret": endpoint.secret}},
            status=status.HTTP_201_CREATED,
        )


class EventsAPI(APIView):
    """Public upcoming events; patrons register via POST."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        from . import events

        organization = api_organization(request)
        if organization is None:
            return Response({"error": {"code": "no_organization"}}, status=404)
        return Response(
            {
                "data": [
                    {
                        "id": e.pk,
                        "title": e.title,
                        "starts_at": e.starts_at,
                        "ends_at": e.ends_at,
                        "capacity": e.capacity,
                        "branch": e.branch.slug if e.branch else None,
                    }
                    for e in events.upcoming_events(organization)
                ]
            }
        )


class EventRegisterAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    def post(self, request, pk):
        from . import events
        from .models import Event

        patron = request.user.patron_profile
        event = get_object_or_404(Event, pk=pk, organization=patron.organization)
        try:
            reg = events.register_for_event(patron=patron, event=event)
        except DomainError as exc:
            return Response({"error": {"code": "register_blocked", "message": str(exc)}}, status=409)
        return Response({"data": {"id": reg.pk, "status": reg.status}}, status=201)


class RoomReserveAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    def post(self, request, pk):
        from datetime import datetime

        from django.utils.dateparse import parse_datetime

        from . import events
        from .models import Room

        patron = request.user.patron_profile
        room = get_object_or_404(Room, pk=pk, organization=patron.organization, active=True)
        data = request.data if isinstance(request.data, dict) else {}
        starts_at = parse_datetime(data.get("starts_at", "") or "")
        ends_at = parse_datetime(data.get("ends_at", "") or "")
        if not isinstance(starts_at, datetime) or not isinstance(ends_at, datetime):
            return Response({"error": {"code": "bad_time"}}, status=400)
        try:
            resv = events.reserve_room(
                patron=patron, room=room, starts_at=starts_at, ends_at=ends_at,
                purpose=data.get("purpose", ""),
            )
        except DomainError as exc:
            return Response({"error": {"code": "reserve_blocked", "message": str(exc)}}, status=409)
        return Response({"data": {"id": resv.pk, "status": resv.status}}, status=201)


def _ill_dict(ill):
    return {
        "id": ill.pk,
        "work": ill.work.canonical_title,
        "status": ill.status,
        "requesting_org": ill.requesting_org_id,
        "lending_org": ill.lending_org_id,
        "due_at": ill.due_at,
    }


class ConsortiumAvailabilityAPI(APIView):
    """Union-catalog availability for a work across a consortium."""

    permission_classes = [permissions.AllowAny]

    def get(self, request, slug):
        from . import consortia
        from .models import Consortium, Work

        consortium = get_object_or_404(Consortium, slug=slug)
        work = get_object_or_404(Work, slug=request.GET.get("work"))
        return Response({"data": consortia.union_availability(consortium, work)})


class ConsortiumSearchAPI(APIView):
    """Union-catalog search across all consortium members."""

    permission_classes = [permissions.AllowAny]

    def get(self, request, slug):
        from . import consortia
        from .models import Consortium

        consortium = get_object_or_404(Consortium, slug=slug)
        works = consortia.union_search(consortium, request.GET.get("q", ""))
        return Response(
            {"data": [{"title": w.canonical_title, "slug": w.slug} for w in works]}
        )


class IllRequestAPI(APIView):
    """A patron requests an inter-library loan of a work."""

    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "circulation:write"

    def post(self, request, slug):
        from . import consortia
        from .models import Consortium, Work

        patron = request.user.patron_profile
        consortium = get_object_or_404(Consortium, slug=slug)
        data = request.data if isinstance(request.data, dict) else {}
        work = get_object_or_404(Work, slug=data.get("work"))
        try:
            ill = consortia.request_ill(
                patron=patron, work=work, consortium=consortium, actor=request.user, source="api"
            )
        except DomainError as exc:
            return Response({"error": {"code": "ill_blocked", "message": str(exc)}}, status=409)
        return Response({"data": _ill_dict(ill)}, status=201)


class IllListAPI(APIView):
    """List ILL requests where the staff's org is borrower or lender."""

    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:read"
    required_staff_permission = "circulation"

    def get(self, request):
        from django.db.models import Q

        from .models import IllRequest

        organization = api_organization(request)
        rows = (
            IllRequest.objects.filter(
                Q(requesting_org=organization) | Q(lending_org=organization)
            )
            .select_related("work")
            .order_by("-created_at")[:200]
        )
        return Response({"data": [_ill_dict(r) for r in rows]})


class IllActionAPI(APIView):
    """Advance an ILL through its lifecycle (ship/receive/return/checkin/cancel)."""

    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "circulation"

    # action -> (service fn name, which org side may perform it)
    ACTIONS = {
        "ship": ("ship_ill", "lending"),
        "receive": ("receive_ill", "requesting"),
        "return": ("return_ill", "requesting"),
        "checkin": ("checkin_ill", "lending"),
        "cancel": ("cancel_ill", "either"),
    }

    def post(self, request, pk, action):
        from . import consortia
        from .models import IllRequest

        if action not in self.ACTIONS:
            return Response({"error": {"code": "bad_action"}}, status=400)
        organization = api_organization(request)
        ill = get_object_or_404(IllRequest, pk=pk)
        fn_name, side = self.ACTIONS[action]
        allowed = (
            (side == "lending" and ill.lending_org_id == getattr(organization, "pk", None))
            or (side == "requesting" and ill.requesting_org_id == getattr(organization, "pk", None))
            or (
                side == "either"
                and organization is not None
                and organization.pk in (ill.lending_org_id, ill.requesting_org_id)
            )
        )
        if not allowed:
            return Response(
                {"error": {"code": "forbidden", "message": "Your library cannot perform this action."}},
                status=403,
            )
        try:
            ill = getattr(consortia, fn_name)(ill=ill, actor=request.user, source="api")
        except DomainError as exc:
            return Response({"error": {"code": "ill_blocked", "message": str(exc)}}, status=409)
        return Response({"data": _ill_dict(ill)})


class NotificationPrefsAPI(APIView):
    """Read/update a patron's notification channels and per-category preferences."""

    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "patron:read"

    def get(self, request):
        patron = request.user.patron_profile
        return Response(
            {
                "data": {
                    "channels": patron.notification_channels or ["email"],
                    "preferences": patron.notification_prefs or {},
                    "unsubscribed": patron.unsubscribed_at is not None,
                }
            }
        )

    def post(self, request):
        from . import channels as channel_mod

        patron = request.user.patron_profile
        data = request.data if isinstance(request.data, dict) else {}
        fields = []
        if isinstance(data.get("preferences"), dict):
            prefs = {
                str(k): bool(v) for k, v in data["preferences"].items()
            }
            patron.notification_prefs = prefs
            fields.append("notification_prefs")
        if isinstance(data.get("channels"), list):
            valid = [c for c in data["channels"] if channel_mod.get_channel(c)]
            patron.notification_channels = valid
            fields.append("notification_channels")
        if "unsubscribed" in data:
            from django.utils import timezone as _tz

            patron.unsubscribed_at = _tz.now() if data.get("unsubscribed") else None
            fields.append("unsubscribed_at")
        if fields:
            fields.append("updated_at")
            patron.save(update_fields=fields)
        return Response(
            {
                "data": {
                    "channels": patron.notification_channels or ["email"],
                    "preferences": patron.notification_prefs or {},
                    "unsubscribed": patron.unsubscribed_at is not None,
                }
            }
        )


# =========================================================================== #
# Staff workflows (Increment 14)
# =========================================================================== #
class BulkCopyAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "copies"

    def post(self, request):
        from . import workflows

        organization = api_organization(request)
        data = request.data if isinstance(request.data, dict) else {}
        barcodes = data.get("barcodes") or []
        if not isinstance(barcodes, list) or not barcodes:
            return Response({"error": {"code": "no_barcodes"}}, status=400)
        try:
            result = workflows.bulk_update_copies(
                organization=organization,
                barcodes=[str(b) for b in barcodes],
                status=data.get("status"),
                public_visible=data.get("public_visible"),
                actor=request.user,
            )
        except DomainError as exc:
            return Response({"error": {"code": "bulk_blocked", "message": str(exc)}}, status=409)
        return Response({"data": result})


class WeedCopiesAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "copies"

    def post(self, request):
        from . import workflows

        organization = api_organization(request)
        data = request.data if isinstance(request.data, dict) else {}
        barcodes = [str(b) for b in (data.get("barcodes") or [])]
        result = workflows.weed_copies(
            organization=organization, barcodes=barcodes,
            reason=data.get("reason", "weeded"), actor=request.user,
        )
        return Response({"data": result})


class InventoryStartAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "copies"

    def post(self, request):
        from . import workflows
        from .models import Branch

        organization = api_organization(request)
        data = request.data if isinstance(request.data, dict) else {}
        branch = get_object_or_404(Branch, organization=organization, slug=data.get("branch"))
        session = workflows.start_inventory(
            organization=organization, branch=branch, actor=request.user
        )
        return Response({"data": {"id": session.pk, "branch": branch.slug}}, status=201)


class InventoryScanAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "copies"

    def post(self, request, pk):
        from . import workflows
        from .models import InventorySession

        organization = api_organization(request)
        session = get_object_or_404(InventorySession, pk=pk, organization=organization)
        data = request.data if isinstance(request.data, dict) else {}
        try:
            result = workflows.scan_inventory(session=session, barcode=data.get("barcode", ""))
        except DomainError as exc:
            return Response({"error": {"code": "scan_blocked", "message": str(exc)}}, status=409)
        return Response({"data": {"result": result}})


class InventoryCloseAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "copies"

    def post(self, request, pk):
        from . import workflows
        from .models import InventorySession

        organization = api_organization(request)
        session = get_object_or_404(InventorySession, pk=pk, organization=organization)
        try:
            session = workflows.close_inventory(session=session, actor=request.user)
        except DomainError as exc:
            return Response({"error": {"code": "close_blocked", "message": str(exc)}}, status=409)
        return Response(
            {"data": {
                "missing": session.missing_barcodes,
                "unexpected": session.unexpected_barcodes,
                "found_checked_out": session.found_checked_out_barcodes,
            }}
        )


class LoanExceptionAPI(APIView):
    """Handle a loan exception: lost / damaged / claims-returned."""

    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "circulation"

    def post(self, request, pk, action):
        from . import workflows
        from .models import Loan

        organization = api_organization(request)
        loan = get_object_or_404(Loan, pk=pk, organization=organization)
        try:
            if action == "lost":
                fee = workflows.mark_loan_lost(loan=loan, actor=request.user)
                return Response({"data": {"status": "lost", "fee_cents": fee.amount_cents}})
            if action == "damaged":
                fee = workflows.return_damaged(
                    loan=loan, actor=request.user, fee_cents=request.data.get("fee_cents"),
                )
                return Response({"data": {"status": "returned", "fee_cents": fee.amount_cents}})
            if action == "claims-returned":
                workflows.mark_claims_returned(loan=loan, actor=request.user)
                return Response({"data": {"status": "claims_returned"}})
        except DomainError as exc:
            return Response({"error": {"code": "loan_blocked", "message": str(exc)}}, status=409)
        return Response({"error": {"code": "bad_action"}}, status=400)


# =========================================================================== #
# Financial depth (Increment 15)
# =========================================================================== #
class RefundAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "billing"

    def post(self, request):
        from . import finance
        from .models import Payment

        organization = api_organization(request)
        data = request.data if isinstance(request.data, dict) else {}
        payment = get_object_or_404(Payment, pk=data.get("payment_id"), organization=organization)
        try:
            refund = finance.refund_payment(
                payment=payment, amount_cents=data.get("amount_cents"), actor=request.user,
                reason=data.get("reason", ""),
            )
        except DomainError as exc:
            return Response({"error": {"code": "refund_blocked", "message": str(exc)}}, status=409)
        return Response({"data": {"id": refund.pk, "amount_cents": refund.amount_cents}}, status=201)


class PaymentPlanAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "billing"

    def post(self, request):
        from . import finance
        from .models import PatronProfile

        organization = api_organization(request)
        data = request.data if isinstance(request.data, dict) else {}
        patron = get_object_or_404(PatronProfile, pk=data.get("patron_id"), organization=organization)
        try:
            plan = finance.create_payment_plan(
                patron=patron, total_cents=int(data.get("total_cents", 0)),
                installments=int(data.get("installments", 1)), actor=request.user,
            )
        except (DomainError, TypeError, ValueError) as exc:
            return Response({"error": {"code": "plan_blocked", "message": str(exc)}}, status=409)
        return Response(
            {"data": {"id": plan.pk, "installment_cents": plan.installment_cents}}, status=201
        )


class PaymentPlanPayAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "billing"

    def post(self, request, pk):
        from . import finance
        from .models import PaymentPlan

        organization = api_organization(request)
        plan = get_object_or_404(PaymentPlan, pk=pk, organization=organization)
        try:
            payment = finance.pay_installment(
                plan=plan, amount_cents=request.data.get("amount_cents"), actor=request.user
            )
        except DomainError as exc:
            return Response({"error": {"code": "pay_blocked", "message": str(exc)}}, status=409)
        plan.refresh_from_db()
        return Response(
            {"data": {"payment_id": payment.pk, "paid_cents": plan.paid_cents, "status": plan.status}}
        )


class AmnestyAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:write"
    required_staff_permission = "billing"

    def post(self, request):
        from . import finance

        organization = api_organization(request)
        waived = finance.run_amnesty(organization=organization, actor=request.user)
        return Response({"data": {"waived": waived}})


class GlExportAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:read"
    required_staff_permission = "billing"

    def get(self, request):
        from django.utils.dateparse import parse_date

        from . import finance

        organization = api_organization(request)
        start = parse_date(request.GET.get("start", "") or "")
        end = parse_date(request.GET.get("end", "") or "")
        rows = finance.gl_export(organization=organization, start=start, end=end)
        return Response({"data": rows, "count": len(rows)})


# =========================================================================== #
# Analytics (Increment 16)
# =========================================================================== #
class AnalyticsAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:read"
    required_staff_permission = "reports"

    def get(self, request, report):
        from . import analytics

        organization = api_organization(request)
        if report == "turnover":
            return Response({"data": analytics.collection_turnover(organization)})
        if report == "purchase-suggestions":
            return Response({"data": analytics.purchase_suggestions(organization)})
        if report == "circulation":
            return Response({"data": analytics.circulation_timeseries(organization)})
        if report == "bi":
            return Response({"data": analytics.bi_export(organization)})
        return Response({"error": {"code": "unknown_report"}}, status=404)


class AuditLogAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:read"
    required_staff_permission = "reports"

    def get(self, request):
        from .models import AuditLog

        organization = api_organization(request)
        qs = AuditLog.objects.filter(organization=organization)
        action = request.GET.get("action")
        if action:
            qs = qs.filter(action=action)
        rows = qs.order_by("-created_at")[:200]
        return Response(
            {"data": [
                {
                    "id": a.pk,
                    "action": a.action,
                    "entity": f"{a.entity_type}:{a.entity_id}",
                    "actor_id": a.actor_id,
                    "source": a.source,
                    "at": a.created_at,
                    "after": a.after,
                }
                for a in rows
            ]}
        )


# =========================================================================== #
# Staff MFA (Increment 17)
# =========================================================================== #
class MfaEnrollAPI(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from . import mfa

        return Response({"data": mfa.begin_enrollment(user=request.user)}, status=201)


class MfaConfirmAPI(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from . import mfa

        try:
            mfa.confirm_enrollment(user=request.user, code=str(request.data.get("code", "")))
        except DomainError as exc:
            return Response({"error": {"code": "mfa_invalid", "message": str(exc)}}, status=400)
        return Response({"data": {"confirmed": True}})


class MfaVerifyAPI(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from . import mfa

        ok = mfa.verify_login(user=request.user, code=str(request.data.get("code", "")))
        if ok and hasattr(request, "session"):
            request.session["mfa_verified"] = True
        return Response({"data": {"verified": ok}}, status=200 if ok else 401)


class MfaDisableAPI(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from . import mfa

        mfa.disable_mfa(user=request.user, actor=request.user)
        return Response({"data": {"enabled": False}})


# =========================================================================== #
# AI assistant (Increment 18)
# =========================================================================== #
class RecommendationsAPI(APIView):
    permission_classes = [IsAuthenticatedPatron, TokenHasScope]
    required_scope = "patron:read"

    def get(self, request):
        from . import assistant

        patron = request.user.patron_profile
        works = assistant.recommend_for_patron(patron, limit=int(request.GET.get("limit", 6)))
        return Response(
            {"data": [{"title": w.canonical_title, "slug": w.slug} for w in works]}
        )


class CatalogAssistAPI(APIView):
    permission_classes = [HasStaffPermission, TokenHasScope]
    required_scope = "staff:read"
    required_staff_permission = "catalog"

    def post(self, request):
        from . import assistant

        text = (request.data or {}).get("text", "") if isinstance(request.data, dict) else ""
        return Response({"data": assistant.catalog_assist(text=str(text))})


class NlSearchAPI(APIView):
    """Parse a natural-language query and run it against the catalog."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        from . import assistant

        if _search_rate_limited(request):
            return Response({"error": {"code": "rate_limited"}}, status=429)
        organization = api_organization(request)
        if organization is None:
            return Response({"data": [], "parsed": {}})
        parsed = assistant.parse_query(request.GET.get("q", ""))
        page = search_catalog(
            organization=organization, query=parsed["q"], filters=parsed["filters"],
            per_page=20, log=False,
        )
        availability_map = availability_map_for_works(organization, [w.id for w in page.results])
        serializer = WorkListSerializer(
            page.results, many=True,
            context={"organization": organization, "availability_map": availability_map},
        )
        return Response({"data": serializer.data, "parsed": parsed})

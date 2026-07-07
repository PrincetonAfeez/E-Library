import hashlib
import logging
import secrets
from functools import wraps

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import billing, delivery, sso
from .forms import (
    CatalogImportUploadForm,
    OrganizationSignupForm,
    PatronRegistrationForm,
    PatronSettingsForm,
)
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
    Fee,
    FeeStatus,
    Hold,
    Loan,
    Organization,
    Plan,
    PublicStatus,
    Work,
)
from .pagination import CursorError
from .permissions import user_has_staff_permission
from .ratelimit import is_rate_limited, rate_limit
from .selectors import (
    get_librarian_dashboard,
    get_patron_holds,
    get_patron_loans,
    get_work_detail,
    search_catalog,
)
from .services import (
    DomainError,
    borrow_work,
    cancel_hold,
    patron_balance_cents,
    place_hold,
    register_patron,
    renew_loan,
    return_loan,
)
from .tenancy import get_current_organization, staff_organization_for_user

logger = logging.getLogger("library")


def patron_required(view):
    """Require an authenticated user that has a patron profile.

    Circulation views read ``request.user.patron_profile``; a logged-in staff or
    admin user without one would otherwise raise RelatedObjectDoesNotExist (500).
    """

    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not hasattr(request.user, "patron_profile"):
            messages.error(request, "You need a patron account to do that.")
            return redirect("catalog_search")
        return view(request, *args, **kwargs)

    return wrapper


def _requester_hash(request) -> str:
    """Privacy-preserving hash of the searcher, without forcing a session."""
    if request.user.is_authenticated:
        base = f"user:{request.user.pk}"
    elif request.session.session_key:
        base = f"session:{request.session.session_key}"
    else:
        return ""
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def catalog_search(request):
    organization = get_current_organization(request)
    if organization is None:
        return render(request, "catalog/empty.html")
    query = request.GET.get("q", "")
    # Rate-limit only actual (non-empty) searches from anonymous clients — never
    # plain browsing/pagination, and never signed-in patrons. This protects the
    # expensive FTS path from scraping without locking out shared-IP library
    # patrons who are merely browsing.
    if query.strip() and not request.user.is_authenticated:
        if is_rate_limited(request, scope="search", limit=120, window=60):
            return HttpResponse(
                "Too many searches. Please wait a moment and try again.", status=429
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
            query=query,
            filters=filters,
            page=int(request.GET.get("page", 1)),
            per_page=12,
            cursor=request.GET.get("cursor"),
            requester_hash=_requester_hash(request),
            # Don't log every debounced live-search keystroke (HTMX partials);
            # log only full-page (committed) searches.
            log=not request.headers.get("HX-Request"),
        )
    except CursorError as exc:
        return HttpResponseBadRequest(str(exc))
    context = {
        "organization": organization,
        "query": request.GET.get("q", ""),
        "filters": filters,
        "page": page,
    }
    if request.headers.get("HX-Request"):
        return render(request, "catalog/partials/_search_results.html", context)
    return render(request, "catalog/search.html", context)


def work_detail(request, slug):
    from . import social

    organization = get_current_organization(request)
    if organization is None:
        return render(request, "catalog/empty.html")
    try:
        work = get_work_detail(organization, slug)
    except Work.DoesNotExist as exc:
        raise Http404("Work not found.") from exc
    return render(
        request,
        "catalog/work_detail.html",
        {
            "organization": organization,
            "work": work,
            "rating": social.work_rating(work),
            "reviews": social.work_reviews(work),
            "recommendations": social.recommendations_for_work(organization, work),
        },
    )


@patron_required
@require_POST
def submit_review_view(request, slug):
    from . import social

    work = get_object_or_404(Work, slug=slug, public_status=PublicStatus.PUBLISHED)
    try:
        social.submit_review(
            patron=request.user.patron_profile,
            work=work,
            rating=int(request.POST.get("rating", 0)),
            body=request.POST.get("body", ""),
        )
        messages.success(request, "Thanks for your review!")
    except (DomainError, ValueError, TypeError):
        messages.error(request, "Please choose a rating from 1 to 5.")
    return redirect(work.get_absolute_url())


@patron_required
def export_my_data(request):
    from django.http import JsonResponse

    from . import privacy

    data = privacy.export_patron_data(request.user.patron_profile)
    response = JsonResponse(data, json_dumps_params={"indent": 2})
    response["Content-Disposition"] = 'attachment; filename="my-library-data.json"'
    return response


@patron_required
@require_POST
def erase_my_account(request):
    from django.contrib.auth import logout

    from . import privacy

    if request.POST.get("confirm") != "yes":
        messages.error(request, "Type the confirmation to erase your account.")
        return redirect("patron_settings")
    privacy.erase_patron(patron=request.user.patron_profile, actor=request.user)
    logout(request)
    messages.success(request, "Your account and personal data have been erased.")
    return redirect("catalog_search")


@patron_required
@require_POST
def borrow_work_view(request, slug):
    work = get_object_or_404(Work, slug=slug, public_status=PublicStatus.PUBLISHED)
    patron = request.user.patron_profile
    branch = None
    if request.POST.get("branch"):
        branch = get_object_or_404(
            Branch, organization=patron.organization, slug=request.POST["branch"]
        )
    try:
        loan = borrow_work(patron=patron, work=work, branch=branch, actor=request.user)
        messages.success(request, f"Checked out until {loan.due_at:%b %d, %Y}.")
    except DomainError as exc:
        messages.error(request, str(exc))
    return redirect(work.get_absolute_url())


@patron_required
@require_POST
def place_hold_view(request, slug):
    work = get_object_or_404(Work, slug=slug, public_status=PublicStatus.PUBLISHED)
    patron = request.user.patron_profile
    branch = None
    if request.POST.get("branch"):
        branch = get_object_or_404(
            Branch, organization=patron.organization, slug=request.POST["branch"]
        )
    try:
        hold = place_hold(patron=patron, work=work, preferred_branch=branch, actor=request.user)
        status = "ready for pickup" if hold.status == "ready" else "queued"
        messages.success(request, f"Hold {status}.")
    except DomainError as exc:
        messages.error(request, str(exc))
    return redirect(work.get_absolute_url())


@rate_limit(scope="register", limit=10, window=3600)
def register(request):
    if request.user.is_authenticated:
        return redirect("account")

    active_orgs = Organization.objects.filter(active=True)
    if not active_orgs.exists():
        return render(request, "catalog/empty.html")

    # Only treat the organization as fixed when it was chosen explicitly (via
    # ?org= / session) or when there is exactly one library. Otherwise the patron
    # must pick their library, rather than being bound to an arbitrary default.
    explicit_slug = request.GET.get("org") or request.session.get("organization_slug")
    if explicit_slug:
        organization = get_object_or_404(Organization, slug=explicit_slug, active=True)
    elif active_orgs.count() == 1:
        organization = active_orgs.first()
    else:
        organization = None
    require_org = organization is None

    if request.method == "POST":
        form = PatronRegistrationForm(
            request.POST, organization=organization, require_org=require_org
        )
        if form.is_valid():
            chosen_org = organization or form.cleaned_data["organization"]
            try:
                with transaction.atomic():
                    user = form.save()
                    register_patron(
                        user=user,
                        organization=chosen_org,
                        home_branch=form.cleaned_data.get("home_branch"),
                        notification_email=form.cleaned_data.get("email", ""),
                    )
            except DomainError as exc:
                messages.error(request, str(exc))
            else:
                login(request, user)
                messages.success(request, "Welcome! Your library account is ready.")
                return redirect("account")
    else:
        form = PatronRegistrationForm(organization=organization, require_org=require_org)
    return render(
        request, "registration/register.html", {"form": form, "organization": organization}
    )


@patron_required
def account(request):
    from .models import DigitalHoldStatus, DigitalLoanStatus

    patron = request.user.patron_profile
    fees = (
        Fee.objects.filter(patron=patron).exclude(status=FeeStatus.WAIVED).order_by("-created_at")
    )
    digital_loans = (
        patron.digital_loans.filter(status=DigitalLoanStatus.ACTIVE)
        .select_related("license__edition__work")
        .order_by("expires_at")
    )
    digital_holds = (
        patron.digital_holds.filter(
            status__in=[DigitalHoldStatus.WAITING, DigitalHoldStatus.READY]
        )
        .select_related("edition__work")
        .order_by("created_at")
    )
    return render(
        request,
        "circulation/account.html",
        {
            "loans": get_patron_loans(patron),
            "holds": get_patron_holds(patron),
            "fees": fees,
            "balance_cents": patron_balance_cents(patron),
            "digital_loans": digital_loans,
            "digital_holds": digital_holds,
        },
    )


@patron_required
def patron_settings(request):
    patron = request.user.patron_profile
    if request.method == "POST":
        form = PatronSettingsForm(request.POST, instance=patron)
        if form.is_valid():
            form.save()
            messages.success(request, "Preferences updated.")
            return redirect("account")
    else:
        form = PatronSettingsForm(instance=patron)
    return render(request, "circulation/settings.html", {"form": form})


@patron_required
@require_POST
def renew_loan_view(request, pk):
    loan = get_object_or_404(Loan, pk=pk, patron=request.user.patron_profile)
    try:
        renew_loan(loan=loan, actor=request.user)
        messages.success(request, "Loan renewed.")
    except DomainError as exc:
        messages.error(request, str(exc))
    return redirect("account")


@patron_required
@require_POST
def return_loan_view(request, pk):
    loan = get_object_or_404(Loan, pk=pk, patron=request.user.patron_profile)
    try:
        return_loan(loan=loan, actor=request.user)
        messages.success(request, "Loan returned.")
    except DomainError as exc:
        messages.error(request, str(exc))
    return redirect("account")


@patron_required
@require_POST
def cancel_hold_view(request, pk):
    hold = get_object_or_404(Hold, pk=pk, patron=request.user.patron_profile)
    try:
        cancel_hold(hold=hold, actor=request.user)
        messages.success(request, "Hold cancelled.")
    except DomainError as exc:
        messages.error(request, str(exc))
    return redirect("account")


@rate_limit(scope="signup", limit=5, window=3600)
def organization_signup(request):
    """Self-serve library signup — provisions a new tenant on a trial."""
    if request.user.is_authenticated:
        return redirect("librarian_dashboard")
    if request.method == "POST":
        form = OrganizationSignupForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                owner = form.save()
                org = billing.provision_tenant(
                    name=form.cleaned_data["organization_name"],
                    slug=form.cleaned_data["organization_slug"],
                    owner_user=owner,
                    plan=form.cleaned_data.get("plan"),
                )
            login(request, owner)
            request.session["organization_slug"] = org.slug
            messages.success(request, f"Welcome to {org.name}! Your trial is active.")
            return redirect("librarian_dashboard")
    else:
        form = OrganizationSignupForm()
    return render(request, "registration/signup_org.html", {"form": form})


@login_required
def billing_dashboard(request):
    organization = _resolve_staff_org(request)
    if not user_has_staff_permission(request.user, organization, "billing"):
        raise PermissionDenied("Only organization admins can manage billing.")
    if request.method == "POST":
        action = request.POST.get("action")
        subscription = billing.get_subscription(organization)
        try:
            if action == "change_plan":
                new_plan = get_object_or_404(
                    Plan, slug=request.POST.get("plan"), active=True
                )
                if subscription is None:
                    billing.subscribe(organization=organization, plan=new_plan, actor=request.user)
                else:
                    billing.change_plan(
                        subscription=subscription, new_plan=new_plan, actor=request.user
                    )
                messages.success(request, "Plan updated.")
            elif action == "cancel" and subscription is not None:
                billing.cancel_subscription(subscription=subscription, actor=request.user)
                messages.success(request, "Subscription canceled.")
            elif action == "add_card":
                billing.add_payment_method(
                    organization=organization,
                    brand=request.POST.get("brand", "visa"),
                    last4=request.POST.get("last4", "4242"),
                    exp_month=int(request.POST.get("exp_month") or 12),
                    exp_year=int(request.POST.get("exp_year") or 2030),
                    actor=request.user,
                )
                messages.success(request, "Card saved.")
        except billing.BillingError as exc:
            messages.error(request, str(exc))
        except (TypeError, ValueError):
            messages.error(request, "Invalid card details.")
        return redirect("billing_dashboard")
    overview = billing.billing_overview(organization)
    plans = Plan.objects.filter(active=True, public=True)
    return render(
        request,
        "billing/dashboard.html",
        {"organization": organization, "overview": overview, "plans": plans},
    )


def _resolve_staff_org(request):
    # Prefer an explicit ?org, then an org the user is staff in (so a staff+patron
    # user isn't routed to their patron org), then the general current org.
    if request.GET.get("org"):
        return get_current_organization(request)
    return staff_organization_for_user(request.user) or get_current_organization(request)


@login_required
def librarian_dashboard(request):
    organization = _resolve_staff_org(request)
    # Staff authorization is scoped to the resolved organization so a librarian
    # of one tenant cannot view another tenant's dashboard via ?org=.
    if not user_has_staff_permission(request.user, organization, "reports"):
        raise PermissionDenied("You do not have staff access to this organization.")
    branch = None
    if request.GET.get("branch"):
        branch = get_object_or_404(Branch, organization=organization, slug=request.GET["branch"])
    dashboard = get_librarian_dashboard(organization, branch=branch)
    return render(
        request,
        "librarian/dashboard.html",
        {
            "organization": organization,
            "dashboard": dashboard,
            "branch": branch,
            "can_import": user_has_staff_permission(request.user, organization, "imports"),
            "can_bill": user_has_staff_permission(request.user, organization, "billing"),
        },
    )


@login_required
def librarian_reports(request):
    from . import reporting

    organization = _resolve_staff_org(request)
    if not user_has_staff_permission(request.user, organization, "reports"):
        raise PermissionDenied("You do not have access to reports.")
    try:
        days = max(1, min(730, int(request.GET.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    report = reporting.dashboard_report(organization, days=days)
    return render(
        request,
        "librarian/reports.html",
        {"organization": organization, "report": report, "days": days},
    )


def _require_staff_org(request, permission="imports"):
    """Resolve the org and enforce the required staff permission for import views."""
    organization = _resolve_staff_org(request)
    if not user_has_staff_permission(request.user, organization, permission):
        raise PermissionDenied("You do not have permission for this action.")
    return organization


@login_required
def librarian_imports(request):
    organization = _require_staff_org(request)
    form = CatalogImportUploadForm()
    if request.method == "POST":
        form = CatalogImportUploadForm(request.POST, request.FILES)
        if form.is_valid():
            rows = parse_rows_from_csv(form.cleaned_data["csv_file"].read())
            if not rows:
                messages.error(request, "No rows found in the uploaded file.")
            else:
                batch = stage_import(
                    organization=organization,
                    rows=rows,
                    uploaded_by=request.user,
                    source_file=form.cleaned_data["csv_file"].name,
                )
                validate_import(batch=batch)
                messages.success(request, f"Staged batch #{batch.pk} with {batch.row_count} rows.")
                return redirect("librarian_import_detail", pk=batch.pk)
    batches = CatalogImportBatch.objects.filter(organization=organization)[:50]
    return render(
        request,
        "librarian/imports.html",
        {"organization": organization, "form": form, "batches": batches},
    )


@login_required
def librarian_import_detail(request, pk):
    organization = _require_staff_org(request)
    batch = get_object_or_404(CatalogImportBatch, pk=pk, organization=organization)
    rows = batch.rows.order_by("row_number")
    return render(
        request,
        "librarian/import_detail.html",
        {"organization": organization, "batch": batch, "rows": rows},
    )


@login_required
@require_POST
def librarian_import_commit(request, pk):
    organization = _require_staff_org(request)
    batch = get_object_or_404(CatalogImportBatch, pk=pk, organization=organization)
    try:
        commit_import(batch=batch, actor=request.user)
        messages.success(request, f"Committed batch #{batch.pk}.")
    except DomainError as exc:
        messages.error(request, str(exc))
    return redirect("librarian_import_detail", pk=batch.pk)


@login_required
@require_POST
def librarian_import_rollback(request, pk):
    organization = _require_staff_org(request)
    batch = get_object_or_404(CatalogImportBatch, pk=pk, organization=organization)
    try:
        rollback_import(batch=batch, actor=request.user, reason=request.POST.get("reason", ""))
        messages.success(request, f"Rolled back batch #{batch.pk}.")
    except DomainError as exc:
        messages.error(request, str(exc))
    return redirect("librarian_import_detail", pk=batch.pk)


def sso_login(request, org_slug):
    from django.urls import reverse

    from .models import SsoConnection

    organization = get_object_or_404(Organization, slug=org_slug, active=True)
    connection = get_object_or_404(SsoConnection, organization=organization, enabled=True)
    state = secrets.token_urlsafe(24)
    request.session["sso_state"] = state
    request.session["sso_org"] = organization.slug
    redirect_uri = request.build_absolute_uri(reverse("sso_callback"))
    return redirect(sso.build_authorize_url(connection, redirect_uri=redirect_uri, state=state))


def sso_callback(request):
    from django.urls import reverse

    from .models import SsoConnection

    state = request.GET.get("state")
    if not state or state != request.session.get("sso_state"):
        return HttpResponseBadRequest("Invalid SSO state.")
    organization = get_object_or_404(
        Organization, slug=request.session.get("sso_org"), active=True
    )
    connection = get_object_or_404(SsoConnection, organization=organization, enabled=True)
    redirect_uri = request.build_absolute_uri(reverse("sso_callback"))
    try:
        user = sso.handle_callback(
            connection, code=request.GET.get("code", ""), redirect_uri=redirect_uri
        )
    except DomainError as exc:
        messages.error(request, str(exc))
        return redirect("login")
    login(request, user)
    request.session["organization_slug"] = organization.slug
    messages.success(request, "Signed in.")
    return redirect("catalog_search")


def healthz(request):
    # Liveness: always 200 if the process is up.
    return HttpResponse("ok", content_type="text/plain")


@login_required
@patron_required
def digital_reader(request, pk):
    """Render the in-browser reader/player for one of the patron's active loans."""
    from .models import DigitalLoan, DigitalLoanStatus

    patron = request.user.patron_profile
    loan = get_object_or_404(
        DigitalLoan, pk=pk, organization=patron.organization, patron=patron,
        status=DigitalLoanStatus.ACTIVE,
    )
    try:
        manifest = delivery.access_manifest(access_token=loan.access_token)
    except DomainError as exc:
        messages.error(request, str(exc))
        return redirect("account")
    return render(request, "digital/reader.html", {"loan": loan, "manifest": manifest})


def digital_content(request, token):
    """Stream watermarked content for a signed, short-lived content token.

    The token itself is the capability, so this endpoint needs no session — it is
    safe to use directly as an ``<audio>``/``<img>`` source or a text fetch.
    """
    try:
        loan, locator = delivery.resolve_content_token(token)
    except DomainError as exc:
        return HttpResponse(str(exc), status=403, content_type="text/plain")

    if locator.startswith("chapter:"):
        try:
            index = int(locator.split(":", 1)[1])
            body, _title = delivery.read_text_chapter(loan, index)
        except (ValueError, DomainError):
            return HttpResponse("Chapter not found.", status=404, content_type="text/plain")
        resp = HttpResponse(body, content_type="text/plain; charset=utf-8")
        resp["Cache-Control"] = "private, no-store"
        return resp

    try:
        data, content_type, headers = delivery.fetch_binary(loan, locator)
    except DomainError as exc:
        return HttpResponse(str(exc), status=404, content_type="text/plain")

    # Honour a single Range request so audio can seek without a full download.
    total = len(data)
    range_header = request.META.get("HTTP_RANGE", "")
    if range_header.startswith("bytes="):
        try:
            start_s, end_s = range_header.split("=", 1)[1].split("-", 1)
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else total - 1
        except ValueError:
            start, end = 0, total - 1
        start = max(0, start)
        end = min(end, total - 1)
        if start > end:
            resp = HttpResponse(status=416)
            resp["Content-Range"] = f"bytes */{total}"
            return resp
        chunk = data[start : end + 1]
        resp = HttpResponse(chunk, status=206, content_type=content_type)
        resp["Content-Range"] = f"bytes {start}-{end}/{total}"
        resp["Content-Length"] = str(len(chunk))
    else:
        resp = HttpResponse(data, content_type=content_type)
        resp["Content-Length"] = str(total)
    resp["Accept-Ranges"] = "bytes"
    for key, value in headers.items():
        resp[key] = value
    return resp


@login_required
def mfa_challenge(request):
    """Second-factor prompt for staff whose org requires MFA."""
    from . import mfa

    next_url = request.GET.get("next") or "/librarian/"
    if request.method == "POST":
        if mfa.verify_login(user=request.user, code=request.POST.get("code", "")):
            request.session["mfa_verified"] = True
            return redirect(request.POST.get("next") or next_url)
        messages.error(request, "That code is incorrect. Try again.")
    return render(request, "mfa/challenge.html", {"next": next_url})


def terms(request):
    return render(request, "legal/terms.html")


def privacy(request):
    return render(request, "legal/privacy.html")


def status_page(request):
    """Public service status page: component health at a glance."""
    components = []
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # pragma: no cover - depends on infra state
        db_ok = False
    components.append({"name": "Database", "ok": db_ok})
    cache_ok = True
    try:
        cache.set("status_probe", "1", 5)
        cache_ok = cache.get("status_probe") == "1"
    except Exception:  # pragma: no cover - depends on infra state
        cache_ok = False
    components.append({"name": "Cache", "ok": cache_ok})
    all_ok = all(c["ok"] for c in components)
    return render(
        request,
        "status.html",
        {"components": components, "all_ok": all_ok},
        status=200 if all_ok else 503,
    )


def unsubscribe(request, token):
    """One-click unsubscribe from non-essential notices (CAN-SPAM compliant).

    A GET immediately unsubscribes (email one-click links are GETs) and shows a
    confirmation with a re-subscribe option; POST toggles back on.
    """
    from .models import PatronProfile

    patron = get_object_or_404(PatronProfile, unsubscribe_token=token)
    # State changes only on POST: a GET (link scanners, client prefetch) must
    # never silently unsubscribe someone (RFC 8058).
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "resubscribe":
            patron.unsubscribed_at = None
            patron.save(update_fields=["unsubscribed_at", "updated_at"])
        elif action == "unsubscribe" and patron.unsubscribed_at is None:
            patron.unsubscribed_at = timezone.now()
            patron.save(update_fields=["unsubscribed_at", "updated_at"])
    return render(
        request,
        "notifications/unsubscribe.html",
        {"patron": patron, "unsubscribed": patron.unsubscribed_at is not None, "token": token},
    )


def readyz(request):
    # Readiness: verify the critical backing services are reachable. Component
    # names only — never raw exception text — are returned to unauthenticated
    # callers; details are logged server-side.
    problems = []
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # pragma: no cover - depends on infra state
        logger.exception("readyz database check failed")
        problems.append("database")
    try:
        cache.set("readyz", "1", 5)
        if cache.get("readyz") != "1":
            problems.append("cache")
    except Exception:  # pragma: no cover - depends on infra state
        logger.exception("readyz cache check failed")
        problems.append("cache")
    if problems:
        return HttpResponse(
            "not ready: " + ",".join(problems), status=503, content_type="text/plain"
        )
    return HttpResponse("ready", content_type="text/plain")

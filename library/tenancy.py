"""Multi-tenant organization resolution from requests and users."""
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import get_object_or_404

from .models import Organization, PatronProfile


def organization_for_user(user):
    """The tenant an authenticated user belongs to (patron first, then staff)."""
    if user is None or not user.is_authenticated:
        return None
    try:
        profile = user.patron_profile
    except ObjectDoesNotExist:
        profile = None
    if profile is not None and profile.organization.active:
        return profile.organization
    membership = (
        user.staff_memberships.filter(active=True, organization__active=True)
        .select_related("organization")
        .first()
    )
    return membership.organization if membership else None


def staff_organization_for_user(user, session=None):
    """Resolve a staff tenant deterministically.

    Callers handling a request should pass its session so an explicitly selected
    staff organization takes precedence. Otherwise the first active membership
    ordered by organization name and primary key is used.
    """
    if user is None or not user.is_authenticated:
        return None
    session_slug = (session or {}).get("organization_slug")
    if session_slug:
        membership = (
            user.staff_memberships.filter(
                active=True, organization__active=True, organization__slug=session_slug
            )
            .select_related("organization")
            .order_by("organization__name", "pk")
            .first()
        )
        if membership is not None:
            return membership.organization
    membership = (
        user.staff_memberships.filter(active=True, organization__active=True)
        .select_related("organization")
        .order_by("organization__name", "pk")
        .first()
    )
    return membership.organization if membership else None


_ORG_UNSET = object()


def _compute_current_organization(request):
    explicit_slug = request.GET.get("org")
    org_slug = explicit_slug or request.session.get("organization_slug")
    if org_slug:
        organization = get_object_or_404(Organization, slug=org_slug, active=True)
        # Authenticated users may select only organizations they belong to,
        # whether the slug comes from the query string or their session.
        # Anonymous users retain public-catalog selection by either mechanism.
        user = getattr(request, "user", None)
        may_use = user is None or not user.is_authenticated
        if not may_use:
            may_use = (
                PatronProfile.objects.filter(user=user, organization=organization).exists()
                or user.staff_memberships.filter(
                    organization=organization, active=True
                ).exists()
            )
        if may_use:
            if explicit_slug:
                request.session["organization_slug"] = organization.slug
            return organization
        if not explicit_slug:
            request.session.pop("organization_slug", None)
        # Fall through to the authenticated user's home tenant.
    # An authenticated user defaults to their own tenant rather than the global
    # first-active org, so a patron never lands on the wrong library's catalog.
    user_org = organization_for_user(getattr(request, "user", None))
    if user_org is not None:
        return user_org
    return Organization.objects.filter(active=True).order_by("name").first()


def get_current_organization(request):
    # Memoize per request: the context processor and the view both resolve the
    # org, and resolution costs a couple of queries for authenticated users.
    cached = getattr(request, "_elib_current_org", _ORG_UNSET)
    if cached is not _ORG_UNSET:
        return cached
    organization = _compute_current_organization(request)
    try:
        request._elib_current_org = organization
    except (AttributeError, TypeError):
        pass
    return organization

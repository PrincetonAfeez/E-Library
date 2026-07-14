"""Multi-tenant organization resolution from requests and users."""
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import get_object_or_404

from .models import Organization


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


def staff_organization_for_user(user):
    """First active org where the user is staff — used to resolve staff areas so
    a staff+patron user isn't sent to their patron org's dashboard."""
    if user is None or not user.is_authenticated:
        return None
    membership = (
        user.staff_memberships.filter(active=True, organization__active=True)
        .select_related("organization")
        .first()
    )
    return membership.organization if membership else None


_ORG_UNSET = object()


def _compute_current_organization(request):
    explicit_slug = request.GET.get("org")
    org_slug = explicit_slug or request.session.get("organization_slug")
    if org_slug:
        organization = get_object_or_404(Organization, slug=org_slug, active=True)
        # Only write the session when the org was chosen explicitly, so anonymous
        # public/API traffic isn't forced to allocate a session cookie every hit.
        if explicit_slug:
            request.session["organization_slug"] = organization.slug
        return organization
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

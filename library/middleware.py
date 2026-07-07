"""Request middleware for staff MFA enforcement.

When a tenant sets ``Organization.require_staff_mfa``, staff who have a confirmed
TOTP device must pass a second factor (recorded in the session) before reaching
staff areas. Default-off, so tenants that don't opt in are unaffected.
"""

from __future__ import annotations

import uuid

from django.http import JsonResponse
from django.shortcuts import redirect

from .logging_utils import request_id_var


class RequestIDMiddleware:
    """Attach a correlation id to each request/response and log context."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rid = request.META.get("HTTP_X_REQUEST_ID") or uuid.uuid4().hex
        request.request_id = rid
        token = request_id_var.set(rid)
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(token)
        response["X-Request-ID"] = rid
        return response

# Staff-area path prefixes gated behind the second factor.
_PROTECTED_PREFIXES = ("/librarian", "/billing", "/api/v1/librarian")
# Never gate these (the challenge itself, MFA endpoints, auth, health).
_EXEMPT_PREFIXES = ("/mfa/", "/api/v1/account/mfa/", "/accounts/", "/healthz", "/readyz", "/status")


class StaffMfaMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        blocked = self._challenge_if_needed(request)
        return blocked if blocked is not None else self.get_response(request)

    def _challenge_if_needed(self, request):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return None
        path = request.path
        if path.startswith(_EXEMPT_PREFIXES) or not path.startswith(_PROTECTED_PREFIXES):
            return None
        if request.session.get("mfa_verified"):
            return None

        from . import mfa
        from .permissions import resolve_request_organization, user_is_staff_for_org

        # Resolve the org actually being accessed (honours ?org=), so a staffer
        # of multiple tenants can't reach an MFA-required org via a non-required
        # "primary" org.
        organization = resolve_request_organization(request)
        if organization is None or not organization.require_staff_mfa:
            return None
        if not user_is_staff_for_org(user, organization):
            return None
        if not mfa.user_has_mfa(user):
            # Org requires MFA but this user hasn't enrolled — enforcement of
            # enrollment is handled out of band; don't lock them out here.
            return None

        if path.startswith("/api/"):
            return JsonResponse(
                {"error": {"code": "mfa_required", "message": "Second factor required."}},
                status=403,
            )
        return redirect(f"/mfa/challenge/?next={path}")

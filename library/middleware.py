"""Request middleware for staff MFA enforcement

When a tenant sets ``Organization.require_staff_mfa``, staff must enroll and
pass a second factor before reaching staff areas. Session users record
verification in the session; Bearer tokens must carry an ``mfa:verified``
(or ``*``) scope — session MFA does not cover API tokens.
"""

from __future__ import annotations

import uuid
from urllib.parse import quote

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


_PROTECTED_PREFIXES = ("/librarian", "/billing", "/api/v1/librarian", "/api/v1/support")
_EXEMPT_PREFIXES = (
    "/mfa/",
    "/api/v1/account/mfa/",
    "/accounts/",
    "/healthz",
    "/readyz",
    "/status",
)


class StaffMfaMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        blocked = self._challenge_if_needed(request)
        return blocked if blocked is not None else self.get_response(request)

    def _token_mfa_ok(self, request) -> bool:
        """Bearer tokens opt into MFA-equivalent access via scope."""
        if getattr(request, "auth", None) is None:
            return False
        scopes = getattr(request, "auth_scopes", None) or []
        return "*" in scopes or "mfa:verified" in scopes

    def _challenge_if_needed(self, request):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return None
        path = request.path
        if path.startswith(_EXEMPT_PREFIXES) or not path.startswith(_PROTECTED_PREFIXES):
            return None

        from . import mfa
        from .permissions import resolve_staff_request_organization, user_is_staff_for_org

        organization = resolve_staff_request_organization(request)
        if (
            organization is None
            or not organization.require_staff_mfa
            or not user_is_staff_for_org(user, organization)
        ):
            # A staff+patron user's selected session organization may be their
            # patron tenant. Protected staff routes must still enforce MFA for
            # any active staff tenant that requires it.
            membership = (
                user.staff_memberships.filter(
                    active=True, organization__active=True, organization__require_staff_mfa=True
                )
                .select_related("organization")
                .order_by("organization__name", "pk")
                .first()
            )
            if membership is None:
                return None
            organization = membership.organization

        if self._token_mfa_ok(request):
            return None
        if mfa.session_mfa_ok(request, organization=organization):
            return None

        # Token auth without mfa:verified cannot use the HTML challenge flow.
        if getattr(request, "auth", None) is not None:
            return JsonResponse(
                {
                    "error": {
                        "code": "mfa_required",
                        "message": "API token requires mfa:verified (or *) scope for this organization.",
                    }
                },
                status=403,
            )

        if not mfa.user_has_mfa(user):
            if path.startswith("/api/"):
                return JsonResponse(
                    {
                        "error": {
                            "code": "mfa_enrollment_required",
                            "message": "Enroll MFA before accessing staff areas.",
                        }
                    },
                    status=403,
                )
            return redirect(f"/mfa/enroll/?next={quote(path)}")

        if path.startswith("/api/"):
            return JsonResponse(
                {"error": {"code": "mfa_required", "message": "Second factor required."}},
                status=403,
            )
        return redirect(f"/mfa/challenge/?next={quote(path)}")

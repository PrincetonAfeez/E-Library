"""DRF permission classes and staff role authorization helpers."""
from rest_framework import permissions

from .tenancy import get_current_organization

# Baseline permissions granted by each staff role. A membership may additionally
# carry explicit permission strings in ``StaffMembership.permissions`` (or "*").
STAFF_ROLE_PERMISSIONS = {
    "admin": {"*"},
    "branch_manager": {"circulation", "copies", "catalog", "imports", "reports", "acquisitions"},
    "librarian": {"circulation", "copies", "catalog", "reports"},
    "support": {"reports"},
}


def user_is_staff_for_org(user, organization) -> bool:
    """Staff authorization is scoped to a specific organization.

    A Django ``is_staff``/admin flag alone is not enough to read another
    tenant's operational data; a superuser is treated as global.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if organization is None:
        return False
    return user.staff_memberships.filter(active=True, organization=organization).exists()


def _active_memberships(user, organization):
    if not user or not user.is_authenticated or organization is None:
        return []
    return list(user.staff_memberships.filter(active=True, organization=organization))


def staff_permissions_for_org(user, organization) -> set:
    """Effective staff permissions (role defaults ∪ explicit grants)."""
    if user and user.is_authenticated and user.is_superuser:
        return {"*"}
    perms: set = set()
    for membership in _active_memberships(user, organization):
        perms |= set(STAFF_ROLE_PERMISSIONS.get(membership.role, set()))
        perms |= set(membership.permissions or [])
    return perms


def user_has_staff_permission(user, organization, permission: str) -> bool:
    perms = staff_permissions_for_org(user, organization)
    return "*" in perms or permission in perms


def staff_branch_ids_for_org(user, organization):
    """Branch ids the user may act on. ``None`` means all branches (an org-wide
    membership, admin/branch_manager role, or superuser)."""
    if user and user.is_authenticated and user.is_superuser:
        return None
    branch_ids: set = set()
    for membership in _active_memberships(user, organization):
        if membership.branch_id is None or membership.role in {"admin", "branch_manager"}:
            return None
        branch_ids.add(membership.branch_id)
    return branch_ids


def user_can_act_on_branch(user, organization, branch_id) -> bool:
    allowed = staff_branch_ids_for_org(user, organization)
    return allowed is None or branch_id in allowed


def resolve_request_organization(request):
    return getattr(request, "organization", None) or get_current_organization(request)


class IsAuthenticatedPatron(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and hasattr(request.user, "patron_profile")
        )


class IsLibraryStaff(permissions.BasePermission):
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        organization = resolve_request_organization(request)
        return user_is_staff_for_org(request.user, organization)


class HasStaffPermission(permissions.BasePermission):
    """Org-scoped staff access gated by a role/permission.

    The view declares ``required_staff_permission``; a member passes if their
    role defaults or explicit grants include it (or "*"). With no declared
    permission this is equivalent to :class:`IsLibraryStaff`.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        organization = resolve_request_organization(request)
        required = getattr(view, "required_staff_permission", None)
        if required is None:
            return user_is_staff_for_org(request.user, organization)
        return user_has_staff_permission(request.user, organization, required)


class TokenHasScope(permissions.BasePermission):
    required_scope = ""

    def has_permission(self, request, view):
        required = getattr(view, "required_scope", self.required_scope)
        if not required:
            return True
        # Session-authenticated users (request.auth is None) are not scoped by
        # API tokens; scope enforcement only applies to token auth.
        if request.auth is None:
            return True
        scopes = getattr(request, "auth_scopes", [])
        return required in scopes or "*" in scopes

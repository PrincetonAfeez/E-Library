"""Subscription-based entitlements (limits and feature flags).

Enforcement is opt-in: an organization with **no** subscription (or a plan with a
null limit) is treated as unlimited, so existing tenants and tests are unaffected.
Limits bite only once a plan with concrete caps is attached.
"""

from __future__ import annotations

from .models import Branch, Copy, PatronProfile, Subscription


class EntitlementError(Exception):
    """Raised when an action would exceed the organization's plan."""


def get_subscription(organization):
    return Subscription.objects.select_related("plan").filter(organization=organization).first()


def _plan(organization):
    sub = get_subscription(organization)
    return sub.plan if sub and sub.is_serviceable else None


def has_feature(organization, feature: str) -> bool:
    plan = _plan(organization)
    if plan is None:
        return True  # no active plan -> unrestricted (self-hosted / trial)
    features = plan.features or []
    return "*" in features or feature in features


def _limit(plan, attr):
    return getattr(plan, attr) if plan is not None else None


def remaining(organization, resource: str) -> int | None:
    """Remaining allowance for 'patrons' | 'copies' | 'branches'; None == unlimited."""
    plan = _plan(organization)
    cap = _limit(plan, {"patrons": "max_patrons", "copies": "max_copies", "branches": "max_branches"}[resource])
    if cap is None:
        return None
    counters = {
        "patrons": PatronProfile.objects.filter(organization=organization).count,
        "copies": Copy.objects.filter(organization=organization).count,
        "branches": Branch.objects.filter(organization=organization).count,
    }
    return max(0, cap - counters[resource]())


def assert_within_limit(organization, resource: str, adding: int = 1) -> None:
    left = remaining(organization, resource)
    if left is not None and adding > left:
        raise EntitlementError(
            f"Your plan's {resource} limit has been reached. Upgrade to add more."
        )


def assert_feature(organization, feature: str) -> None:
    if not has_feature(organization, feature):
        raise EntitlementError(f"Your plan does not include the '{feature}' feature.")

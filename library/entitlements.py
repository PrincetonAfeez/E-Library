"""Subscription-based entitlements (limits and feature flags).
 
Behaviour:
* **No subscription** — unrestricted unless ``settings.SAAS_MODE`` is True
  (hosted multi-tenant), in which case features are denied.
* **Serviceable subscription** (trialing / active / past_due within grace) —
  plan features and caps apply.
* **Non-serviceable** (canceled, past_due past grace) — features denied and
  remaining allowances are zero (never treated as unlimited).
"""

from __future__ import annotations

from django.conf import settings

from .models import Branch, Copy, PatronProfile, Subscription


class EntitlementError(Exception):
    """Raised when an action would exceed the organization's plan."""


def get_subscription(organization):
    return Subscription.objects.select_related("plan").filter(organization=organization).first()


def _plan_state(organization) -> tuple[object | None, str]:
    """Return (plan_or_None, state) where state is none|ok|inactive."""
    sub = get_subscription(organization)
    if sub is None:
        return None, "none"
    if sub.is_serviceable:
        return sub.plan, "ok"
    return sub.plan, "inactive"


def has_feature(organization, feature: str) -> bool:
    plan, state = _plan_state(organization)
    if state == "none":
        return not getattr(settings, "SAAS_MODE", False)
    if state == "inactive" or plan is None:
        return False
    features = getattr(plan, "features", None) or []
    return "*" in features or feature in features


def _limit(plan, attr):
    return getattr(plan, attr) if plan is not None else None


def remaining(organization, resource: str) -> int | None:
    """Remaining allowance for 'patrons' | 'copies' | 'branches'; None == unlimited."""
    plan, state = _plan_state(organization)
    if state == "inactive":
        return 0
    if state == "none" and getattr(settings, "SAAS_MODE", False):
        return 0
    cap = _limit(
        plan, {"patrons": "max_patrons", "copies": "max_copies", "branches": "max_branches"}[resource]
    )
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

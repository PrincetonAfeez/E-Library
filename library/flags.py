"""Feature flags for safe rollout.
 
Resolution order for a key:
1. A per-organization ``FeatureFlag`` row (tenant override).
2. A global ``FeatureFlag`` row (organization is null).
3. ``settings.FEATURE_FLAGS`` (static default).
4. ``False``.

DB-backed flags can be toggled at runtime via the admin without a deploy.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache

from .models import FeatureFlag

_CACHE_TTL = 30


def flag_enabled(key: str, organization=None) -> bool:
    if organization is not None:
        cache_key = f"flag:{key}:{organization.pk}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        row = FeatureFlag.objects.filter(key=key, organization=organization).first()
        if row is not None:
            cache.set(cache_key, row.enabled, _CACHE_TTL)
            return row.enabled

    glob = FeatureFlag.objects.filter(key=key, organization__isnull=True).first()
    if glob is not None:
        return glob.enabled
    return bool(getattr(settings, "FEATURE_FLAGS", {}).get(key, False))


def set_flag(key: str, enabled: bool, *, organization=None, description: str = "") -> FeatureFlag:
    flag, _ = FeatureFlag.objects.update_or_create(
        key=key,
        organization=organization,
        defaults={"enabled": enabled, "description": description},
    )
    if organization is not None:
        cache.delete(f"flag:{key}:{organization.pk}")
    return flag

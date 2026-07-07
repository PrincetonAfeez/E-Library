"""Lightweight cache-backed rate limiting for HTML endpoints.

DRF endpoints are throttled by REST framework; these decorators cover the
server-rendered views (registration, login, search) that DRF does not see.
The limiter fails **open** if the cache backend is unavailable, so a Redis
outage degrades protection rather than taking the site down.
"""

from __future__ import annotations

from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse


def client_ip(request) -> str:
    """Best-effort client IP.

    ``X-Forwarded-For`` is attacker-controlled, so we only trust it when the
    deployment declares how many proxies sit in front (``RATELIMIT_TRUSTED_PROXY_COUNT``)
    and take the entry that many hops from the right. With no trusted proxies we
    use ``REMOTE_ADDR``, which the app server sets and a client cannot spoof.
    """
    trusted = getattr(settings, "RATELIMIT_TRUSTED_PROXY_COUNT", 0)
    if trusted > 0:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if len(parts) >= trusted:
            return parts[-trusted]
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def is_rate_limited(request, *, scope: str, limit: int, window: int) -> bool:
    """Return True if this client has exceeded ``limit`` in ``window`` seconds."""
    return _over_limit(f"{scope}:{client_ip(request)}", limit, window)


def _over_limit(bucket: str, limit: int, window: int) -> bool:
    cache_key = f"ratelimit:{bucket}"
    try:
        current = cache.get(cache_key)
        if current is None:
            cache.set(cache_key, 1, window)
            return False
        if current >= limit:
            return True
        try:
            cache.incr(cache_key)
        except ValueError:
            # Key expired between get and incr; restart the window.
            cache.set(cache_key, 1, window)
        return False
    except Exception:
        # Cache unavailable -> fail open.
        return False


def rate_limit(*, scope: str, limit: int, window: int, methods=("POST",)):
    """Limit ``limit`` requests per ``window`` seconds per client IP for the
    given HTTP ``methods``. Returns HTTP 429 when exceeded."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method in methods:
                bucket = f"{scope}:{client_ip(request)}"
                if _over_limit(bucket, limit, window):
                    return HttpResponse(
                        "Too many requests. Please slow down and try again shortly.",
                        status=429,
                    )
            return view(request, *args, **kwargs)

        return wrapper

    return decorator

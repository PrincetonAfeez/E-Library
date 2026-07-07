"""Outbound HTTP safety helpers.

``validate_outbound_url`` rejects non-http(s) schemes before any ``urlopen``,
closing the ``file:``/custom-scheme class of SSRF that a tenant-supplied URL
(e.g. a webhook target) could otherwise reach.
"""

from __future__ import annotations

from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}


class UnsafeUrlError(ValueError):
    pass


def validate_outbound_url(url: str) -> str:
    scheme = urlparse(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"Refusing to fetch non-http(s) URL scheme: {scheme!r}")
    return url

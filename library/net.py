"""Outbound HTTP safety helpers

Rejects non-http(s) schemes and blocks private, loopback, link-local, and
cloud-metadata addresses. ``safe_urlopen`` re-resolves and re-checks the host
immediately before connect and on every redirect (closes most DNS-rebinding
TOCTOU without breaking TLS SNI/certificate checks).
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

from django.conf import settings

ALLOWED_SCHEMES = {"http", "https"}

_BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "metadata",
}


class UnsafeUrlError(Exception):
    """Raised when an outbound URL is rejected. Not a ValueError — callers'
    ``except ValueError`` must not swallow SSRF denials."""

    pass


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or ip in ipaddress.ip_network("169.254.0.0/16")
        or ip in ipaddress.ip_network("fc00::/7")
        or ip in ipaddress.ip_network("fe80::/10")
        or ip == ipaddress.ip_address("169.254.169.254")
        or ip == ipaddress.ip_address("fd00:ec2::254")
    )


def _host_allowed_override(host: str) -> bool:
    allow = {h.lower() for h in (getattr(settings, "OUTBOUND_URL_ALLOW_HOSTS", None) or [])}
    return host in allow


def _assert_host_safe(host: str) -> None:
    host = (host or "").strip("[]").lower()
    if not host or host in _BLOCKED_HOSTS:
        raise UnsafeUrlError(f"Refusing to fetch URL targeting a private or metadata host: {host!r}")
    if _host_allowed_override(host):
        return
    try:
        if _ip_blocked(ipaddress.ip_address(host)):
            raise UnsafeUrlError(
                f"Refusing to fetch URL targeting a private or metadata host: {host!r}"
            )
        return
    except ValueError:
        pass
    if host.endswith((".test", ".invalid", ".example", ".localhost")):
        return
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Refusing URL; host did not resolve: {host!r}") from exc
    for info in infos:
        addr = info[4][0]
        try:
            if _ip_blocked(ipaddress.ip_address(addr)):
                raise UnsafeUrlError(
                    f"Refusing to fetch URL targeting a private or metadata host: {host!r}"
                )
        except ValueError:
            continue


def validate_outbound_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"Refusing to fetch non-http(s) URL scheme: {scheme!r}")
    if not parsed.hostname:
        raise UnsafeUrlError("Refusing URL without a hostname.")
    _assert_host_safe(parsed.hostname)
    return url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_outbound_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_urlopen(url: str, *, data=None, headers: dict | None = None, method: str = "GET", timeout: int = 8):
    """Validate, re-resolve, then fetch; re-validate redirect targets."""
    validate_outbound_url(url)
    # Re-check DNS immediately before connect (narrows the TOCTOU window).
    host = urlparse(url).hostname
    if host:
        _assert_host_safe(host)
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    opener = urllib.request.build_opener(_SafeRedirectHandler)
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.URLError as exc:
        raise UnsafeUrlError(f"Outbound fetch failed: {exc}") from exc

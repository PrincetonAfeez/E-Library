"""SIP2 (Standard Interchange Protocol) message handling for self-check kiosks.

Implements the request→response message pairs used by self-checkout machines and
RFID gates, mapping them onto the existing circulation services. The message
parsing/formatting is pure and testable; a thin socket server (management command)
feeds raw lines to :func:`handle_message`.
"""

from __future__ import annotations

from django.utils import timezone

from .models import Copy, PatronProfile
from .services import DomainError, borrow_work, staff_checkin

FS = "|"  # SIP2 field separator


def _fields(body: str) -> dict:
    """Parse variable fields (2-char code + value, separated by '|')."""
    out: dict[str, str] = {}
    for part in body.split(FS):
        if len(part) >= 2:
            out.setdefault(part[:2], part[2:])
    return out


def _yn(value: bool) -> str:
    return "Y" if value else "N"


def _timestamp(now=None) -> str:
    return (now or timezone.now()).strftime("%Y%m%d    %H%M%S")


def handle_message(raw: str, *, organization, actor=None) -> str:
    """Dispatch a SIP2 request message and return the response message string."""
    raw = raw.strip()
    if len(raw) < 2:
        return "96"  # Request SC Resend
    code = raw[:2]
    handler = _HANDLERS.get(code)
    if handler is None:
        return "96"
    return handler(raw, organization, actor)


def _login(raw, organization, actor) -> str:
    # 93 Login -> 94 Login Response (ok=1 when well-formed).
    return "941"


def _sc_status(raw, organization, actor) -> str:
    # 99 SC Status -> 98 ACS Status. Online=Y, checkin/checkout allowed.
    return f"98YYYYNN00000{_timestamp()}2.00AOAM|"


def _patron_status(raw, organization, actor) -> str:
    # 23 Patron Status Request -> 24 Patron Status Response.
    fields = _fields(raw)
    card = fields.get("AA", "")
    patron = PatronProfile.objects.filter(
        organization=organization, library_card_number=card
    ).select_related("user").first()
    if patron is None:
        # Patron unknown: deny privileges (14 char status block of Y).
        return f"24{'Y' * 14}000{_timestamp()}AO|AA{card}|BLN|"
    blocked = patron.status != "active"
    status = ("Y" if blocked else " ") + " " * 13
    name = patron.user.get_full_name() or patron.user.get_username()
    return (
        f"24{status}000{_timestamp()}AO|AA{card}|AE{name}|"
        f"BL{_yn(True)}|CQ{_yn(not blocked)}|"
    )


def _checkout(raw, organization, actor) -> str:
    # 11 Checkout -> 12 Checkout Response.
    fields = _fields(raw)
    card = fields.get("AA", "")
    barcode = fields.get("AB", "")
    patron = PatronProfile.objects.filter(
        organization=organization, library_card_number=card
    ).first()
    copy = Copy.objects.filter(organization=organization, barcode=barcode).select_related(
        "edition__work"
    ).first()
    if patron is None or copy is None:
        return _checkout_fail(card, barcode, "Unknown patron or item.")
    work = copy.edition.work
    try:
        loan = borrow_work(patron=patron, work=work, branch=copy.branch, actor=actor, source="sip2")
    except DomainError as exc:
        return _checkout_fail(card, barcode, str(exc))
    due = loan.due_at.strftime("%Y%m%d    %H%M%S")
    return (
        f"121NNN{_timestamp()}AO|AA{card}|AB{barcode}|"
        f"AJ{work.canonical_title}|AH{due}|AFCheckout ok.|"
    )


def _checkout_fail(card, barcode, message) -> str:
    return f"120NUN{_timestamp()}AO|AA{card}|AB{barcode}|AF{message}|"


def _checkin(raw, organization, actor) -> str:
    # 09 Checkin -> 10 Checkin Response.
    fields = _fields(raw)
    barcode = fields.get("AB", "")
    copy = Copy.objects.filter(organization=organization, barcode=barcode).first()
    if copy is None:
        return f"100NUN{_timestamp()}AO|AB{barcode}|AFUnknown item.|"
    try:
        staff_checkin(copy=copy, actor=actor)
    except DomainError as exc:
        return f"100NUN{_timestamp()}AO|AB{barcode}|AF{exc}|"
    return f"101YNN{_timestamp()}AO|AB{barcode}|AQ{copy.branch.slug}|AFCheckin ok.|"


_HANDLERS = {
    "93": _login,
    "99": _sc_status,
    "23": _patron_status,
    "11": _checkout,
    "09": _checkin,
}

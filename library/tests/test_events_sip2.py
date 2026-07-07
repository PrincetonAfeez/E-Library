"""Tests for events/room booking, SIP2 self-check, and i18n (final increment)."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from library import events, sip2
from library.models import (
    Branch,
    Copy,
    CopyStatus,
    Edition,
    Event,
    EventRegistration,
    Organization,
    PatronProfile,
    RegistrationStatus,
    Room,
    Work,
)
from library.services import DomainError

pytestmark = pytest.mark.django_db(transaction=True)


def make_catalog():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    copy = Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="ITEM1")
    return org, branch, work, edition, copy


def make_patron(org, branch, n=1, card="CARD-1"):
    user = get_user_model().objects.create_user(username=f"reader{n}", email=f"r{n}@x.test")
    return PatronProfile.objects.create(
        user=user, organization=org, library_card_number=card, home_branch=branch
    )


def _api(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


# --------------------------------------------------------------------------- #
# Room reservations
# --------------------------------------------------------------------------- #
def test_room_reservation_conflict():
    org, branch, work, edition, copy = make_catalog()
    room = Room.objects.create(organization=org, branch=branch, code="A", name="Study A")
    p1 = make_patron(org, branch, 1, "C1")
    p2 = make_patron(org, branch, 2, "C2")
    start = timezone.now() + timedelta(days=1)
    events.reserve_room(patron=p1, room=room, starts_at=start, ends_at=start + timedelta(hours=2))
    # Overlapping booking is rejected.
    with pytest.raises(DomainError):
        events.reserve_room(
            patron=p2, room=room, starts_at=start + timedelta(hours=1), ends_at=start + timedelta(hours=3)
        )
    # Non-overlapping is fine.
    events.reserve_room(
        patron=p2, room=room, starts_at=start + timedelta(hours=2), ends_at=start + timedelta(hours=3)
    )


def test_room_reserve_past_rejected():
    org, branch, work, edition, copy = make_catalog()
    room = Room.objects.create(organization=org, branch=branch, code="A", name="A")
    p1 = make_patron(org, branch, 1, "C1")
    past = timezone.now() - timedelta(hours=1)
    with pytest.raises(DomainError):
        events.reserve_room(patron=p1, room=room, starts_at=past, ends_at=past + timedelta(hours=1))


# --------------------------------------------------------------------------- #
# Event registration + waitlist
# --------------------------------------------------------------------------- #
def test_event_capacity_waitlists_and_promotes():
    org, branch, work, edition, copy = make_catalog()
    start = timezone.now() + timedelta(days=2)
    event = Event.objects.create(
        organization=org, title="Story Time", starts_at=start, ends_at=start + timedelta(hours=1), capacity=1
    )
    p1 = make_patron(org, branch, 1, "C1")
    p2 = make_patron(org, branch, 2, "C2")
    r1 = events.register_for_event(patron=p1, event=event)
    r2 = events.register_for_event(patron=p2, event=event)
    assert r1.status == RegistrationStatus.REGISTERED
    assert r2.status == RegistrationStatus.WAITLISTED

    events.cancel_registration(registration=r1)
    r2.refresh_from_db()
    assert r2.status == RegistrationStatus.REGISTERED  # promoted


def test_event_api_flow():
    org, branch, work, edition, copy = make_catalog()
    start = timezone.now() + timedelta(days=1)
    event = Event.objects.create(
        organization=org, title="Talk", starts_at=start, ends_at=start + timedelta(hours=1)
    )
    p1 = make_patron(org, branch, 1, "C1")
    anon = APIClient()
    assert anon.get("/api/v1/events/", secure=True, HTTP_HOST="testserver").status_code == 200
    resp = _api(p1.user).post(f"/api/v1/events/{event.pk}/register/", {}, format="json", secure=True)
    assert resp.status_code == 201
    assert EventRegistration.objects.filter(event=event, patron=p1).exists()


# --------------------------------------------------------------------------- #
# SIP2
# --------------------------------------------------------------------------- #
def test_sip2_checkout_and_checkin():
    org, branch, work, edition, copy = make_catalog()
    make_patron(org, branch, 1, "CARD-1")

    # 11 Checkout: <code>...|AA card|AB barcode|
    resp = sip2.handle_message(
        "11YN20240101    120000AOAM|AACARD-1|ABITEM1|", organization=org
    )
    assert resp.startswith("121")  # ok flag 1
    assert "ABITEM1" in resp
    copy.refresh_from_db()
    assert copy.status == CopyStatus.LOANED

    # 09 Checkin.
    resp = sip2.handle_message("09N20240101    120000AOAM|ABITEM1|", organization=org)
    assert resp.startswith("101")
    copy.refresh_from_db()
    assert copy.status == CopyStatus.AVAILABLE


def test_sip2_patron_status_and_unknown():
    org, branch, work, edition, copy = make_catalog()
    make_patron(org, branch, 1, "CARD-1")
    resp = sip2.handle_message("23000AOAM|AACARD-1|", organization=org)
    assert resp.startswith("24")
    assert "AACARD-1" in resp
    # Unknown command -> resend.
    assert sip2.handle_message("XY", organization=org) == "96"


def test_sip2_checkout_unknown_item_fails():
    org, branch, work, edition, copy = make_catalog()
    make_patron(org, branch, 1, "CARD-1")
    resp = sip2.handle_message("11YN...AOAM|AACARD-1|ABNOPE|", organization=org)
    assert resp.startswith("120")  # not ok


# --------------------------------------------------------------------------- #
# i18n
# --------------------------------------------------------------------------- #
def test_language_switch(client):
    org = Organization.objects.create(name="Lib", slug="lib")
    Branch.objects.create(organization=org, name="Main", slug="main")
    resp = client.post("/i18n/setlang/", {"language": "es", "next": "/"}, secure=True)
    assert resp.status_code in (302, 200)

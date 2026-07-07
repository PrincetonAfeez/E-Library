"""Tests for multi-channel notifications and acquisitions (Increment 7)."""

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from rest_framework.test import APIClient

from library import acquisitions, channels
from library.models import (
    Branch,
    Copy,
    Edition,
    Fund,
    Organization,
    PatronProfile,
    PurchaseOrderStatus,
    StaffMembership,
    StaffRole,
    Vendor,
    Work,
)
from library.services import DomainError, borrow_work, drain_outbox

pytestmark = pytest.mark.django_db(transaction=True)


def make_catalog():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="C1")
    user = get_user_model().objects.create_user(username="reader", email="r@example.test")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    return org, branch, work, edition, patron


def _staff(org):
    staff = get_user_model().objects.create_user(username="mgr", is_staff=True)
    StaffMembership.objects.create(
        user=staff, organization=org, branch=None, role=StaffRole.BRANCH_MANAGER
    )
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=staff)
    client.defaults["secure"] = True
    return client, staff


# --------------------------------------------------------------------------- #
# Multi-channel notifications
# --------------------------------------------------------------------------- #
def test_default_channel_is_email():
    org, branch, work, edition, patron = make_catalog()
    channels.sms_outbox.clear()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    drain_outbox()
    assert len(mail.outbox) == 1
    assert not channels.sms_outbox


def test_sms_and_email_fanout():
    org, branch, work, edition, patron = make_catalog()
    patron.sms_number = "+15550001111"
    patron.notification_channels = ["email", "sms"]
    patron.save(update_fields=["sms_number", "notification_channels"])
    channels.sms_outbox.clear()
    mail.outbox.clear()

    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    drain_outbox()
    assert len(mail.outbox) == 1
    assert len(channels.sms_outbox) == 1
    assert channels.sms_outbox[0]["to"] == "+15550001111"


def test_channel_delivery_is_idempotent_per_channel():
    from library.models import NotificationDelivery

    org, branch, work, edition, patron = make_catalog()
    patron.notification_channels = ["email", "push"]
    patron.push_token = "devicetoken"
    patron.save(update_fields=["notification_channels", "push_token"])
    channels.push_outbox.clear()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    drain_outbox()
    drain_outbox()  # second drain must not re-deliver (events already processed)
    assert (
        NotificationDelivery.objects.filter(template_key="loan_borrowed", status="sent").count()
        == 2  # one per channel, exactly once each
    )


# --------------------------------------------------------------------------- #
# Acquisitions
# --------------------------------------------------------------------------- #
def test_receive_creates_copies_and_spends_fund():
    org, branch, work, edition, patron = make_catalog()
    vendor = Vendor.objects.create(organization=org, code="baker", name="Baker & Taylor")
    fund = Fund.objects.create(organization=org, code="2026", name="FY26", budget_cents=100000)

    po = acquisitions.create_purchase_order(organization=org, vendor=vendor, fund=fund)
    line = acquisitions.add_line(
        purchase_order=po, edition=edition, branch=branch, quantity=3, unit_cost_cents=1500
    )
    acquisitions.place_order(purchase_order=po)

    before = Copy.objects.filter(organization=org).count()
    acquisitions.receive_line(line=line, quantity=3)
    assert Copy.objects.filter(organization=org).count() == before + 3
    fund.refresh_from_db()
    assert fund.spent_cents == 4500  # 3 * 1500
    po.refresh_from_db()
    assert po.status == PurchaseOrderStatus.RECEIVED


def test_partial_receive_keeps_order_open():
    org, branch, work, edition, patron = make_catalog()
    vendor = Vendor.objects.create(organization=org, code="v", name="V")
    fund = Fund.objects.create(organization=org, code="f", name="F", budget_cents=100000)
    po = acquisitions.create_purchase_order(organization=org, vendor=vendor, fund=fund)
    line = acquisitions.add_line(
        purchase_order=po, edition=edition, branch=branch, quantity=5, unit_cost_cents=100
    )
    acquisitions.place_order(purchase_order=po)
    acquisitions.receive_line(line=line, quantity=2)
    po.refresh_from_db()
    line.refresh_from_db()
    assert po.status == PurchaseOrderStatus.ORDERED
    assert line.received_quantity == 2


def test_budget_enforced():
    org, branch, work, edition, patron = make_catalog()
    vendor = Vendor.objects.create(organization=org, code="v", name="V")
    fund = Fund.objects.create(organization=org, code="f", name="F", budget_cents=1000)
    po = acquisitions.create_purchase_order(organization=org, vendor=vendor, fund=fund)
    acquisitions.add_line(
        purchase_order=po, edition=edition, branch=branch, quantity=5, unit_cost_cents=1000
    )
    # 5000c order cannot be committed against a 1000c budget — rejected at placement.
    with pytest.raises(DomainError):
        acquisitions.place_order(purchase_order=po)


def test_acquisitions_api_flow():
    org, branch, work, edition, patron = make_catalog()
    Vendor.objects.create(organization=org, code="baker", name="Baker")
    Fund.objects.create(organization=org, code="2026", name="FY26", budget_cents=100000)
    client, _ = _staff(org)

    resp = client.post(
        "/api/v1/librarian/acquisitions/orders/",
        {"vendor": "baker", "fund": "2026"},
        format="json",
        secure=True,
    )
    assert resp.status_code == 201
    po_id = resp.json()["data"]["id"]

    resp = client.post(
        f"/api/v1/librarian/acquisitions/orders/{po_id}/lines/",
        {"edition_id": edition.pk, "branch": "main", "quantity": 2, "unit_cost_cents": 1000},
        format="json",
        secure=True,
    )
    assert resp.status_code == 201
    line_id = resp.json()["data"]["lines"][0]["id"]

    assert client.post(
        f"/api/v1/librarian/acquisitions/orders/{po_id}/place/", {}, format="json", secure=True
    ).status_code == 200
    resp = client.post(
        f"/api/v1/librarian/acquisitions/lines/{line_id}/receive/",
        {"quantity": 2},
        format="json",
        secure=True,
    )
    assert resp.status_code == 200
    assert Copy.objects.filter(organization=org, edition=edition).count() == 3  # 1 original + 2


def test_acquisitions_requires_permission():
    org, branch, work, edition, patron = make_catalog()
    # A plain librarian lacks 'acquisitions'.
    staff = get_user_model().objects.create_user(username="liv", is_staff=True)
    StaffMembership.objects.create(
        user=staff, organization=org, branch=None, role=StaffRole.LIBRARIAN
    )
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=staff)
    assert client.get("/api/v1/librarian/acquisitions/orders/", secure=True).status_code == 403

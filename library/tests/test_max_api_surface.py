"""Focused HTTP coverage for library.api endpoints not thoroughly exercised elsewhere."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from library.models import (
    Author,
    Branch,
    Consortium,
    ConsortiumMembership,
    Copy,
    CopyStatus,
    DigitalAsset,
    DigitalAssetFormat,
    DigitalLicense,
    Edition,
    Event,
    Fee,
    FeeType,
    Fund,
    LicenseModel,
    Organization,
    PatronProfile,
    Plan,
    PublicStatus,
    StaffMembership,
    StaffRole,
    Subscription,
    SubscriptionStatus,
    Vendor,
    Work,
)
from library.services import borrow_work, place_hold, rebuild_work_search_document

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


def _slug(prefix: str = "maxsurf") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _staff_setup(*, with_plan: bool = True):
    slug = _slug("org")
    org = Organization.objects.create(name=f"Org {slug}", slug=slug)
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    admin = User.objects.create_user(username=f"admin-{slug}", is_staff=True)
    StaffMembership.objects.create(
        user=admin, organization=org, branch=None, role=StaffRole.ADMIN
    )
    plan = None
    if with_plan:
        plan, _ = Plan.objects.update_or_create(
            slug=f"plan-{slug}",
            defaults={
                "name": "Test Plan",
                "price_cents": 0,
                "max_branches": 5,
                "max_patrons": 1000,
                "max_copies": 5000,
                "features": ["*"],
            },
        )
    return org, branch, admin, plan


def _support_staff(org, branch):
    user = User.objects.create_user(username=f"support-{_slug()}", is_staff=True)
    StaffMembership.objects.create(
        user=user, organization=org, branch=branch, role=StaffRole.SUPPORT
    )
    return user


def _patron(org, branch, n: int = 1):
    tag = _slug("patron")
    user = User.objects.create_user(
        username=f"reader-{tag}-{n}", email=f"p{n}@{tag}.test"
    )
    return PatronProfile.objects.create(
        user=user,
        organization=org,
        library_card_number=f"CARD-{tag}-{n}",
        home_branch=branch,
    )


def _api(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


def _anon():
    client = APIClient(enforce_csrf_checks=False)
    client.defaults["secure"] = True
    return client


def _catalog(org, branch, *, title=None, slug=None, barcode=None, with_copy: bool = True):
    slug = slug or _slug("work")
    title = title or f"Title {slug}"
    work = Work.objects.create(
        canonical_title=title,
        slug=slug,
        public_status=PublicStatus.PUBLISHED,
        summary="Cyberpunk hacker in cyberspace.",
    )
    author, _ = Author.objects.get_or_create(name="William Gibson")
    work.authors.add(author)
    edition = Edition.objects.create(
        work=work,
        isbn_13=f"978{uuid.uuid4().hex[:10]}",
        public_status=PublicStatus.PUBLISHED,
    )
    copy = None
    if with_copy:
        copy = Copy.objects.create(
            organization=org,
            edition=edition,
            branch=branch,
            barcode=barcode or f"BC-{_slug()}",
            public_visible=True,
        )
    rebuild_work_search_document(work.pk)
    return work, edition, copy


def _digital(org, branch):
    work = Work.objects.create(
        canonical_title="Neuromancer",
        slug=_slug("ebook"),
        public_status=PublicStatus.PUBLISHED,
    )
    edition = Edition.objects.create(work=work, isbn_13=f"978{uuid.uuid4().hex[:10]}", format="ebook")
    lic = DigitalLicense.objects.create(
        organization=org,
        edition=edition,
        license_model=LicenseModel.ONE_COPY_ONE_USER,
        concurrent_limit=1,
        content_url="https://cdn.example/epub",
        loan_period_days=21,
    )
    DigitalAsset.objects.create(
        edition=edition,
        fmt=DigitalAssetFormat.TEXT,
        title="Neuromancer",
        text_content=[
            {"title": "Chapter 1", "body": "The sky above the port was the color of television."},
        ],
    )
    return work, edition, lic


def _org_qs(org):
    return f"?org={org.slug}"


def _staff_path(path: str, org) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}org={org.slug}"


# --------------------------------------------------------------------------- #
# Catalog search / suggest / semantic / NL
# --------------------------------------------------------------------------- #
def test_catalog_search_api():
    org, branch, _admin, _ = _staff_setup()
    work, _edition, _copy = _catalog(org, branch, title="Snow Crash")
    resp = _anon().get(f"/api/v1/catalog/search/{_org_qs(org)}&q=Snow", secure=True)
    assert resp.status_code == 200
    payload = resp.json()
    assert any(row["slug"] == work.slug for row in payload["data"])
    assert "facets" in payload


def test_catalog_search_no_org_returns_404():
    resp = _anon().get("/api/v1/catalog/search/?q=test", secure=True)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "no_organization"


def test_search_suggest_api():
    org, branch, _admin, _ = _staff_setup()
    _catalog(org, branch, title="Snow Crash")
    resp = _anon().get(f"/api/v1/catalog/suggest/{_org_qs(org)}&q=snow", secure=True)
    assert resp.status_code == 200
    assert any("Snow" in item.get("value", "") for item in resp.json()["data"])


def test_semantic_search_api():
    org, branch, _admin, _ = _staff_setup()
    work, _edition, _copy = _catalog(org, branch, title="Neuromancer")
    work.summary = "Cyberpunk hacker jacks into cyberspace."
    work.save(update_fields=["summary"])
    rebuild_work_search_document(work.pk)
    resp = _anon().get(
        f"/api/v1/catalog/semantic/{_org_qs(org)}&q=cyberpunk+hacker", secure=True
    )
    assert resp.status_code == 200
    assert resp.json()["count"] >= 1


def test_nl_search_api():
    org, branch, _admin, _ = _staff_setup()
    _catalog(org, branch, title="Neuromancer")
    resp = _anon().get(
        f"/api/v1/catalog/nl-search/{_org_qs(org)}&q=available+cyberpunk+books", secure=True
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "parsed" in body
    assert body["parsed"]["filters"].get("availability") == "available"


# --------------------------------------------------------------------------- #
# Reviews, reading lists, notification prefs, fees
# --------------------------------------------------------------------------- #
def test_work_reviews_get_and_post():
    org, branch, _admin, _ = _staff_setup()
    work, _edition, _copy = _catalog(org, branch)
    patron = _patron(org, branch)
    client = _api(patron.user)

    post = client.post(
        f"/api/v1/catalog/works/{work.slug}/reviews/",
        {"rating": 5, "body": "Excellent"},
        format="json",
        secure=True,
    )
    assert post.status_code == 201
    assert post.json()["data"]["rating"] == 5

    get = _anon().get(f"/api/v1/catalog/works/{work.slug}/reviews/", secure=True)
    assert get.status_code == 200
    assert get.json()["rating"]["count"] == 1


def test_reading_lists_get_create_and_add_item():
    org, branch, _admin, _ = _staff_setup()
    work, _edition, _copy = _catalog(org, branch)
    patron = _patron(org, branch)
    client = _api(patron.user)

    created = client.post(
        "/api/v1/account/lists/",
        {"name": "Summer reads", "public": True},
        format="json",
        secure=True,
    )
    assert created.status_code == 201
    list_id = created.json()["data"]["id"]

    listed = client.get("/api/v1/account/lists/", secure=True)
    assert listed.status_code == 200
    assert any(row["id"] == list_id for row in listed.json()["data"])

    item = client.post(
        f"/api/v1/account/lists/{list_id}/items/",
        {"work_slug": work.slug},
        format="json",
        secure=True,
    )
    assert item.status_code == 200
    assert item.json()["data"]["work_count"] == 1


def test_notification_prefs_get_and_update():
    org, branch, _admin, _ = _staff_setup()
    patron = _patron(org, branch)
    client = _api(patron.user)

    initial = client.get("/api/v1/account/notifications/", secure=True)
    assert initial.status_code == 200
    assert initial.json()["data"]["channels"]

    updated = client.post(
        "/api/v1/account/notifications/",
        {"preferences": {"courtesy": False}, "channels": ["email"], "unsubscribed": True},
        format="json",
        secure=True,
    )
    assert updated.status_code == 200
    data = updated.json()["data"]
    assert data["preferences"] == {"courtesy": False}
    assert data["unsubscribed"] is True


def test_account_fees_list():
    org, branch, _admin, _ = _staff_setup(with_plan=False)
    patron = _patron(org, branch)
    fee = Fee.objects.create(
        organization=org,
        patron=patron,
        fee_type=FeeType.MANUAL,
        amount_cents=500,
        description="desk fine",
    )
    assert fee.pk
    patron.user.refresh_from_db()
    resp = _api(patron.user).get("/api/v1/account/fees/", secure=True)
    assert resp.status_code == 200
    body = resp.json()
    assert body["balance_cents"] == 500
    assert len(body["fees"]) == 1
    assert body["fees"][0]["amount_cents"] == 500


# --------------------------------------------------------------------------- #
# Digital lending surface
# --------------------------------------------------------------------------- #
def test_digital_account_borrow_access_return():
    org, branch, _admin, _ = _staff_setup()
    _work, edition, _lic = _digital(org, branch)
    patron = _patron(org, branch)
    client = _api(patron.user)

    borrow = client.post(
        f"/api/v1/digital/editions/{edition.pk}/borrow/", {}, format="json", secure=True
    )
    assert borrow.status_code == 201
    loan_id = borrow.json()["data"]["id"]

    account = client.get("/api/v1/digital/", secure=True)
    assert account.status_code == 200
    assert len(account.json()["loans"]) == 1

    access = client.get(f"/api/v1/digital/loans/{loan_id}/access/", secure=True)
    assert access.status_code == 200
    access_data = access.json()["data"]
    assert (
        "content_token" in access_data
        or access_data.get("format") in {"external", "text"}
        or "chapters" in access_data
    )

    returned = client.post(
        f"/api/v1/digital/loans/{loan_id}/return/", {}, format="json", secure=True
    )
    assert returned.status_code == 204


def test_digital_reader_and_progress():
    org, branch, _admin, _ = _staff_setup()
    _work, edition, _lic = _digital(org, branch)
    patron = _patron(org, branch)
    client = _api(patron.user)

    loan_id = client.post(
        f"/api/v1/digital/editions/{edition.pk}/borrow/", {}, format="json", secure=True
    ).json()["data"]["id"]

    reader = client.get(f"/api/v1/digital/loans/{loan_id}/reader/", secure=True)
    assert reader.status_code == 200
    assert reader.json()["data"]["chapters"]

    progress = client.post(
        f"/api/v1/digital/loans/{loan_id}/progress/",
        {"locator": "chapter:1", "percent": 25},
        format="json",
        secure=True,
    )
    assert progress.status_code == 200
    assert progress.json()["data"]["locator"] == "chapter:1"


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
def test_events_list_and_register():
    org, branch, _admin, _ = _staff_setup()
    start = timezone.now() + timedelta(days=3)
    event = Event.objects.create(
        organization=org,
        branch=branch,
        title="Author talk",
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        capacity=20,
    )
    patron = _patron(org, branch)

    listed = _anon().get(f"/api/v1/events/{_org_qs(org)}", secure=True)
    assert listed.status_code == 200
    assert any(row["id"] == event.pk for row in listed.json()["data"])

    registered = _api(patron.user).post(
        f"/api/v1/events/{event.pk}/register/", {}, format="json", secure=True
    )
    assert registered.status_code == 201
    assert registered.json()["data"]["status"]


# --------------------------------------------------------------------------- #
# Staff: imports, checkout, copies, MARC, policies, acquisitions, billing
# --------------------------------------------------------------------------- #
def test_librarian_imports_get():
    org, branch, admin, _ = _staff_setup()
    client = _api(admin)

    empty = client.get(_staff_path("/api/v1/librarian/imports/", org), secure=True)
    assert empty.status_code == 200
    assert empty.json()["data"] == []

    staged = client.post(
        _staff_path("/api/v1/librarian/imports/", org),
        {"rows": [{"title": "Imported Row", "isbn": "9780000000999", "barcode": "IMP-1"}]},
        format="json",
        secure=True,
    )
    assert staged.status_code == 201

    listed = client.get(_staff_path("/api/v1/librarian/imports/", org), secure=True)
    assert listed.status_code == 200
    assert len(listed.json()["data"]) == 1


def test_staff_checkout_and_checkin():
    org, branch, admin, _ = _staff_setup()
    work, _edition, copy = _catalog(org, branch)
    patron = _patron(org, branch)
    client = _api(admin)

    checkout = client.post(
        _staff_path("/api/v1/librarian/checkout/", org),
        {
            "card_number": patron.library_card_number,
            "work_slug": work.slug,
            "branch": "main",
        },
        format="json",
        secure=True,
    )
    assert checkout.status_code == 201
    copy.refresh_from_db()
    assert copy.status == CopyStatus.LOANED

    checkin = client.post(
        _staff_path("/api/v1/librarian/checkin/", org),
        {"barcode": copy.barcode},
        format="json",
        secure=True,
    )
    assert checkin.status_code == 200
    assert checkin.json()["data"]["outcome"] == "returned"


def test_copy_move_and_retire():
    org, branch, admin, _ = _staff_setup()
    dest = Branch.objects.create(organization=org, name="West", slug="west")
    work, _edition, copy = _catalog(org, branch)
    client = _api(admin)

    moved = client.post(
        _staff_path("/api/v1/librarian/copies/move/", org),
        {"barcode": copy.barcode, "to_branch": "west", "reason": "rebalance"},
        format="json",
        secure=True,
    )
    assert moved.status_code == 200
    copy.refresh_from_db()
    assert copy.branch_id == dest.pk

    retired = client.post(
        _staff_path("/api/v1/librarian/copies/retire/", org),
        {"barcode": copy.barcode, "reason": "damaged"},
        format="json",
        secure=True,
    )
    assert retired.status_code == 200
    copy.refresh_from_db()
    assert copy.status == CopyStatus.RETIRED


def test_marc_export_xml_and_binary():
    org, branch, admin, _ = _staff_setup()
    _catalog(org, branch, title="Export Me")
    client = _api(admin)

    xml = client.get(_staff_path("/api/v1/librarian/exports/marc/?fmt=xml", org), secure=True)
    assert xml.status_code == 200
    assert b"Export Me" in xml.content

    marc = client.get(_staff_path("/api/v1/librarian/exports/marc/?fmt=marc", org), secure=True)
    assert marc.status_code == 200
    assert marc["Content-Type"] == "application/marc"


def test_circulation_policies_get():
    org, branch, admin, _ = _staff_setup(with_plan=False)
    from library.models import CirculationPolicy

    CirculationPolicy.objects.create(organization=org, loan_days=14)
    resp = _api(admin).get(_staff_path("/api/v1/librarian/policies/", org), secure=True)
    assert resp.status_code == 200
    body = resp.json()
    assert "matrix" in body
    assert len(body["matrix"]) == 1


def test_acquisition_order_create_minimal():
    org, branch, admin, _ = _staff_setup()
    Vendor.objects.create(organization=org, code="vendor", name="Vendor")
    Fund.objects.create(organization=org, code="fund", name="Fund", budget_cents=50000)
    resp = _api(admin).post(
        _staff_path("/api/v1/librarian/acquisitions/orders/", org),
        {"vendor": "vendor", "fund": "fund"},
        format="json",
        secure=True,
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["vendor"] == "vendor"


def test_billing_overview_get():
    org, _branch, admin, plan = _staff_setup()
    Subscription.objects.create(
        organization=org, plan=plan, status=SubscriptionStatus.TRIALING
    )
    resp = _api(admin).get(_staff_path("/api/v1/billing/", org), secure=True)
    assert resp.status_code == 200
    body = resp.json()
    assert body["subscription"] is not None
    assert body["subscription"]["plan"] == plan.slug
    assert "usage" in body


def test_billing_checkout_and_payment_method():
    org, _branch, admin, plan = _staff_setup()
    client = _api(admin)

    checkout = client.post(
        _staff_path("/api/v1/billing/checkout/", org),
        {"plan": plan.slug},
        format="json",
        secure=True,
    )
    assert checkout.status_code == 201
    assert checkout.json()["data"]["token"]

    method = client.post(
        _staff_path("/api/v1/billing/payment-methods/", org),
        {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030},
        format="json",
        secure=True,
    )
    assert method.status_code == 201
    assert method.json()["data"]["last4"] == "4242"


def test_webhook_endpoints_get_and_create():
    org, _branch, admin, _ = _staff_setup()
    client = _api(admin)

    empty = client.get(_staff_path("/api/v1/librarian/webhooks/", org), secure=True)
    assert empty.status_code == 200
    assert empty.json()["data"] == []

    created = client.post(
        _staff_path("/api/v1/librarian/webhooks/", org),
        {"url": "https://hooks.test/max-surface", "event_types": ["loan.borrowed"]},
        format="json",
        secure=True,
    )
    assert created.status_code == 201
    assert created.json()["data"]["secret"]

    listed = client.get(_staff_path("/api/v1/librarian/webhooks/", org), secure=True)
    assert len(listed.json()["data"]) == 1


# --------------------------------------------------------------------------- #
# Support, assistant, consortium, stripe webhook
# --------------------------------------------------------------------------- #
def test_support_patron_lookup():
    org, branch, _admin, _ = _staff_setup(with_plan=False)
    patron = _patron(org, branch)
    support = _support_staff(org, branch)
    resp = _api(support).get(
        f"/api/v1/support/patrons/?org={org.slug}&card={patron.library_card_number}&reason=ticket-42",
        secure=True,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["card"] == patron.library_card_number
    assert data["username"] == patron.user.username


def test_catalog_assist_api():
    org, _branch, admin, _ = _staff_setup()
    resp = _api(admin).post(
        _staff_path("/api/v1/librarian/catalog/assist/", org),
        {"text": "Cyberpunk hackers navigate a dystopian metaverse."},
        format="json",
        secure=True,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["keywords"]


def test_consortium_search_and_availability():
    cons_slug = _slug("cons")
    cons = Consortium.objects.create(name="Network", slug=cons_slug)
    org_a = Organization.objects.create(name="Alpha", slug=_slug("alpha"))
    org_b = Organization.objects.create(name="Beta", slug=_slug("beta"))
    ba = Branch.objects.create(organization=org_a, name="A", slug="a-main")
    bb = Branch.objects.create(organization=org_b, name="B", slug="b-main")
    ConsortiumMembership.objects.create(consortium=cons, organization=org_a)
    ConsortiumMembership.objects.create(consortium=cons, organization=org_b)
    work = Work.objects.create(
        canonical_title="Shared Work",
        slug=_slug("shared"),
        public_status=PublicStatus.PUBLISHED,
    )
    edition = Edition.objects.create(work=work, isbn_13=f"978{uuid.uuid4().hex[:10]}")
    Copy.objects.create(organization=org_b, edition=edition, branch=bb, barcode=f"B-{_slug()}")

    client = _anon()
    search = client.get(
        f"/api/v1/consortium/{cons_slug}/search/?q=Shared", secure=True
    )
    assert search.status_code == 200
    assert any(row["slug"] == work.slug for row in search.json()["data"])

    avail = client.get(
        f"/api/v1/consortium/{cons_slug}/availability/?work={work.slug}", secure=True
    )
    assert avail.status_code == 200
    assert avail.json()["data"]


def test_stripe_webhook_debug_noop_event(settings):
    settings.DEBUG = True
    settings.STRIPE_WEBHOOK_SECRET = ""
    resp = _anon().post(
        "/api/v1/billing/webhook/stripe/",
        {"id": "evt_max_surface_noop", "type": "noop.event"},
        format="json",
        secure=True,
    )
    assert resp.status_code == 200
    assert resp.json()["handled"] is False


# --------------------------------------------------------------------------- #
# Error branches
# --------------------------------------------------------------------------- #
def test_borrow_no_copy_returns_409():
    org, branch, _admin, _ = _staff_setup()
    work, _edition, _copy = _catalog(org, branch, with_copy=False)
    patron = _patron(org, branch)
    resp = _api(patron.user).post(
        f"/api/v1/catalog/works/{work.slug}/borrow/",
        {"branch": "main"},
        format="json",
        secure=True,
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "borrow_blocked"


def test_renew_with_waiting_holds_returns_409():
    org, branch, _admin, _ = _staff_setup()
    work, _edition, copy = _catalog(org, branch)
    borrower = _patron(org, branch, 1)
    waiter = _patron(org, branch, 2)

    loan = borrow_work(patron=borrower, work=work, branch=branch, actor=borrower.user)
    place_hold(patron=waiter, work=work, preferred_branch=branch, actor=waiter.user)
    copy.refresh_from_db()
    assert copy.status == CopyStatus.LOANED

    resp = _api(borrower.user).post(f"/api/v1/loans/{loan.pk}/renew/", secure=True)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "renewal_blocked"


def test_librarian_export_unknown_type_returns_400():
    org, _branch, admin, _ = _staff_setup()
    resp = _api(admin).get(
        _staff_path("/api/v1/librarian/exports/?type=not-a-real-type", org), secure=True
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_export"

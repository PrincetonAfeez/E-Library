"""Focused helper/API coverage: digital, events, notifications, social, selectors, etc."""

from __future__ import annotations

import itertools
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import RequestFactory
from django.utils import timezone
from rest_framework.test import APIClient

from library import channels, consortia, digital, entitlements, events, finance, notifications, reporting, selectors, social, webhooks
from library.context_processors import current_organization
from library.forms import OrganizationSignupForm, PatronRegistrationForm
from library.models import (
    Branch,
    Consortium,
    ConsortiumMembership,
    Copy,
    CopyStatus,
    DigitalHoldStatus,
    DigitalLicense,
    DigitalLoanStatus,
    DomainEvent,
    Edition,
    Event,
    Fund,
    Hold,
    HoldStatus,
    LicenseModel,
    Organization,
    OutboxEvent,
    PatronProfile,
    PaymentPlan,
    PaymentPlanStatus,
    Plan,
    PublicStatus,
    RegistrationStatus,
    ReservationStatus,
    Review,
    Room,
    RoomReservation,
    ScopedApiToken,
    Subject,
    Subscription,
    SubscriptionStatus,
    WebhookDelivery,
    WebhookEndpoint,
    Work,
    stable_patron_hash,
)
from library.services import DomainError, borrow_work

pytestmark = pytest.mark.django_db(transaction=True)

_seq = itertools.count(1)
User = get_user_model()


def _uniq(prefix: str = "x") -> str:
    return f"{prefix}-{next(_seq)}"


def _api(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


def _make_digital(concurrent=1, model=LicenseModel.ONE_COPY_ONE_USER, **kw):
    org = Organization.objects.create(name=f"Dig {_seq}", slug=_uniq("dig"))
    branch = Branch.objects.create(organization=org, name="Main", slug=_uniq("main"))
    work = Work.objects.create(canonical_title="E-Book", slug=_uniq("ebook"))
    edition = Edition.objects.create(work=work, isbn_13=f"978{next(_seq):010d}", format="ebook")
    lic = DigitalLicense.objects.create(
        organization=org,
        edition=edition,
        license_model=model,
        concurrent_limit=concurrent,
        content_url="https://cdn/example.epub",
        loan_period_days=21,
        **kw,
    )
    return org, branch, work, edition, lic


def _patron(org, branch, n=None):
    n = n or next(_seq)
    user = User.objects.create_user(username=_uniq("reader"), email=f"r{n}@example.test")
    return PatronProfile.objects.create(
        user=user, organization=org, library_card_number=f"C{n}", home_branch=branch
    )


def _catalog(**kw):
    org = Organization.objects.create(name=f"Cat {_seq}", slug=_uniq("cat"))
    branch = Branch.objects.create(organization=org, name="Main", slug=_uniq("br"))
    work = Work.objects.create(
        canonical_title=kw.get("title", "Title"),
        slug=kw.get("slug", _uniq("work")),
        public_status=PublicStatus.PUBLISHED,
    )
    edition = Edition.objects.create(
        work=work,
        isbn_13=kw.get("isbn", f"978{next(_seq):010d}"),
        public_status=PublicStatus.PUBLISHED,
    )
    copy = Copy.objects.create(
        organization=org,
        edition=edition,
        branch=branch,
        barcode=kw.get("barcode", f"B{next(_seq)}"),
        status=kw.get("copy_status", CopyStatus.AVAILABLE),
        public_visible=True,
    )
    return org, branch, work, edition, copy


# --------------------------------------------------------------------------- #
# Digital
# --------------------------------------------------------------------------- #
def test_cancel_digital_hold_waiting():
    org, branch, work, edition, lic = _make_digital(concurrent=1)
    p1 = _patron(org, branch)
    p2 = _patron(org, branch)
    digital.borrow_digital(patron=p1, edition=edition, actor=p1.user)
    hold = digital.place_digital_hold(patron=p2, edition=edition, actor=p2.user)
    assert hold.status == DigitalHoldStatus.WAITING
    digital.cancel_digital_hold(hold=hold, actor=p2.user)
    hold.refresh_from_db()
    assert hold.status == DigitalHoldStatus.CANCELLED


def test_cancel_digital_hold_ready_promotes_next():
    org, branch, work, edition, lic = _make_digital(concurrent=1)
    p1, p2, p3 = _patron(org, branch), _patron(org, branch), _patron(org, branch)
    loan = digital.borrow_digital(patron=p1, edition=edition, actor=p1.user)
    h2 = digital.place_digital_hold(patron=p2, edition=edition, actor=p2.user)
    h3 = digital.place_digital_hold(patron=p3, edition=edition, actor=p3.user)
    digital.return_digital(loan=loan, actor=p1.user)
    h2.refresh_from_db()
    assert h2.status == DigitalHoldStatus.READY
    digital.cancel_digital_hold(hold=h2, actor=p2.user)
    h3.refresh_from_db()
    assert h3.status == DigitalHoldStatus.READY


def test_expire_digital_ready_holds_backdated():
    org, branch, work, edition, lic = _make_digital(concurrent=1)
    p1, p2 = _patron(org, branch), _patron(org, branch)
    loan = digital.borrow_digital(patron=p1, edition=edition, actor=p1.user)
    hold = digital.place_digital_hold(patron=p2, edition=edition, actor=p2.user)
    digital.return_digital(loan=loan, actor=p1.user)
    hold.refresh_from_db()
    past = timezone.now() - timedelta(hours=1)
    hold.__class__.objects.filter(pk=hold.pk).update(expires_at=past)
    assert digital.expire_digital_ready_holds(now=timezone.now()) == 1
    hold.refresh_from_db()
    assert hold.status == DigitalHoldStatus.EXPIRED


def test_license_is_available_inactive():
    org, branch, work, edition, lic = _make_digital(concurrent=1)
    lic.active = False
    lic.save(update_fields=["active"])
    assert digital.license_is_available(lic) is False


def test_license_is_available_metered_time_expired():
    org, branch, work, edition, lic = _make_digital(
        concurrent=1, model=LicenseModel.METERED_TIME, expires_at=timezone.now() - timedelta(days=1)
    )
    assert digital.license_is_available(lic) is False


def test_license_is_available_metered_checkouts_depleted():
    org, branch, work, edition, lic = _make_digital(
        concurrent=1, model=LicenseModel.METERED_CHECKOUTS, checkouts_allowed=1, checkouts_used=1
    )
    assert digital.license_is_available(lic) is False


def test_license_is_available_simultaneous_unlimited():
    org, branch, work, edition, lic = _make_digital(concurrent=None, model=LicenseModel.SIMULTANEOUS)
    assert digital.license_is_available(lic) is True


def test_license_is_available_concurrent_full():
    org, branch, work, edition, lic = _make_digital(concurrent=1)
    p1 = _patron(org, branch)
    digital.borrow_digital(patron=p1, edition=edition, actor=p1.user)
    assert digital.license_is_available(lic) is False


def test_place_and_cancel_digital_hold_api():
    org, branch, work, edition, lic = _make_digital(concurrent=1)
    p1, p2 = _patron(org, branch), _patron(org, branch)
    client = _api(p2.user)
    digital.borrow_digital(patron=p1, edition=edition, actor=p1.user)
    resp = client.post(f"/api/v1/digital/editions/{edition.pk}/hold/", {}, format="json", secure=True)
    assert resp.status_code == 201
    hold_id = resp.json()["data"]["id"]
    resp = client.post(f"/api/v1/digital/holds/{hold_id}/cancel/", {}, format="json", secure=True)
    assert resp.status_code == 204


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
def test_cancel_reservation_booked():
    org, branch, work, edition, copy = _catalog()
    room = Room.objects.create(organization=org, branch=branch, code="A", name="Study")
    patron = _patron(org, branch)
    start = timezone.now() + timedelta(days=1)
    resv = events.reserve_room(
        patron=patron, room=room, starts_at=start, ends_at=start + timedelta(hours=2)
    )
    events.cancel_reservation(reservation=resv, actor=patron.user)
    resv.refresh_from_db()
    assert resv.status == ReservationStatus.CANCELLED


def test_cancel_reservation_rejects_non_booked():
    org, branch, work, edition, copy = _catalog()
    room = Room.objects.create(organization=org, branch=branch, code="B", name="B")
    patron = _patron(org, branch)
    start = timezone.now() + timedelta(days=1)
    resv = events.reserve_room(
        patron=patron, room=room, starts_at=start, ends_at=start + timedelta(hours=1)
    )
    events.cancel_reservation(reservation=resv)
    with pytest.raises(DomainError):
        events.cancel_reservation(reservation=resv)


def test_room_availability_filters_window():
    org, branch, work, edition, copy = _catalog()
    room = Room.objects.create(organization=org, branch=branch, code="C", name="C")
    patron = _patron(org, branch)
    start = timezone.now() + timedelta(days=2)
    events.reserve_room(
        patron=patron, room=room, starts_at=start, ends_at=start + timedelta(hours=2)
    )
    qs = events.room_availability(room, start=start, end=start + timedelta(days=3))
    assert qs.count() == 1
    qs_far = events.room_availability(room, start=start + timedelta(days=10))
    assert qs_far.count() == 0


def test_upcoming_events_public_future():
    org, branch, work, edition, copy = _catalog()
    future = timezone.now() + timedelta(days=5)
    past = timezone.now() - timedelta(days=5)
    Event.objects.create(
        organization=org, title="Future", starts_at=future, ends_at=future + timedelta(hours=1)
    )
    Event.objects.create(
        organization=org, title="Past", starts_at=past, ends_at=past + timedelta(hours=1)
    )
    Event.objects.create(
        organization=org,
        title="Private",
        starts_at=future,
        ends_at=future + timedelta(hours=1),
        public=False,
    )
    titles = list(events.upcoming_events(org).values_list("title", flat=True))
    assert titles == ["Future"]


def test_cancel_registration_promotes_waitlist():
    org, branch, work, edition, copy = _catalog()
    start = timezone.now() + timedelta(days=3)
    event = Event.objects.create(
        organization=org, title="Talk", starts_at=start, ends_at=start + timedelta(hours=1), capacity=1
    )
    p1, p2 = _patron(org, branch), _patron(org, branch)
    r1 = events.register_for_event(patron=p1, event=event)
    r2 = events.register_for_event(patron=p2, event=event)
    assert r2.status == RegistrationStatus.WAITLISTED
    events.cancel_registration(registration=r1)
    r2.refresh_from_db()
    assert r2.status == RegistrationStatus.REGISTERED


def test_room_reserve_api_post():
    org, branch, work, edition, copy = _catalog()
    room = Room.objects.create(organization=org, branch=branch, code="R1", name="Room 1")
    patron = _patron(org, branch)
    start = timezone.now() + timedelta(days=1)
    end = start + timedelta(hours=2)
    resp = _api(patron.user).post(
        f"/api/v1/rooms/{room.pk}/reserve/",
        {"starts_at": start.isoformat(), "ends_at": end.isoformat(), "purpose": "Study"},
        format="json",
        secure=True,
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["status"] == ReservationStatus.BOOKED


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def test_category_for_event_defaults():
    assert notifications.category_for_event("loan.borrowed") == "courtesy"
    assert notifications.category_for_event("hold.ready") == "holds"
    assert notifications.category_for_event("unknown.event") == "courtesy"


def test_category_allowed_essential_always():
    org = Organization.objects.create(name="N", slug=_uniq("n"))
    branch = Branch.objects.create(organization=org, name="M", slug=_uniq("m"))
    patron = _patron(org, branch)
    patron.unsubscribed_at = timezone.now()
    patron.notification_prefs = {"courtesy": False}
    patron.save(update_fields=["unsubscribed_at", "notification_prefs"])
    assert notifications.category_allowed(patron, "holds") is True
    assert notifications.category_allowed(patron, "courtesy") is False


def test_patron_channels_and_get_channel():
    org, branch, work, edition, copy = _catalog()
    patron = _patron(org, branch)
    patron.notification_channels = ["sms", "bogus"]
    patron.sms_number = "+15551234567"
    patron.save(update_fields=["notification_channels", "sms_number"])
    keys = channels.patron_channels(patron)
    assert keys == ["sms"]
    assert channels.get_channel("email").key == "email"
    assert channels.get_channel("nope") is None


def test_unsubscribe_url(settings):
    settings.SITE_BASE_URL = "https://library.test"
    org, branch, work, edition, copy = _catalog()
    patron = _patron(org, branch)
    url = notifications.unsubscribe_url(patron)
    assert url.startswith("https://library.test/u/")
    patron.refresh_from_db()
    assert patron.unsubscribe_token


# --------------------------------------------------------------------------- #
# Social
# --------------------------------------------------------------------------- #
def test_delete_review():
    org, branch, work, edition, copy = _catalog()
    patron = _patron(org, branch)
    review = social.submit_review(patron=patron, work=work, rating=4, body="Nice")
    social.delete_review(review=review)
    assert not Review.objects.filter(pk=review.pk).exists()


def test_remove_from_list():
    org, branch, work, edition, copy = _catalog()
    patron = _patron(org, branch)
    rl = social.create_reading_list(patron=patron, name="Favorites")
    social.add_to_list(reading_list=rl, work=work)
    assert rl.works.filter(pk=work.pk).exists()
    social.remove_from_list(reading_list=rl, work=work)
    assert not rl.works.filter(pk=work.pk).exists()


def test_work_reviews_public_only():
    org, branch, work, edition, copy = _catalog()
    p1, p2 = _patron(org, branch), _patron(org, branch)
    social.submit_review(patron=p1, work=work, rating=5, body="Public")
    r2 = social.submit_review(patron=p2, work=work, rating=3)
    r2.public = False
    r2.save(update_fields=["public"])
    reviews = list(social.work_reviews(work))
    assert len(reviews) == 1
    assert reviews[0].rating == 5


# --------------------------------------------------------------------------- #
# Selectors
# --------------------------------------------------------------------------- #
def test_get_work_detail_and_availability():
    org, branch, work, edition, copy = _catalog(slug=_uniq("detail"))
    detail = selectors.get_work_detail(org, work.slug)
    assert detail.pk == work.pk
    avail = selectors.availability_for_work(org, work)
    assert avail["available"] == 1
    assert avail["total"] == 1


def test_get_patron_loans_active():
    org, branch, work, edition, copy = _catalog()
    patron = _patron(org, branch)
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    loans = list(selectors.get_patron_loans(patron))
    assert len(loans) == 1


def test_apply_catalog_filters_and_facets():
    org, branch, work, edition, copy = _catalog(slug=_uniq("facet"))
    subj = Subject.objects.create(name="Sci-Fi", slug=_uniq("scifi"))
    work.subjects.add(subj)
    qs = selectors.base_visible_works(org)
    filtered = selectors.apply_catalog_filters(qs, org, {"availability": "available"})
    assert work in list(filtered)
    facets = selectors.get_facets_for_query(org, "", {})
    assert any(f["slug"] == subj.slug for f in facets["subjects"])


# --------------------------------------------------------------------------- #
# Consortia
# --------------------------------------------------------------------------- #
def test_member_org_ids_and_union_search():
    cons = Consortium.objects.create(name="Net", slug=_uniq("cons"))
    org_a = Organization.objects.create(name="A", slug=_uniq("a"))
    org_b = Organization.objects.create(name="B", slug=_uniq("b"))
    ba = Branch.objects.create(organization=org_a, name="A", slug=_uniq("ba"))
    bb = Branch.objects.create(organization=org_b, name="B", slug=_uniq("bb"))
    ConsortiumMembership.objects.create(consortium=cons, organization=org_a)
    ConsortiumMembership.objects.create(consortium=cons, organization=org_b)
    work = Work.objects.create(canonical_title="Shared", slug=_uniq("shared"))
    edition = Edition.objects.create(work=work, isbn_13=f"978{next(_seq):010d}")
    Copy.objects.create(organization=org_b, edition=edition, branch=bb, barcode=f"X{next(_seq)}")
    ids = consortia.member_org_ids(cons)
    assert org_a.pk in ids and org_b.pk in ids
    results = consortia.union_search(cons, "Shared")
    assert any(w.pk == work.pk for w in results)


# --------------------------------------------------------------------------- #
# Finance encumbrance (direct)
# --------------------------------------------------------------------------- #
def test_encumber_release_and_spend():
    org = Organization.objects.create(name="Fin", slug=_uniq("fin"))
    fund = Fund.objects.create(organization=org, code="G", name="General", budget_cents=10000)
    with transaction.atomic():
        finance.encumber(fund=fund, amount_cents=3000)
    fund.refresh_from_db()
    assert fund.encumbered_cents == 3000
    assert fund.available_cents == 7000
    with transaction.atomic():
        finance.spend_encumbered(fund=fund, amount_cents=1000)
    fund.refresh_from_db()
    assert fund.spent_cents == 1000 and fund.encumbered_cents == 2000
    with transaction.atomic():
        finance.release_encumbrance(fund=fund, amount_cents=2000)
    fund.refresh_from_db()
    assert fund.encumbered_cents == 0
    assert fund.remaining_cents == 9000


def test_encumber_insufficient_raises():
    org = Organization.objects.create(name="Poor", slug=_uniq("poor"))
    fund = Fund.objects.create(organization=org, code="Z", name="Zero", budget_cents=500)
    with transaction.atomic():
        with pytest.raises(DomainError):
            finance.encumber(fund=fund, amount_cents=600)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def test_holds_stats_and_branch_activity():
    org, branch, work, edition, copy = _catalog()
    patron = _patron(org, branch)
    Hold.objects.create(
        organization=org,
        work=work,
        patron=patron,
        preferred_branch=branch,
        status=HoldStatus.WAITING,
    )
    hs = reporting.holds_stats(org, *reporting.default_window(30))
    assert hs["waiting_now"] == 1
    assert hs["placed"] >= 1

    org2, branch2, work2, edition2, copy2 = _catalog()
    patron2 = _patron(org2, branch2)
    borrow_work(patron=patron2, work=work2, branch=branch2, actor=patron2.user)
    start, end = reporting.default_window(30)
    branches = reporting.branch_activity(org2, start, end)
    assert branches[0]["loans"] == 1


def test_build_report_and_dashboard_report():
    org, branch, work, edition, copy = _catalog()
    start, end = reporting.default_window(7)
    assert reporting.build_report("holds", org, start, end) is not None
    assert reporting.build_report("nope", org, start, end) is None
    dash = reporting.dashboard_report(org, days=7)
    assert "circulation" in dash and "holds" in dash


# --------------------------------------------------------------------------- #
# Entitlements
# --------------------------------------------------------------------------- #
def test_assert_feature_raises_entitlement_error():
    org, branch, work, edition, copy = _catalog()
    plan = Plan.objects.create(
        slug=_uniq("basic"), name="Basic", features=["catalog"], active=True, public=True
    )
    Subscription.objects.create(organization=org, plan=plan, status=SubscriptionStatus.ACTIVE)
    with pytest.raises(entitlements.EntitlementError, match="digital"):
        entitlements.assert_feature(org, "digital")


# --------------------------------------------------------------------------- #
# Forms
# --------------------------------------------------------------------------- #
def test_organization_signup_form_duplicate_slug():
    slug = _uniq("taken")
    Organization.objects.create(name="Existing", slug=slug)
    form = OrganizationSignupForm(
        data={
            "username": _uniq("owner"),
            "password1": "long-enough-pass-123",
            "password2": "long-enough-pass-123",
            "organization_name": "New Lib",
            "organization_slug": slug,
            "email": f"o{next(_seq)}@example.test",
        }
    )
    assert not form.is_valid()
    assert "organization_slug" in form.errors


def test_patron_registration_form_branch_mismatch():
    org_a = Organization.objects.create(name="A", slug=_uniq("oa"))
    org_b = Organization.objects.create(name="B", slug=_uniq("ob"))
    ba = Branch.objects.create(organization=org_a, name="A", slug=_uniq("ba"))
    Branch.objects.create(organization=org_b, name="B", slug=_uniq("bb"))
    form = PatronRegistrationForm(
        data={
            "username": _uniq("patron"),
            "password1": "long-enough-pass-123",
            "password2": "long-enough-pass-123",
            "email": f"p{next(_seq)}@example.test",
            "organization": org_b.pk,
            "home_branch": ba.pk,
        },
        require_org=True,
    )
    assert not form.is_valid()
    assert "home_branch" in form.errors


def test_patron_registration_form_duplicate_email():
    org = Organization.objects.create(name="Lib", slug=_uniq("reg"))
    branch = Branch.objects.create(organization=org, name="M", slug=_uniq("rm"))
    User.objects.create_user(username=_uniq("existing"), email="dup@example.test")
    form = PatronRegistrationForm(
        data={
            "username": _uniq("new"),
            "password1": "long-enough-pass-123",
            "password2": "long-enough-pass-123",
            "email": "dup@example.test",
            "home_branch": branch.pk,
        },
        organization=org,
    )
    assert not form.is_valid()
    assert "email" in form.errors


# --------------------------------------------------------------------------- #
# Context processor
# --------------------------------------------------------------------------- #
def test_current_organization_context_processor():
    org = Organization.objects.create(name="Ctx", slug=_uniq("ctx"))
    Branch.objects.create(organization=org, name="M", slug=_uniq("cm"))
    patron = _patron(org, Branch.objects.get(organization=org))
    request = RequestFactory().get(f"/?org={org.slug}")
    request.user = patron.user
    request.session = {}
    ctx = current_organization(request)
    assert ctx["current_organization"].pk == org.pk


# --------------------------------------------------------------------------- #
# ScopedApiToken
# --------------------------------------------------------------------------- #
def test_scoped_api_token_issue_verify_mark_used():
    org = Organization.objects.create(name="Tok", slug=_uniq("tok"))
    user = User.objects.create_user(username=_uniq("apiuser"))
    raw, token = ScopedApiToken.issue(user=user, organization=org, name="cli", scopes=["patron:read"])
    assert token.verify(raw) is True
    assert token.verify("wrong-key") is False
    assert token.last_used_at is None
    token.mark_used()
    token.refresh_from_db()
    assert token.last_used_at is not None


def test_scoped_api_token_revoked_not_verified():
    org = Organization.objects.create(name="Rev", slug=_uniq("rev"))
    user = User.objects.create_user(username=_uniq("revuser"))
    raw, token = ScopedApiToken.issue(user=user, organization=org, name="x", scopes=["*"])
    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])
    assert token.verify(raw) is False


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #
def test_webhook_endpoint_matches():
    org = Organization.objects.create(name="Hook", slug=_uniq("hook"))
    ep_all = WebhookEndpoint.objects.create(organization=org, url="https://h.test/a", event_types=["*"])
    ep_loan = WebhookEndpoint.objects.create(
        organization=org, url="https://h.test/b", event_types=["loan.borrowed"]
    )
    assert ep_all.matches("hold.ready") is True
    assert ep_loan.matches("loan.borrowed") is True
    assert ep_loan.matches("hold.ready") is False


def test_enqueue_for_outbox_event():
    org = Organization.objects.create(name="Out", slug=_uniq("out"))
    ep = WebhookEndpoint.objects.create(
        organization=org, url="https://h.test/c", event_types=["loan.returned"]
    )
    domain = DomainEvent.objects.create(
        organization=org,
        event_type="loan.returned",
        aggregate_type="Loan",
        aggregate_id="1",
        payload={"foo": "bar"},
    )
    outbox = OutboxEvent.objects.create(
        organization=org,
        event_type="loan.returned",
        payload={"domain_event_id": domain.pk},
    )
    created = webhooks.enqueue_for_outbox_event(outbox)
    assert created == 1
    assert WebhookDelivery.objects.filter(endpoint=ep, outbox_event_id=outbox.pk).exists()


# --------------------------------------------------------------------------- #
# Model helpers
# --------------------------------------------------------------------------- #
def test_fund_remaining_and_available_cents():
    org = Organization.objects.create(name="Fund", slug=_uniq("fund"))
    fund = Fund.objects.create(
        organization=org, code="F1", name="F1", budget_cents=10000, spent_cents=2000, encumbered_cents=3000
    )
    assert fund.remaining_cents == 8000
    assert fund.available_cents == 5000


def test_subscription_is_serviceable():
    org = Organization.objects.create(name="Sub", slug=_uniq("sub"))
    plan = Plan.objects.create(slug=_uniq("p"), name="P", active=True)
    sub = Subscription.objects.create(organization=org, plan=plan, status=SubscriptionStatus.ACTIVE)
    assert sub.is_serviceable is True
    sub.status = SubscriptionStatus.CANCELED
    sub.save(update_fields=["status"])
    assert sub.is_serviceable is False
    sub.status = SubscriptionStatus.PAST_DUE
    sub.grace_until = timezone.now() + timedelta(days=1)
    sub.save(update_fields=["status", "grace_until"])
    assert sub.is_serviceable is True


def test_stable_patron_hash_deterministic():
    org, branch, work, edition, copy = _catalog()
    patron = _patron(org, branch)
    h1 = stable_patron_hash(patron)
    h2 = stable_patron_hash(patron)
    assert h1 == h2 and len(h1) == 64
    assert stable_patron_hash(None) == ""


def test_payment_plan_remaining_cents():
    org = Organization.objects.create(name="Plan", slug=_uniq("plan"))
    plan = PaymentPlan.objects.create(
        organization=org,
        total_cents=5000,
        installment_cents=1000,
        status=PaymentPlanStatus.ACTIVE,
        paid_cents=1500,
    )
    assert plan.remaining_cents == 3500

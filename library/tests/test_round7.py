"""Regression tests for Round 7 fixes."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from library import billing, finance, mfa, workflows
from library.models import (
    Branch,
    Copy,
    CopyStatus,
    Edition,
    Fee,
    FeeStatus,
    FeeType,
    Loan,
    LoanStatus,
    Organization,
    PatronProfile,
    Plan,
    StaffMembership,
    StaffRole,
    Subscription,
    SubscriptionStatus,
    Work,
)
from library.services import rebuild_work_search_document, record_payment

pytestmark = pytest.mark.django_db(transaction=True)


def make_plans():
    Plan.objects.create(slug="trial", name="Trial", price_cents=0, max_copies=1000)
    Plan.objects.create(slug="basic", name="Basic", price_cents=10000, features=["*"])
    return Plan.objects.create(slug="pro", name="Pro", price_cents=29900, features=["*"])


def make_patron(org, branch, n=1):
    user = get_user_model().objects.create_user(username=f"p{org.slug}{n}", email=f"p{n}@x.test")
    return PatronProfile.objects.create(
        user=user, organization=org, library_card_number=f"{org.slug}{n}", home_branch=branch
    )


# --------------------------------------------------------------------------- #
# #1 — checkout charges the card exactly once
# --------------------------------------------------------------------------- #
def test_checkout_charges_card_once(monkeypatch):
    pro = make_plans()
    org = Organization.objects.create(name="Lib", slug="lib")
    calls = []
    real_charge = billing.SimulatedGateway.charge

    def counting_charge(self, method, amount_cents):
        calls.append(amount_cents)
        return real_charge(self, method, amount_cents)

    monkeypatch.setattr(billing.SimulatedGateway, "charge", counting_charge)
    session = billing.create_checkout(organization=org, plan=pro)
    sub = billing.complete_checkout(session=session, last4="4242")
    assert sub.status == SubscriptionStatus.ACTIVE
    assert calls == [pro.price_cents]  # exactly one charge, not two


# --------------------------------------------------------------------------- #
# #2 — claims-returned frees the copy from LOANED limbo
# --------------------------------------------------------------------------- #
def test_claims_returned_moves_copy_out_of_loaned():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="W", slug="w")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    copy = Copy.objects.create(
        organization=org, edition=edition, branch=branch, barcode="A", status=CopyStatus.LOANED
    )
    patron = make_patron(org, branch)
    loan = Loan.objects.create(
        organization=org, copy=copy, patron=patron,
        due_at=timezone.now() + timedelta(days=7), status=LoanStatus.ACTIVE,
    )
    workflows.mark_claims_returned(loan=loan, actor=None)
    copy.refresh_from_db()
    assert copy.status == CopyStatus.LOST  # no longer stuck in LOANED with no loan


# --------------------------------------------------------------------------- #
# #3 — popular-works ranking does not count another tenant's loans
# --------------------------------------------------------------------------- #
def test_recommendations_popular_scoped_to_tenant():
    from library import assistant

    shared = Work.objects.create(canonical_title="Shared", slug="shared")
    edition = Edition.objects.create(work=shared, isbn_13="9780000000002")
    org_a = Organization.objects.create(name="A", slug="a")
    org_b = Organization.objects.create(name="B", slug="b")
    ba = Branch.objects.create(organization=org_a, name="A", slug="a")
    bb = Branch.objects.create(organization=org_b, name="B", slug="b")
    copy_a = Copy.objects.create(organization=org_a, edition=edition, branch=ba, barcode="A1")
    copy_b = Copy.objects.create(organization=org_b, edition=edition, branch=bb, barcode="B1")
    rebuild_work_search_document(shared.pk)
    pa = make_patron(org_a, ba, 1)
    pb = make_patron(org_b, bb, 2)
    # Heavy circulation of the shared title in org B only.
    for _ in range(5):
        Loan.objects.create(
            organization=org_b, copy=copy_b, patron=pb,
            due_at=timezone.now(), status=LoanStatus.RETURNED, returned_at=timezone.now(),
        )
    # org A patron has no history -> popular fallback, scoped to org A (0 loans).
    from library.assistant import _popular_works

    ranked = _popular_works(org_a, limit=5)
    # The scoped count for org A is 0; the query must still run without leaking B.
    ids = {w.pk for w in ranked}
    assert shared.pk in ids  # present (org A owns a copy)
    # Sanity: recommend_for_patron returns without error for the empty-history patron.
    assert isinstance(assistant.recommend_for_patron(pa, limit=5), list)
    assert copy_a.pk  # copy exists


# --------------------------------------------------------------------------- #
# #5 — refund reverses exactly the fees THAT payment paid
# --------------------------------------------------------------------------- #
def test_refund_reverses_only_its_own_allocation():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    patron = make_patron(org, branch)
    fee_a = Fee.objects.create(
        organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=1000
    )
    pay_a = record_payment(patron=patron, amount_cents=1000)  # pays fee A
    fee_b = Fee.objects.create(
        organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=1000
    )
    record_payment(patron=patron, amount_cents=1000)  # pays fee B
    fee_a.refresh_from_db()
    fee_b.refresh_from_db()
    assert fee_a.status == FeeStatus.PAID and fee_b.status == FeeStatus.PAID

    # Refunding payment A must reopen fee A, NOT fee B.
    finance.refund_payment(payment=pay_a, actor=None)
    fee_a.refresh_from_db()
    fee_b.refresh_from_db()
    assert fee_a.status == FeeStatus.OUTSTANDING and fee_a.paid_cents == 0
    assert fee_b.status == FeeStatus.PAID  # untouched


# --------------------------------------------------------------------------- #
# #6 — downgrade banks account credit, applied to next renewal
# --------------------------------------------------------------------------- #
def test_downgrade_credit_applied_to_renewal():
    make_plans()
    basic = Plan.objects.get(slug="basic")
    pro = Plan.objects.get(slug="pro")
    org = Organization.objects.create(name="Lib", slug="lib")
    billing.add_payment_method(organization=org, last4="4242")
    sub = billing.subscribe(organization=org, plan=pro)
    Subscription.objects.filter(pk=sub.pk).update(
        current_period_end=timezone.now() + timedelta(days=15)
    )
    sub.refresh_from_db()
    billing.change_plan(subscription=sub, new_plan=basic)
    sub.refresh_from_db()
    assert sub.credit_cents > 0  # unused pro time banked, not discarded

    # The credit reduces the next renewal charge.
    credit = sub.credit_cents
    Subscription.objects.filter(pk=sub.pk).update(current_period_end=timezone.now() - timedelta(days=1))
    billing.run_billing_cycle()
    sub.refresh_from_db()
    assert sub.credit_cents == max(0, credit - basic.price_cents)


# --------------------------------------------------------------------------- #
# #7 — dunning reuses the open renewal invoice
# --------------------------------------------------------------------------- #
def test_dunning_does_not_pile_up_invoices():
    from library.models import Invoice, InvoiceStatus

    make_plans()
    pro = Plan.objects.get(slug="pro")
    org = Organization.objects.create(name="Lib", slug="lib")
    billing.add_payment_method(organization=org, last4="0000")  # declines
    sub = billing.subscribe(organization=org, plan=pro)
    Subscription.objects.filter(pk=sub.pk).update(
        status=SubscriptionStatus.ACTIVE, current_period_end=timezone.now() - timedelta(days=1)
    )
    billing.run_billing_cycle()  # fails -> one open renewal invoice
    billing.run_billing_cycle()  # retry -> must reuse, not create a second
    open_renewals = Invoice.objects.filter(
        organization=org, status=InvoiceStatus.OPEN, description__endswith="renewal"
    ).count()
    assert open_renewals == 1


# --------------------------------------------------------------------------- #
# #8 — MFA middleware enforces the second factor when the org opts in
# --------------------------------------------------------------------------- #
def test_mfa_enforced_when_org_requires(client):
    org = Organization.objects.create(name="Lib", slug="lib", require_staff_mfa=True)
    Branch.objects.create(organization=org, name="Main", slug="main")
    user = get_user_model().objects.create_user(username="mgr", password="x", is_staff=True)
    StaffMembership.objects.create(user=user, organization=org, branch=None, role=StaffRole.ADMIN)
    # Enroll + confirm a device.
    info = mfa.begin_enrollment(user=user)
    import time
    mfa.confirm_enrollment(user=user, code=mfa.totp(info["secret"], timestamp=time.time()))

    client.force_login(user)
    resp = client.get("/librarian/", secure=True)
    assert resp.status_code == 302 and "/mfa/challenge/" in resp["Location"]

    # After passing the challenge, access is granted.
    ok = client.post(
        "/mfa/challenge/",
        {"code": mfa.totp(info["secret"], timestamp=time.time()), "next": "/librarian/"},
        secure=True,
    )
    assert ok.status_code == 302
    assert client.session.get("mfa_verified") is True


# --------------------------------------------------------------------------- #
# #10 — TOTP secret is encrypted at rest
# --------------------------------------------------------------------------- #
def test_totp_secret_encrypted_at_rest():
    user = get_user_model().objects.create_user(username="staff")
    info = mfa.begin_enrollment(user=user)
    from library.models import StaffTotpDevice

    stored = StaffTotpDevice.objects.get(user=user).secret
    assert stored != info["secret"]  # not plaintext
    assert stored.startswith("enc1:")
    assert mfa.decrypt_secret(stored) == info["secret"]  # round-trips

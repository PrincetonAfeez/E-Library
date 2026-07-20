"""Round-5 regressions for auth, tenancy, billing, privacy, and protocol hardening."""

import time

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from library import billing, mfa, privacy, sip2
from library.models import (
    Branch,
    Fee,
    FeeType,
    Organization,
    PatronProfile,
    Plan,
    StaffMembership,
    StaffRole,
    SubscriptionStatus,
)
from library.services import DomainError, waive_fee
from library.tenancy import get_current_organization

pytestmark = pytest.mark.django_db(transaction=True)


def _patron(org, user=None):
    branch, _ = Branch.objects.get_or_create(
        organization=org, slug="main", defaults={"name": "Main"}
    )
    user = user or get_user_model().objects.create_user(
        username=f"user-{org.pk}-{PatronProfile.objects.count()}"
    )
    return PatronProfile.objects.create(
        user=user,
        organization=org,
        library_card_number=f"C{org.pk}-{user.pk}",
        home_branch=branch,
    )


def test_session_org_is_ignored_for_authenticated_non_member():
    member_org = Organization.objects.create(name="Member", slug="member")
    foreign_org = Organization.objects.create(name="Foreign", slug="foreign")
    user = get_user_model().objects.create_user(username="reader")
    PatronProfile.objects.create(user=user, organization=member_org, library_card_number="P1")
    request = RequestFactory().get("/")
    request.user = user
    request.session = {"organization_slug": foreign_org.slug}

    assert get_current_organization(request) == member_org
    assert "organization_slug" not in request.session


def test_staff_mfa_uses_staff_org_when_session_selects_patron_org(client):
    patron_org = Organization.objects.create(name="Patron", slug="patron")
    staff_org = Organization.objects.create(
        name="Staff", slug="staff", require_staff_mfa=True
    )
    user = get_user_model().objects.create_user(username="dual-role", password="x", is_staff=True)
    PatronProfile.objects.create(user=user, organization=patron_org, library_card_number="P1")
    StaffMembership.objects.create(user=user, organization=staff_org, role=StaffRole.ADMIN)
    info = mfa.begin_enrollment(user=user)
    mfa.confirm_enrollment(user=user, code=mfa.totp(info["secret"], timestamp=time.time()))

    client.force_login(user)
    session = client.session
    session["organization_slug"] = patron_org.slug
    session.save()

    response = client.get("/librarian/", secure=True)

    assert response.status_code == 302
    assert "/mfa/challenge/" in response["Location"]


def test_change_plan_rejects_canceled_subscription():
    org = Organization.objects.create(name="Lib", slug="r15-lib")
    old = Plan.objects.create(slug="r15-old", name="Old", price_cents=100)
    new = Plan.objects.create(slug="r15-new", name="New", price_cents=200)
    billing.add_payment_method(organization=org, purpose="saas")
    subscription = billing.subscribe(organization=org, plan=old)
    billing.cancel_subscription(subscription=subscription)
    with pytest.raises(billing.BillingError, match="subscribe"):
        billing.change_plan(subscription=subscription, new_plan=new)
    subscription.refresh_from_db()
    assert subscription.status == SubscriptionStatus.CANCELED


def test_waive_rejects_partially_paid_fee():
    org = Organization.objects.create(name="Lib", slug="r15-fees")
    patron = _patron(org)
    fee = Fee.objects.create(
        organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=100, paid_cents=25
    )
    with pytest.raises(DomainError, match="Refund or reverse"):
        waive_fee(fee=fee)


def test_erase_keeps_other_organization_staff_membership():
    org = Organization.objects.create(name="One", slug="r15-one")
    other = Organization.objects.create(name="Two", slug="r15-two")
    user = get_user_model().objects.create_user(username="multi", email="multi@example.test")
    patron = _patron(org, user)
    membership = StaffMembership.objects.create(
        user=user, organization=other, role=StaffRole.ADMIN, active=True
    )
    privacy.erase_patron(patron=patron)
    membership.refresh_from_db()
    user.refresh_from_db()
    assert membership.active is True
    assert user.is_active is True
    assert user.email == "multi@example.test"


def test_sip2_login_length_mismatch_denies_without_error():
    org = Organization.objects.create(
        name="SIP", slug="r15-sip", sip2_login_user="terminal", sip2_login_password="secret"
    )
    assert sip2.handle_message("93CNx|COx|", organization=org) == "940"


def test_fines_pm_not_used_for_saas_subscribe():
    org = Organization.objects.create(name="Lib", slug="r15-fines-only")
    plan = Plan.objects.create(slug="pro", name="Pro", price_cents=500)
    billing.add_payment_method(organization=org, purpose="fines", last4="4242")
    with pytest.raises(billing.BillingError, match="card is required"):
        billing.subscribe(organization=org, plan=plan)


def test_charge_online_requires_fines_purpose_pm():
    org = Organization.objects.create(name="Lib", slug="r15-saas-only")
    billing.add_payment_method(organization=org, purpose="saas", last4="4242")
    with pytest.raises(billing.BillingError, match="fines"):
        billing.charge_online_amount(organization=org, amount_cents=100)

"""Maximize coverage for library.privacy and library.views (HTML + delivery edges)."""

from __future__ import annotations

import time
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from library import delivery, digital, notifications, privacy, social
from library.models import (
    Branch,
    Consortium,
    Copy,
    DigitalAsset,
    DigitalAssetFormat,
    DigitalLicense,
    Edition,
    Fee,
    FeeStatus,
    FeeType,
    IllRequest,
    IllStatus,
    LicenseModel,
    LoanStatus,
    Organization,
    PatronProfile,
    Payment,
    Plan,
    ReadingList,
    SsoConnection,
    SsoIdentity,
    StaffMembership,
    StaffRole,
    Work,
)
from library.services import DomainError, borrow_work, place_hold, rebuild_work_search_document, return_loan

pytestmark = pytest.mark.django_db(transaction=True)


def _slug(prefix: str = "pv") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _catalog(*, slug: str | None = None):
    slug = slug or _slug()
    org = Organization.objects.create(name=f"Org {slug}", slug=slug)
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title=f"Book {slug}", slug=f"book-{slug}")
    edition = Edition.objects.create(work=work, isbn_13=f"978{uuid.uuid4().int % 10**10:010d}")
    copy = Copy.objects.create(
        organization=org, edition=edition, branch=branch, barcode=f"B-{slug}"
    )
    rebuild_work_search_document(work.pk)
    user = get_user_model().objects.create_user(
        username=f"patron-{slug}",
        password="demo12345",
        email=f"{slug}@example.test",
        first_name="Pat",
        last_name="Ron",
    )
    patron = PatronProfile.objects.create(
        user=user,
        organization=org,
        library_card_number=f"C-{slug}",
        home_branch=branch,
        notification_email=f"notify-{slug}@example.test",
        sms_number="+15555550100",
        notification_channels=["email", "sms"],
    )
    return org, branch, work, edition, copy, patron


def _patron(org, branch, n: int = 1):
    slug = _slug(f"p{n}")
    user = get_user_model().objects.create_user(username=f"u-{slug}", password="demo12345")
    return PatronProfile.objects.create(
        user=user, organization=org, library_card_number=f"C-{slug}", home_branch=branch
    )


def _staff(org, *, role=StaffRole.BRANCH_MANAGER, branch=None):
    slug = _slug("staff")
    user = get_user_model().objects.create_user(username=f"staff-{slug}", password="demo12345", is_staff=True)
    StaffMembership.objects.create(user=user, organization=org, branch=branch, role=role, active=True)
    return user


def _digital_env(org, branch, edition):
    lic = DigitalLicense.objects.create(
        organization=org,
        edition=edition,
        license_model=LicenseModel.ONE_COPY_ONE_USER,
        concurrent_limit=1,
        loan_period_days=21,
    )
    DigitalAsset.objects.create(
        edition=edition,
        fmt=DigitalAssetFormat.TEXT,
        title="Reader",
        text_content=[
            {"title": "Ch 1", "body": "Opening paragraph."},
            {"title": "Ch 2", "body": "Second chapter."},
        ],
    )
    return lic


def _sso_connection(org):
    return SsoConnection.objects.create(
        organization=org,
        client_id="cid",
        client_secret="csecret",
        authorize_url="https://idp.test/authorize",
        token_url="https://idp.test/token",
        userinfo_url="https://idp.test/userinfo",
    )


# --------------------------------------------------------------------------- #
# privacy.erase_patron guards
# --------------------------------------------------------------------------- #
def test_erase_patron_blocks_active_loan():
    org, branch, work, _edition, _copy, patron = _catalog()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    with pytest.raises(DomainError, match="Return all loans"):
        privacy.erase_patron(patron=patron, actor=patron.user)


def test_erase_patron_blocks_overdue_loan():
    org, branch, work, _edition, _copy, patron = _catalog()
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    loan.status = LoanStatus.OVERDUE
    loan.save(update_fields=["status", "updated_at"])
    with pytest.raises(DomainError, match="Return all loans"):
        privacy.erase_patron(patron=patron, actor=patron.user)


def test_erase_patron_blocks_active_digital_loan():
    org, branch, _work, edition, _copy, patron = _catalog()
    _digital_env(org, branch, edition)
    digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)
    with pytest.raises(DomainError, match="digital loans"):
        privacy.erase_patron(patron=patron, actor=patron.user)


def test_erase_patron_blocks_outstanding_fees():
    org, branch, work, _edition, _copy, patron = _catalog()
    Fee.objects.create(
        organization=org,
        patron=patron,
        fee_type=FeeType.MANUAL,
        amount_cents=500,
        status=FeeStatus.OUTSTANDING,
    )
    with pytest.raises(DomainError, match="outstanding fees"):
        privacy.erase_patron(patron=patron, actor=patron.user)


def test_erase_patron_blocks_active_ill():
    org, branch, work, _edition, _copy, patron = _catalog()
    cons = Consortium.objects.create(name="Net", slug=_slug("cons"))
    IllRequest.objects.create(
        consortium=cons,
        work=work,
        requesting_org=org,
        requesting_patron=patron,
        status=IllStatus.REQUESTED,
    )
    with pytest.raises(DomainError, match="inter-library"):
        privacy.erase_patron(patron=patron, actor=patron.user)


# --------------------------------------------------------------------------- #
# privacy.export_patron_data completeness
# --------------------------------------------------------------------------- #
def test_export_patron_data_rich_snapshot():
    org, branch, work, edition, copy, patron = _catalog()
    patron.retain_loan_history = True
    patron.save(update_fields=["retain_loan_history"])
    Copy.objects.create(
        organization=org,
        edition=edition,
        branch=branch,
        barcode=f"B2-{org.slug}",
    )
    _digital_env(org, branch, edition)

    returned = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    return_loan(loan=returned, actor=patron.user)
    active = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    other = _patron(org, branch, 2)
    borrow_work(patron=other, work=work, branch=branch, actor=other.user)
    place_hold(patron=patron, work=work, preferred_branch=branch, actor=patron.user)

    digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)
    Fee.objects.create(
        organization=org,
        patron=patron,
        fee_type=FeeType.OVERDUE,
        amount_cents=250,
        paid_cents=250,
        status=FeeStatus.PAID,
    )
    Payment.objects.create(
        organization=org, patron=patron, amount_cents=250, method="online"
    )
    social.submit_review(patron=patron, work=work, rating=4, body="Solid.")
    rl = ReadingList.objects.create(
        organization=org, patron=patron, name="Favorites", public=True
    )
    rl.works.add(work)

    conn = _sso_connection(org)
    SsoIdentity.objects.create(connection=conn, user=patron.user, subject="idp-subject")

    data = privacy.export_patron_data(patron)
    assert data["profile"]["library_card_number"] == patron.library_card_number
    assert data["profile"]["organization"] == org.slug
    assert data["profile"]["notification_email"] == patron.notification_email
    assert len(data["loans"]) >= 2
    assert any(l["status"] == LoanStatus.RETURNED for l in data["loans"])
    assert any(l["status"] == LoanStatus.ACTIVE for l in data["loans"])
    assert len(data["holds"]) == 1
    assert len(data["digital_loans"]) == 1
    assert len(data["fees"]) == 1
    assert len(data["payments"]) == 1
    assert len(data["reviews"]) == 1
    assert data["reading_lists"][0]["name"] == "Favorites"
    assert work.canonical_title in data["reading_lists"][0]["works"]
    assert any(l["status"] == LoanStatus.ACTIVE for l in data["loans"])
    assert active.status == LoanStatus.ACTIVE


def test_erase_patron_scrubs_sso_identity():
    org, branch, work, _edition, copy, patron = _catalog()
    user = patron.user
    patron.retain_loan_history = True
    patron.save(update_fields=["retain_loan_history"])
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    return_loan(loan=loan, actor=patron.user)
    conn = _sso_connection(org)
    SsoIdentity.objects.create(connection=conn, user=user, subject="erase-me")
    patron_pk = patron.pk

    privacy.erase_patron(patron=patron, actor=user)
    assert not PatronProfile.objects.filter(pk=patron_pk).exists()
    assert not SsoIdentity.objects.filter(connection=conn, user=user).exists()


# --------------------------------------------------------------------------- #
# views: organization signup, reviews, account erase
# --------------------------------------------------------------------------- #
def test_organization_signup_get(client):
    Plan.objects.create(slug=_slug("trial"), name="Trial", active=True, public=True)
    resp = client.get("/signup/", secure=True)
    assert resp.status_code == 200
    assert b"organization" in resp.content.lower() or b"signup" in resp.content.lower()


def test_organization_signup_redirects_authenticated(client):
    org, branch, _work, _edition, _copy, patron = _catalog()
    staff = _staff(org, role=StaffRole.ADMIN, branch=branch)
    assert client.login(username=staff.username, password="demo12345")
    resp = client.get("/signup/", secure=True)
    assert resp.status_code == 302
    assert "/librarian/" in resp["Location"]


def test_submit_review_invalid_rating(client):
    org, branch, work, _edition, _copy, patron = _catalog()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    assert client.login(username=patron.user.username, password="demo12345")
    resp = client.post(
        f"/works/{work.slug}/review/",
        {"rating": "9", "body": "Invalid"},
        secure=True,
    )
    assert resp.status_code == 302
    assert resp.url.endswith(work.get_absolute_url())


def test_erase_my_account_shows_domain_error(client):
    org, branch, work, _edition, _copy, patron = _catalog()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    assert client.login(username=patron.user.username, password="demo12345")
    session = client.session
    session["organization_slug"] = org.slug
    session.save()
    resp = client.post("/account/erase/", {"confirm": "yes"}, secure=True)
    assert resp.status_code == 302
    assert PatronProfile.objects.filter(pk=patron.pk).exists()


# --------------------------------------------------------------------------- #
# views: digital reader + content streaming
# --------------------------------------------------------------------------- #
def test_digital_reader_view(client):
    org, branch, _work, edition, _copy, patron = _catalog()
    _digital_env(org, branch, edition)
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)
    assert client.login(username=patron.user.username, password="demo12345")
    session = client.session
    session["organization_slug"] = org.slug
    session.save()
    resp = client.get(f"/read/{loan.pk}/", secure=True)
    assert resp.status_code == 200
    assert b"manifest" in resp.content.lower() or b"reader" in resp.content.lower()


def test_digital_reader_manifest_error_redirects(client, monkeypatch):
    org, branch, _work, edition, _copy, patron = _catalog()
    _digital_env(org, branch, edition)
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)

    def _boom(**kwargs):
        raise DomainError("Loan expired.")

    monkeypatch.setattr("library.delivery.access_manifest", _boom)
    assert client.login(username=patron.user.username, password="demo12345")
    session = client.session
    session["organization_slug"] = org.slug
    session.save()
    resp = client.get(f"/read/{loan.pk}/", secure=True)
    assert resp.status_code == 302
    assert "/account/" in resp["Location"]


def test_digital_content_text_chapter(client):
    org, branch, _work, edition, _copy, patron = _catalog()
    _digital_env(org, branch, edition)
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)
    token = delivery.mint_content_token(loan, locator="chapter:0")
    resp = client.get(f"/digital/content/{token}/", secure=True)
    assert resp.status_code == 200
    assert b"Opening paragraph" in resp.content


def test_digital_content_missing_chapter(client):
    org, branch, _work, edition, _copy, patron = _catalog()
    _digital_env(org, branch, edition)
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)
    token = delivery.mint_content_token(loan, locator="chapter:99")
    resp = client.get(f"/digital/content/{token}/", secure=True)
    assert resp.status_code == 404
    assert b"Chapter not found" in resp.content


def test_digital_content_audio_range(client):
    org, branch, _work, edition, _copy, patron = _catalog()
    _digital_env(org, branch, edition)
    delivery.store_blob("audio-key", b"0123456789ABCDEF", content_type="audio/mpeg")
    DigitalAsset.objects.filter(edition=edition).update(
        fmt=DigitalAssetFormat.AUDIO,
        media_key="audio-key",
        content_type="audio/mpeg",
        byte_size=16,
        text_content=[],
    )
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)
    token = delivery.mint_content_token(loan, locator="audio")
    resp = client.get(f"/digital/content/{token}/", HTTP_RANGE="bytes=0-3", secure=True)
    assert resp.status_code == 206
    assert resp["Content-Range"].startswith("bytes 0-3/")
    assert len(resp.content) == 4


def test_digital_content_range_not_satisfiable(client):
    org, branch, _work, edition, _copy, patron = _catalog()
    _digital_env(org, branch, edition)
    delivery.store_blob("tiny-audio", b"12345", content_type="audio/mpeg")
    DigitalAsset.objects.filter(edition=edition).update(
        fmt=DigitalAssetFormat.AUDIO,
        media_key="tiny-audio",
        content_type="audio/mpeg",
        byte_size=5,
        text_content=[],
    )
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)
    token = delivery.mint_content_token(loan, locator="audio")
    resp = client.get(f"/digital/content/{token}/", HTTP_RANGE="bytes=100-50", secure=True)
    assert resp.status_code == 416


def test_digital_content_bad_token(client):
    resp = client.get("/digital/content/not-valid/", secure=True)
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# views: SSO
# --------------------------------------------------------------------------- #
def test_sso_login_redirects_to_idp(client):
    slug = _slug("sso")
    org = Organization.objects.create(name="SSO Lib", slug=slug, active=True)
    _sso_connection(org)
    resp = client.get(f"/sso/{slug}/login/", secure=True)
    assert resp.status_code == 302
    assert resp["Location"].startswith("https://idp.test/authorize?")


def test_sso_callback_success(client, monkeypatch):
    slug = _slug("cb")
    org = Organization.objects.create(name="CB Lib", slug=slug, active=True)
    Branch.objects.create(organization=org, name="Main", slug="main")
    _sso_connection(org)
    state = "state-token-123"
    session = client.session
    session["sso_state"] = state
    session["sso_org"] = slug
    session["sso_nonce"] = "nonce-abc"
    session.save()

    user = get_user_model().objects.create_user(username=f"sso-{slug}", is_active=True)

    def _handle_callback(connection, code, redirect_uri, expected_nonce):
        assert code == "auth-code"
        assert expected_nonce == "nonce-abc"
        return user

    monkeypatch.setattr("library.views.sso.handle_callback", _handle_callback)
    resp = client.get(f"/sso/callback/?state={state}&code=auth-code", secure=True)
    assert resp.status_code == 302
    assert resp["Location"].endswith("/")


def test_sso_callback_domain_error(client, monkeypatch):
    slug = _slug("cb-err")
    org = Organization.objects.create(name="Err Lib", slug=slug, active=True)
    Branch.objects.create(organization=org, name="Main", slug="main")
    _sso_connection(org)
    state = "good-state"
    session = client.session
    session["sso_state"] = state
    session["sso_org"] = slug
    session["sso_nonce"] = "n1"
    session.save()

    def _fail(*args, **kwargs):
        raise DomainError("SSO failed.")

    monkeypatch.setattr("library.views.sso.handle_callback", _fail)
    resp = client.get(f"/sso/callback/?state={state}&code=x", secure=True)
    assert resp.status_code == 302
    assert "login" in resp["Location"]


# --------------------------------------------------------------------------- #
# views: unsubscribe + MFA enroll
# --------------------------------------------------------------------------- #
def test_unsubscribe_get_and_one_click(client):
    org, _branch, _work, _edition, _copy, patron = _catalog()
    token = notifications.ensure_unsubscribe_token(patron)
    get_resp = client.get(f"/u/{token}/", secure=True)
    assert get_resp.status_code == 200
    patron.refresh_from_db()
    assert patron.unsubscribed_at is None

    post_resp = client.post(
        f"/u/{token}/",
        {"List-Unsubscribe": "One-Click"},
        secure=True,
    )
    assert post_resp.status_code == 200
    patron.refresh_from_db()
    assert patron.unsubscribed_at is not None


def test_mfa_enroll_get_and_begin(client):
    from library import mfa

    slug = _slug("mfa")
    org = Organization.objects.create(name="MFA Org", slug=slug)
    user = _staff(org, role=StaffRole.ADMIN)
    assert client.login(username=user.username, password="demo12345")
    session = client.session
    session["organization_slug"] = slug
    session.save()

    get_resp = client.get("/mfa/enroll/", secure=True)
    assert get_resp.status_code == 200

    begin = client.post("/mfa/enroll/", {"action": "begin"}, secure=True)
    assert begin.status_code == 200
    secret = client.session.get("mfa_enroll_secret")
    assert secret

    confirm = client.post(
        "/mfa/enroll/",
        {"action": "confirm", "code": mfa.totp(secret, timestamp=time.time())},
        secure=True,
    )
    assert confirm.status_code == 302
    assert mfa.user_has_mfa(user)


def test_mfa_enroll_shows_session_secret_on_get(client):
    slug = _slug("mfa2")
    org = Organization.objects.create(name="MFA2", slug=slug)
    user = _staff(org, role=StaffRole.ADMIN)
    assert client.login(username=user.username, password="demo12345")
    session = client.session
    session["organization_slug"] = slug
    session["mfa_enroll_secret"] = "TESTSECRET123"
    session.save()
    resp = client.get("/mfa/enroll/", secure=True)
    assert resp.status_code == 200
    assert b"TESTSECRET123" in resp.content or b"secret" in resp.content.lower()


# --------------------------------------------------------------------------- #
# views: librarian import commit / rollback HTML
# --------------------------------------------------------------------------- #
def test_librarian_import_commit_and_rollback_html(client):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from library.models import CatalogImportStatus, ShelfLocation

    org, branch, _work, _edition, _copy, _patron = _catalog()
    ShelfLocation.objects.create(branch=branch, code="FIC", name="Fiction")
    librarian = _staff(org, role=StaffRole.BRANCH_MANAGER, branch=branch)
    assert client.login(username=librarian.username, password="demo12345")
    session = client.session
    session["organization_slug"] = org.slug
    session.save()

    csv_bytes = (
        b"title,authors,isbn_13,branch,barcode,shelf_code\n"
        b"Import Title,Jane Doe,9784444444444,main,IMP-0001,FIC\n"
    )
    upload = SimpleUploadedFile("rows.csv", csv_bytes, content_type="text/csv")
    upload_resp = client.post("/librarian/imports/", {"csv_file": upload}, secure=True)
    assert upload_resp.status_code == 302

    batch = org.import_batches.get()
    assert batch.status == CatalogImportStatus.VALIDATED

    detail = client.get(f"/librarian/imports/{batch.pk}/", secure=True)
    assert detail.status_code == 200

    commit = client.post(f"/librarian/imports/{batch.pk}/commit/", {}, secure=True)
    assert commit.status_code == 302
    batch.refresh_from_db()
    assert batch.status == CatalogImportStatus.COMMITTED
    assert Copy.objects.filter(organization=org, barcode="IMP-0001").exists()

    rollback = client.post(
        f"/librarian/imports/{batch.pk}/rollback/",
        {"reason": "test rollback"},
        secure=True,
    )
    assert rollback.status_code == 302
    batch.refresh_from_db()
    assert batch.status == CatalogImportStatus.ROLLED_BACK

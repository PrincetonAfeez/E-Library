import pytest
from django.contrib.auth import get_user_model

from library.models import (
    Author,
    Branch,
    Copy,
    CopyStatus,
    Edition,
    HoldStatus,
    LoanStatus,
    Organization,
    PatronProfile,
    Subject,
    Work,
)
from library.services import borrow_work, place_hold, return_loan

pytestmark = pytest.mark.django_db(transaction=True)


def make_catalog():
    User = get_user_model()
    org = Organization.objects.create(name="Test Library", slug="test")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    author = Author.objects.create(name="Test Author")
    subject = Subject.objects.create(name="Fiction", slug="fiction")
    work = Work.objects.create(canonical_title="The Test Book", slug="test-book")
    work.authors.add(author)
    work.subjects.add(subject)
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    copy = Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="COPY-1")
    user = User.objects.create_user(username="reader", password="demo12345")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="CARD-1", home_branch=branch
    )
    other_user = User.objects.create_user(username="other", password="demo12345")
    other = PatronProfile.objects.create(
        user=other_user, organization=org, library_card_number="CARD-2", home_branch=branch
    )
    return org, branch, work, copy, patron, other


def test_borrow_locks_copy_state():
    _org, branch, work, copy, patron, _other = make_catalog()
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    copy.refresh_from_db()
    assert loan.status == LoanStatus.ACTIVE
    assert copy.status == CopyStatus.LOANED


def test_return_reoffers_copy_to_fifo_hold():
    _org, branch, work, copy, patron, other = make_catalog()
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    hold = place_hold(patron=other, work=work, preferred_branch=branch, actor=other.user)
    assert hold.status == HoldStatus.WAITING

    return_loan(loan=loan, actor=patron.user)

    hold.refresh_from_db()
    copy.refresh_from_db()
    loan.refresh_from_db()
    assert hold.status == HoldStatus.READY
    assert hold.assigned_copy == copy
    assert copy.status == CopyStatus.ON_HOLD
    assert loan.status == LoanStatus.RETURNED
    assert loan.patron is None
    assert loan.patron_hash


def test_ready_hold_checkout_fulfills_hold():
    _org, branch, work, copy, patron, _other = make_catalog()
    hold = place_hold(patron=patron, work=work, preferred_branch=branch, actor=patron.user)
    assert hold.status == HoldStatus.READY
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    hold.refresh_from_db()
    assert hold.status == HoldStatus.FULFILLED
    assert hold.loan == loan

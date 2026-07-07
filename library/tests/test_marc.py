"""Tests for MARC21 import/export and ISBN enrichment (Increment 3)."""

import json

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from library import enrichment
from library.enrichment import enrich_edition
from library.imports import commit_import, import_marc
from library.marc import (
    MarcField,
    MarcRecord,
    marc_record_to_import_row,
    parse_iso2709,
    parse_marc,
    to_iso2709,
    to_marcxml,
)
from library.models import (
    Author,
    Branch,
    Copy,
    Edition,
    Organization,
    StaffMembership,
    StaffRole,
    Subject,
    Work,
)

pytestmark = pytest.mark.django_db(transaction=True)


def sample_record():
    return MarcRecord(
        fields=[
            MarcField("020", subfields=[("a", "9780441478125")]),
            MarcField("100", indicators=("1", " "), subfields=[("a", "Ursula K. Le Guin")]),
            MarcField(
                "245",
                indicators=("1", "0"),
                subfields=[("a", "The Left Hand of Darkness"), ("b", "a novel")],
            ),
            MarcField("264", indicators=(" ", "1"), subfields=[("b", "Ace"), ("c", "1969")]),
            MarcField("650", indicators=(" ", "0"), subfields=[("a", "Science fiction")]),
        ]
    )


# --------------------------------------------------------------------------- #
# MARC round-trips (no DB)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_iso2709_round_trip():
    data = to_iso2709([sample_record()])
    back = parse_iso2709(data)
    assert len(back) == 1
    rec = back[0]
    assert rec.first("245", "a") == "The Left Hand of Darkness"
    assert rec.first("245", "b") == "a novel"
    assert rec.first("020", "a") == "9780441478125"
    assert rec.first("100", "a") == "Ursula K. Le Guin"
    assert rec.first("650", "a") == "Science fiction"


@pytest.mark.django_db
def test_marcxml_round_trip():
    xml = to_marcxml([sample_record()])
    back = parse_marc(xml)
    assert back[0].first("245", "a") == "The Left Hand of Darkness"
    assert back[0].first("264", "c") == "1969"


@pytest.mark.django_db
def test_record_maps_to_import_row():
    row = marc_record_to_import_row(sample_record())
    assert row["title"] == "The Left Hand of Darkness"
    assert row["subtitle"] == "a novel"
    assert row["authors"] == "Ursula K. Le Guin"
    assert row["subjects"] == "Science fiction"
    assert row["isbn_13"] == "9780441478125"
    assert row["publisher"] == "Ace"
    assert row["publication_year"] == "1969"


# --------------------------------------------------------------------------- #
# MARC import pipeline
# --------------------------------------------------------------------------- #
def test_import_marc_stages_and_commits():
    org = Organization.objects.create(name="Lib", slug="lib")
    xml = to_marcxml([sample_record()])
    batch = import_marc(organization=org, content=xml)
    assert batch.error_count == 0
    assert batch.validation_summary["valid_rows"] == 1
    commit_import(batch=batch)
    work = Work.objects.get(slug="the-left-hand-of-darkness")
    assert Edition.objects.filter(isbn_13="9780441478125", work=work).exists()
    assert Author.objects.filter(name="Ursula K. Le Guin").exists()
    assert Subject.objects.filter(name="Science fiction").exists()


# --------------------------------------------------------------------------- #
# MARC export
# --------------------------------------------------------------------------- #
def _staff_client(org):
    staff = get_user_model().objects.create_user(username="adm", is_staff=True)
    StaffMembership.objects.create(user=staff, organization=org, branch=None, role=StaffRole.ADMIN)
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=staff)
    client.defaults["secure"] = True
    return client


def test_marc_export_api_round_trips():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="The Left Hand of Darkness", slug="lhod")
    work.authors.add(Author.objects.create(name="Ursula K. Le Guin"))
    edition = Edition.objects.create(work=work, isbn_13="9780441478125", publisher="Ace")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="C1")

    client = _staff_client(org)
    resp = client.get("/api/v1/librarian/exports/marc/?fmt=xml", secure=True)
    assert resp.status_code == 200
    assert b"The Left Hand of Darkness" in resp.content
    records = parse_marc(resp.content)
    assert records[0].first("245", "a") == "The Left Hand of Darkness"

    resp = client.get("/api/v1/librarian/exports/marc/?fmt=marc", secure=True)
    assert resp.status_code == 200
    assert parse_marc(resp.content)[0].first("020", "a") == "9780441478125"


# --------------------------------------------------------------------------- #
# ISBN enrichment
# --------------------------------------------------------------------------- #
def _fake_openlibrary(url):
    return json.dumps(
        {
            "ISBN:9780441478125": {
                "title": "The Left Hand of Darkness",
                "authors": [{"name": "Ursula K. Le Guin"}],
                "publishers": [{"name": "Ace Books"}],
                "publish_date": "1969",
                "subjects": [{"name": "Science fiction"}, {"name": "Gender"}],
                "cover": {"large": "https://covers.openlibrary.org/b/id/1-L.jpg"},
            }
        }
    ).encode()


def test_enrich_edition_fills_blanks():
    Organization.objects.create(name="Lib", slug="lib")
    work = Work.objects.create(canonical_title="Untitled Import", slug="ui")
    edition = Edition.objects.create(work=work, isbn_13="9780441478125")
    assert not edition.publisher and edition.publication_year is None

    changed = enrich_edition(edition=edition, fetch=_fake_openlibrary)
    assert changed is True
    edition.refresh_from_db()
    assert edition.publisher == "Ace Books"
    assert edition.publication_year == 1969
    assert edition.cover_image
    assert work.authors.filter(name="Ursula K. Le Guin").exists()
    assert work.subjects.filter(name="Science fiction").exists()


def test_enrich_edition_no_metadata_is_noop():
    Organization.objects.create(name="Lib", slug="lib")
    work = Work.objects.create(canonical_title="X", slug="x")
    edition = Edition.objects.create(work=work, isbn_13="9780000000000")
    assert enrich_edition(edition=edition, fetch=lambda url: b"{}") is False


def test_enrich_api_endpoint(monkeypatch):
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="X", slug="x")
    edition = Edition.objects.create(work=work, isbn_13="9780441478125")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="C1")

    monkeypatch.setattr(
        enrichment,
        "fetch_isbn_metadata",
        lambda isbn, fetch=None: {
            "publisher": "Ace Books",
            "publication_year": 1969,
            "cover_image": "http://x/cover.jpg",
            "summary": "",
            "authors": ["Ursula K. Le Guin"],
            "subjects": ["Science fiction"],
        },
    )
    resp = _staff_client(org).post(
        f"/api/v1/librarian/editions/{edition.pk}/enrich/", {}, format="json", secure=True
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["enriched"] is True

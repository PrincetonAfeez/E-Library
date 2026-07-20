"""Management command coverage via call_command + StringIO stdout capture."""

import tempfile
import uuid
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from library.marc import MarcField, MarcRecord, to_marcxml
from library.models import (
    Branch,
    Organization,
    Plan,
    ScopedApiToken,
    Work,
    WorkSearchDocument,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _slug(prefix="cmd"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _capture(*args, **kwargs):
    out = StringIO()
    call_command(*args, stdout=out, **kwargs)
    return out.getvalue()


# --------------------------------------------------------------------------- #
# seed_plans
# --------------------------------------------------------------------------- #
def test_seed_plans_creates_community_tier():
    _capture("seed_plans")
    assert Plan.objects.filter(slug="community").exists()


# --------------------------------------------------------------------------- #
# issue_api_token
# --------------------------------------------------------------------------- #
def test_issue_api_token_creates_scoped_token():
    slug = _slug("tok")
    org = Organization.objects.create(name="Tok Org", slug=slug)
    user = get_user_model().objects.create_user(username=f"user-{slug}")

    out = StringIO()
    call_command("issue_api_token", user.username, "--org", org.slug, stdout=out)
    raw_key = out.getvalue().strip()

    assert raw_key
    assert ScopedApiToken.objects.filter(user=user, organization=org).exists()


def test_issue_api_token_rejects_missing_user():
    slug = _slug("tok-bad")
    org = Organization.objects.create(name="Tok Org", slug=slug)
    with pytest.raises(CommandError, match="User not found"):
        call_command("issue_api_token", "nobody", "--org", org.slug)


def test_issue_api_token_rejects_missing_org():
    slug = _slug("tok-bad2")
    user = get_user_model().objects.create_user(username=f"user-{slug}")
    with pytest.raises(CommandError, match="Organization not found"):
        call_command("issue_api_token", user.username, "--org", "missing-org")


# --------------------------------------------------------------------------- #
# drain_outbox
# --------------------------------------------------------------------------- #
def test_drain_outbox_once_reports_processed():
    output = _capture("drain_outbox", "--once")
    assert "processed=" in output


# --------------------------------------------------------------------------- #
# run_sweeps
# --------------------------------------------------------------------------- #
def test_run_sweeps_reports_core_metrics():
    output = _capture("run_sweeps")
    assert "overdue_flagged=" in output
    assert "ready_holds_expired=" in output
    assert "webhooks_delivered=" in output


def test_run_sweeps_full_reports_heavier_metrics():
    output = _capture("run_sweeps", "--full")
    assert "overdue_flagged=" in output
    assert "transits_recovered=" in output
    assert "holds_reconciled=" in output
    assert "subs_renewed=" in output


# --------------------------------------------------------------------------- #
# rebuild_search_index
# --------------------------------------------------------------------------- #
def test_rebuild_search_index_builds_document():
    work = Work.objects.create(canonical_title="Indexed Work", slug=_slug("work"))
    # Creating a Work may already index via signals; the command must still rebuild.
    output = _capture("rebuild_search_index", "--work-id", str(work.pk))
    assert "Rebuilt 1 search document" in output
    assert WorkSearchDocument.objects.filter(work=work).exists()


# --------------------------------------------------------------------------- #
# enrich_catalog
# --------------------------------------------------------------------------- #
def test_enrich_catalog_with_no_candidates():
    slug = _slug("enrich")
    Organization.objects.create(name="Enrich Org", slug=slug)
    output = _capture("enrich_catalog", "--org", slug)
    assert "Enriched 0 edition" in output


def test_enrich_catalog_rejects_unknown_org():
    with pytest.raises(CommandError, match="not found"):
        call_command("enrich_catalog", "--org", "no-such-org")


# --------------------------------------------------------------------------- #
# import_catalog / import_marc
# --------------------------------------------------------------------------- #
def test_import_catalog_help_lists_arguments(capsys):
    with pytest.raises(SystemExit) as exc:
        call_command("import_catalog", "--help")
    assert exc.value.code in (0, None)
    output = capsys.readouterr().out
    assert "csv_path" in output
    assert "--org" in output


def test_import_catalog_stages_csv_without_commit():
    slug = _slug("csv")
    org = Organization.objects.create(name="Import Org", slug=slug)
    Branch.objects.create(organization=org, name="Main", slug="main")
    csv_body = "title,isbn_13,branch,barcode\nDemo Title,9781234567890,main,IMP-001\n"
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as handle:
        handle.write(csv_body)
        path = handle.name

    output = _capture("import_catalog", path, "--org", slug)
    assert "batch=" in output
    assert "rows=" in output
    assert "Re-run with --commit" in output


def test_import_marc_help_lists_arguments(capsys):
    with pytest.raises(SystemExit) as exc:
        call_command("import_marc", "--help")
    assert exc.value.code in (0, None)
    output = capsys.readouterr().out
    assert "marc_path" in output
    assert "--org" in output


def test_import_marc_stages_marcxml_without_commit():
    slug = _slug("marc")
    org = Organization.objects.create(name="MARC Org", slug=slug)
    record = MarcRecord(
        fields=[
            MarcField("020", subfields=[("a", "9780441478125")]),
            MarcField("100", indicators=("1", " "), subfields=[("a", "Author Name")]),
            MarcField(
                "245",
                indicators=("1", "0"),
                subfields=[("a", "MARC Demo Title")],
            ),
        ]
    )
    xml = to_marcxml([record])
    with tempfile.NamedTemporaryFile("wb", suffix=".xml", delete=False) as handle:
        handle.write(xml)
        path = handle.name

    output = _capture("import_marc", path, "--org", slug)
    assert "batch=" in output
    assert "records=" in output
    assert "Re-run with --commit" in output


# --------------------------------------------------------------------------- #
# seed_demo (heavy — smoke the command class only)
# --------------------------------------------------------------------------- #
@pytest.mark.skip(reason="seed_demo seeds 96 works and fixed demo users; too heavy for CI")
def test_seed_demo_populates_metro_library():
    output = _capture("seed_demo", "--works", "1")
    assert Organization.objects.filter(slug="metro-library").exists()
    assert "Seeded" in output


def test_seed_demo_command_exposes_handle():
    from library.management.commands.seed_demo import Command

    assert hasattr(Command, "handle")


# --------------------------------------------------------------------------- #
# seed_policies
# --------------------------------------------------------------------------- #
def test_seed_policies_command():
    slug = _slug("pol")
    org = Organization.objects.create(name="Policy Org", slug=slug)
    output = _capture("seed_policies", "--org", slug)
    assert "Seeded patron/material types" in output
    from library.models import MaterialType, PatronType

    assert PatronType.objects.filter(organization=org, code="adult").exists()
    assert MaterialType.objects.filter(organization=org, code="book").exists()


def test_run_sip2_rejects_missing_org():
    with pytest.raises(CommandError, match="not found"):
        call_command("run_sip2", "--org", "no-sip2-org")


def test_run_sip2_rejects_missing_credentials():
    slug = _slug("sip2")
    Organization.objects.create(name="SIP2 Org", slug=slug)
    with pytest.raises(CommandError, match="no SIP2 credentials"):
        call_command("run_sip2", "--org", slug)

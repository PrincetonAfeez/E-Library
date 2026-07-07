"""Tests for the second remediation pass: audit immutability, flags, circuit breaker."""

import pytest
from django.db import Error as DBError
from django.db import connection, transaction

from library import circuit, flags
from library.models import AuditLog, FeatureFlag, Organization

pytestmark = pytest.mark.django_db(transaction=True)


# --------------------------------------------------------------------------- #
# Audit-log immutability (ENT-audit-immutability)
# --------------------------------------------------------------------------- #
def test_auditlog_save_blocks_updates():
    log = AuditLog.objects.create(action="x", entity_type="t", entity_id="1")
    log.reason = "tampered"
    with pytest.raises(ValueError):
        log.save()


def test_auditlog_db_trigger_blocks_raw_update():
    log = AuditLog.objects.create(action="x", entity_type="t", entity_id="1")
    with pytest.raises(DBError):  # DB trigger raises; atomic keeps conn usable
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute("UPDATE library_auditlog SET reason='x' WHERE id=%s", [log.id])
    log.refresh_from_db()
    assert log.reason == ""  # unchanged


def test_auditlog_delete_still_allowed_for_retention():
    log = AuditLog.objects.create(action="x", entity_type="t", entity_id="1")
    AuditLog.objects.filter(pk=log.pk).delete()
    assert not AuditLog.objects.filter(pk=log.pk).exists()


# --------------------------------------------------------------------------- #
# Feature flags (GROWTH-feature-flags)
# --------------------------------------------------------------------------- #
def test_feature_flag_resolution_order(settings):
    org = Organization.objects.create(name="O", slug="o")
    # Default when nothing is set.
    assert flags.flag_enabled("beta") is False
    # settings fallback.
    settings.FEATURE_FLAGS = {"beta": True}
    assert flags.flag_enabled("beta") is True
    # Global DB row overrides settings.
    flags.set_flag("beta", False)
    assert flags.flag_enabled("beta") is False
    # Per-org row overrides the global row.
    flags.set_flag("beta", True, organization=org)
    assert flags.flag_enabled("beta", org) is True
    assert flags.flag_enabled("beta") is False  # global still off
    assert FeatureFlag.objects.filter(key="beta").count() == 2


# --------------------------------------------------------------------------- #
# Circuit breaker (INT-integrations)
# --------------------------------------------------------------------------- #
def test_circuit_opens_after_threshold():
    circuit.reset()
    host = "down.test"

    def call():
        with circuit.guard(host, threshold=2, cooldown=60):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        call()
    with pytest.raises(RuntimeError):
        call()
    # Circuit now open -> fail fast without executing the body.
    with pytest.raises(circuit.CircuitOpen):
        with circuit.guard(host, threshold=2, cooldown=60):
            raise AssertionError("body must not run while open")


def test_circuit_half_open_recovers_on_success():
    circuit.reset()
    host = "flaky.test"
    for _ in range(2):
        with pytest.raises(RuntimeError):
            with circuit.guard(host, threshold=2, cooldown=0):
                raise RuntimeError("boom")
    # cooldown=0 -> half-open allows a trial; a success closes the circuit.
    with circuit.guard(host, threshold=2, cooldown=0):
        pass
    ran = False
    with circuit.guard(host, threshold=2, cooldown=0):
        ran = True
    assert ran is True

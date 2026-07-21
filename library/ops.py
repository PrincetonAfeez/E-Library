"""Operational helpers for scheduled jobs: single-runner locking + DLQ alerting."""
 
from __future__ import annotations

import contextlib
import logging

from django.db import connection

logger = logging.getLogger("library")

# Arbitrary constant advisory-lock key for the sweep runner.
SWEEP_LOCK_KEY = 992001
DEAD_LETTER_THRESHOLD = 25


@contextlib.contextmanager
def scheduler_lock(key: int = SWEEP_LOCK_KEY):
    """Session-level PostgreSQL advisory lock so only one instance runs a sweep.

    Guards scheduled jobs against double-execution when more than one scheduler
    container is running. Yields True if the lock was acquired, else False.
    """
    if connection.vendor != "postgresql":
        yield True  # nothing to serialize on other backends (tests/sqlite)
        return
    acquired = False
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
        acquired = bool(cursor.fetchone()[0])
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])


def dead_letter_backlog(*, threshold: int = DEAD_LETTER_THRESHOLD) -> dict:
    """Count parked/failed async work and warn (queryable in logs) if it's high."""
    from .models import OutboxEvent, OutboxStatus, WebhookDelivery, WebhookDeliveryStatus

    outbox_failed = OutboxEvent.objects.filter(status=OutboxStatus.FAILED).count()
    webhook_failed = WebhookDelivery.objects.filter(
        status=WebhookDeliveryStatus.FAILED
    ).count()
    total = outbox_failed + webhook_failed
    if total >= threshold:
        logger.warning(
            "dead_letter_backlog_high outbox_failed=%s webhook_failed=%s threshold=%s",
            outbox_failed, webhook_failed, threshold,
        )
    return {"outbox_failed": outbox_failed, "webhook_failed": webhook_failed}

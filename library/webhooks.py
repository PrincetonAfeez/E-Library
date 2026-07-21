"""Outbound webhooks: fan domain events out to tenant HTTP endpoints.

When the outbox processes a domain event we enqueue a ``WebhookDelivery`` for each
matching endpoint; ``deliver_webhooks`` then POSTs them (HMAC-signed) with
retry/backoff. The HTTP POST is injectable so delivery is testable offline.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .crypto import decrypt_value
from .models import (
    DomainEvent,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookEndpoint,
)

logger = logging.getLogger("library")
MAX_ATTEMPTS = 6


def enqueue_for_outbox_event(event) -> int:
    """Create webhook deliveries for a processed outbox event's matching endpoints."""
    organization = event.organization
    if organization is None:
        return 0
    domain_event_id = event.payload.get("domain_event_id")
    domain_event = DomainEvent.objects.filter(pk=domain_event_id).first() if domain_event_id else None
    body = {
        "event": event.event_type,
        "organization": organization.slug,
        "occurred_at": (domain_event.created_at if domain_event else timezone.now()).isoformat(),
        # Use the durable DomainEvent payload (never the outbox-only extras such as
        # a recipient email captured for notifications).
        "data": (domain_event.payload if domain_event else {}),
        "aggregate": {
            "type": domain_event.aggregate_type if domain_event else "",
            "id": domain_event.aggregate_id if domain_event else "",
        },
    }
    created = 0
    for endpoint in WebhookEndpoint.objects.filter(organization=organization, active=True):
        if not endpoint.matches(event.event_type):
            continue
        _, delivery_created = WebhookDelivery.objects.get_or_create(
            endpoint=endpoint,
            outbox_event_id=event.pk,
            defaults={"event_type": event.event_type, "payload": body},
        )
        created += int(delivery_created)
    return created


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _default_post(url: str, body: bytes, headers: dict) -> int:
    from urllib.parse import urlparse

    from . import circuit
    from .net import safe_urlopen

    # Trip a per-host breaker so a persistently failing endpoint is skipped fast
    # instead of retried on every sweep (CircuitOpen is handled like any failure).
    with circuit.guard(urlparse(url).netloc):
        with safe_urlopen(url, data=body, headers=headers, method="POST", timeout=8) as response:
            return response.status


def deliver_webhooks(*, batch_size: int = 100, post=None, now=None) -> int:
    post = post or _default_post
    now = now or timezone.now()
    delivered = 0
    WebhookDelivery.objects.filter(
        status=WebhookDeliveryStatus.SENDING, updated_at__lt=now - timedelta(minutes=15)
    ).update(status=WebhookDeliveryStatus.PENDING, next_attempt_at=now, updated_at=now)
    with transaction.atomic():
        due = list(
            WebhookDelivery.objects.select_for_update(skip_locked=True)
            .select_related("endpoint")
            .filter(status=WebhookDeliveryStatus.PENDING, next_attempt_at__lte=now)
            .order_by("next_attempt_at", "id")[:batch_size]
        )
        for delivery in due:
            delivery.status = WebhookDeliveryStatus.SENDING
            delivery.attempts += 1
            delivery.save(update_fields=["status", "attempts", "updated_at"])
    for delivery in due:
        body = json.dumps(delivery.payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Elibrary-Event": delivery.event_type}
        if delivery.endpoint.secret:
            secret = decrypt_value(delivery.endpoint.secret)
            headers["X-Elibrary-Signature"] = _sign(secret, body)
        try:
            code = post(delivery.endpoint.url, body, headers)
            if 200 <= code < 300:
                delivery.status = WebhookDeliveryStatus.DELIVERED
                delivery.response_status = code
                delivery.delivered_at = timezone.now()
                delivery.last_error = ""
                delivered += 1
            else:
                raise RuntimeError(f"HTTP {code}")
        except Exception as exc:
            delivery.last_error = str(exc)[:500]
            if delivery.attempts >= MAX_ATTEMPTS:
                delivery.status = WebhookDeliveryStatus.FAILED
                logger.error("Webhook %s dead-lettered: %s", delivery.pk, exc)
            else:
                delivery.status = WebhookDeliveryStatus.PENDING
                delivery.next_attempt_at = timezone.now() + timedelta(minutes=2**delivery.attempts)
        delivery.save(
            update_fields=[
                "status",
                "attempts",
                "response_status",
                "last_error",
                "next_attempt_at",
                "delivered_at",
                "updated_at",
            ]
        )
    return delivered

"""Notification delivery channels (email, SMS, push).

A pluggable abstraction like the billing gateway: email uses Django's mail
backend; SMS/push use provider SDKs when configured (Twilio / FCM) and
otherwise a manual backend that records to an in-memory outbox — so the whole
multi-channel flow runs and is testable without external services.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger("library")

# Test/dev-inspectable outboxes for the manual backends (cf. Django's mail.outbox).
sms_outbox: list[dict] = []
push_outbox: list[dict] = []


class EmailChannel:
    key = "email"

    def address(self, patron) -> str:
        return patron.notification_email or patron.user.email

    def send(self, to: str, subject: str, body: str) -> None:
        send_mail(subject, body, None, [to], fail_silently=False)


class SmsChannel:
    key = "sms"

    def address(self, patron) -> str:
        return patron.sms_number

    def send(self, to: str, subject: str, body: str) -> None:
        sid = getattr(settings, "TWILIO_ACCOUNT_SID", "")
        if sid:  # pragma: no cover - only when Twilio is configured
            try:
                from twilio.rest import Client

                Client(sid, settings.TWILIO_AUTH_TOKEN).messages.create(
                    to=to, from_=settings.TWILIO_FROM_NUMBER, body=f"{subject}\n{body}"
                )
                return
            except ImportError:
                logger.warning("Twilio configured but SDK missing; using manual SMS backend.")
        sms_outbox.append({"to": to, "body": f"{subject}\n{body}"})
        logger.info("SMS -> %s: %s", to, subject)


class PushChannel:
    key = "push"

    def address(self, patron) -> str:
        return patron.push_token

    def send(self, to: str, subject: str, body: str) -> None:
        # A real integration would call FCM/APNs here; the manual backend records.
        push_outbox.append({"to": to, "title": subject, "body": body})
        logger.info("PUSH -> %s: %s", to, subject)


_CHANNELS = {c.key: c for c in (EmailChannel(), SmsChannel(), PushChannel())}


def get_channel(key: str):
    return _CHANNELS.get(key)


def patron_channels(patron) -> list[str]:
    """Enabled, known channels for a patron; defaults to email."""
    configured = patron.notification_channels or ["email"]
    return [key for key in configured if key in _CHANNELS] or ["email"]

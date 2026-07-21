"""Delivery of patron-facing notifications triggered by outbox events

``process_outbox_event`` in :mod:`library.services` hands each drained
``OutboxEvent`` to :func:`deliver`.  We resolve the originating aggregate from
the linked :class:`DomainEvent`, look up an organization ``NotificationTemplate``
(falling back to a built-in default), render it, send the email, and record a
truthful :class:`NotificationDelivery` row.  Any send failure is re-raised so the
outbox drainer applies its retry/backoff policy.
"""

from __future__ import annotations

from django.template import Context, Template
from django.utils import timezone

from . import channels
from .models import (
    DigitalHold,
    DomainEvent,
    Hold,
    Loan,
    NotificationDelivery,
    NotificationTemplate,
)

# event_type -> (template_key, default subject, default body)
# Bodies/subjects are Django template strings rendered with a small context.
PATRON_EVENT_TEMPLATES: dict[str, tuple[str, str, str]] = {
    "loan.borrowed": (
        "loan_borrowed",
        "Checked out: {{ title }}",
        "You borrowed “{{ title }}”. It is due on {{ due_at|date:'M j, Y' }}.",
    ),
    "loan.returned": (
        "loan_returned",
        "Returned: {{ title }}",
        "We received your return of “{{ title }}”. Thank you.",
    ),
    "loan.renewed": (
        "loan_renewed",
        "Renewed: {{ title }}",
        "“{{ title }}” was renewed and is now due on {{ due_at|date:'M j, Y' }}.",
    ),
    "loan.overdue": (
        "loan_overdue",
        "Overdue: {{ title }}",
        "“{{ title }}” was due on {{ due_at|date:'M j, Y' }} and is now overdue.",
    ),
    "loan.due_soon": (
        "due_soon",
        "Due soon: {{ title }}",
        "“{{ title }}” is due on {{ due_at|date:'M j, Y' }}. Renew it if you need more time.",
    ),
    "hold.in_transit": (
        "hold_in_transit",
        "On its way: {{ title }}",
        "Your hold on “{{ title }}” is being transferred to {{ branch }}. "
        "We'll let you know when it's ready for pickup.",
    ),
    "hold.ready": (
        "hold_ready",
        "Ready for pickup: {{ title }}",
        "Your hold on “{{ title }}” is ready at {{ branch }}"
        "{% if expires_at %} until {{ expires_at|date:'M j, Y' }}{% endif %}.",
    ),
    "hold.expired": (
        "hold_expired",
        "Hold expired: {{ title }}",
        "Your hold on “{{ title }}” expired before pickup.",
    ),
    "digital.hold_ready": (
        "digital_hold_ready",
        "Ready to borrow: {{ title }}",
        "Your digital hold on “{{ title }}” is ready. Borrow it before the hold expires.",
    ),
    "loan.overdue_reminder": (
        "overdue_reminder",
        "Reminder: “{{ title }}” is {{ stage }} day(s) overdue",
        "“{{ title }}” was due on {{ due_at|date:'M j, Y' }} and is now {{ stage }} day(s) "
        "overdue. Please return or renew it to avoid further charges.",
    ),
    "hold.expiring_soon": (
        "hold_expiring",
        "Pick up soon: {{ title }}",
        "Your hold on “{{ title }}” at {{ branch }} expires on "
        "{{ expires_at|date:'M j, Y' }}. Please pick it up before then.",
    ),
}

# Notification category per event, and which categories are essential
# (transactional — always delivered, never suppressed by preferences).
EVENT_CATEGORY: dict[str, str] = {
    "loan.borrowed": "courtesy",
    "loan.returned": "courtesy",
    "loan.renewed": "courtesy",
    "loan.due_soon": "courtesy",
    "loan.overdue": "overdue",
    "loan.overdue_reminder": "overdue",
    "hold.in_transit": "holds",
    "hold.ready": "holds",
    "hold.expired": "holds",
    "hold.expiring_soon": "holds",
    "digital.hold_ready": "holds",
}
ESSENTIAL_CATEGORIES = {"overdue", "holds"}


def category_for_event(event_type: str) -> str:
    return EVENT_CATEGORY.get(event_type, "courtesy")


def category_allowed(patron, category: str) -> bool:
    """Whether a patron should receive a notice in this category.

    Essential (transactional) categories are always delivered. Non-essential
    ones respect a global unsubscribe and per-category opt-outs.
    """
    if category in ESSENTIAL_CATEGORIES:
        return True
    if patron is None:
        return True  # scrubbed-aggregate fallback only reaches essential paths
    if patron.unsubscribed_at is not None:
        return False
    prefs = patron.notification_prefs or {}
    return bool(prefs.get(category, True))


def ensure_unsubscribe_token(patron) -> str:
    if not patron.unsubscribe_token:
        import secrets

        patron.unsubscribe_token = secrets.token_urlsafe(24)
        patron.save(update_fields=["unsubscribe_token", "updated_at"])
    return patron.unsubscribe_token


def unsubscribe_url(patron) -> str:
    from django.conf import settings

    base = getattr(settings, "SITE_BASE_URL", "") or ""
    return f"{base.rstrip('/')}/u/{ensure_unsubscribe_token(patron)}/"


def ensure_default_templates(organization) -> int:
    """Create any missing default email templates for an organization.

    Delivery already falls back to the built-in defaults, but materializing
    editable rows lets each library customize subject/body via the admin.
    """
    created = 0
    for template_key, default_subject, default_body in PATRON_EVENT_TEMPLATES.values():
        _, was_created = NotificationTemplate.objects.get_or_create(
            organization=organization,
            key=template_key,
            channel="email",
            defaults={"subject": default_subject, "body": default_body},
        )
        created += int(was_created)
    return created


def _render(source: str, context: dict) -> str:
    # Emails are text/plain; autoescape must be off or titles like "Cats & Dogs"
    # would render as "Cats &amp; Dogs".
    return Template(source).render(Context(context, autoescape=False))


def _resolve(domain_event: DomainEvent):
    """Return ``(patron, context)`` for the aggregate, or ``(None, {})``.

    ``patron`` may be ``None`` when privacy scrubbing has already detached it
    (e.g. a returned loan for a patron who does not retain history), in which
    case there is nobody to notify.
    """
    aggregate_type = domain_event.aggregate_type
    aggregate_id = domain_event.aggregate_id
    if aggregate_type == "Loan":
        loan = (
            Loan.objects.select_related(
                "patron__user", "copy__edition__work", "copy__branch"
            )
            .filter(pk=aggregate_id)
            .first()
        )
        if loan is None:
            return None, {}
        context = {
            "title": loan.copy.edition.work.canonical_title,
            "due_at": loan.due_at,
            "branch": loan.copy.branch.name,
        }
        return loan.patron, context
    if aggregate_type == "Hold":
        hold = (
            Hold.objects.select_related("patron__user", "work", "preferred_branch")
            .filter(pk=aggregate_id)
            .first()
        )
        if hold is None:
            return None, {}
        context = {
            "title": hold.work.canonical_title,
            "branch": hold.preferred_branch.name,
            "expires_at": hold.expires_at,
        }
        return hold.patron, context
    if aggregate_type == "DigitalHold":
        hold = (
            DigitalHold.objects.select_related("patron__user", "edition__work")
            .filter(pk=aggregate_id)
            .first()
        )
        if hold is None:
            return None, {}
        return hold.patron, {
            "title": hold.edition.work.canonical_title,
            "expires_at": hold.expires_at,
        }
    return None, {}


def deliver(event) -> None:
    """Deliver a notification for ``event`` across the patron's enabled channels.

    Raises on any send failure so the outbox drainer can retry (already-sent
    channels short-circuit on the retry via their idempotency row).
    """
    spec = PATRON_EVENT_TEMPLATES.get(event.event_type)
    if spec is None:
        return

    domain_event_id = event.payload.get("domain_event_id")
    if not domain_event_id:
        return
    domain_event = DomainEvent.objects.filter(pk=domain_event_id).first()
    if domain_event is None:
        return

    patron, context = _resolve(domain_event)
    payload = event.payload or {}
    # Carry durable event payload fields (e.g. the overdue "stage") into the
    # render context so cadence templates can reference them.
    context = {**(domain_event.payload or {}), **context}

    category = category_for_event(event.event_type)
    if not category_allowed(patron, category):
        return  # patron opted out of this non-essential category

    if patron is None:
        # Scrubbed aggregate: fall back to an email recipient captured in the event.
        context = dict(context)
        context.setdefault("title", payload.get("title", ""))
        channel_keys = ["email"]
    else:
        channel_keys = channels.patron_channels(patron)

    template_key, default_subject, default_body = spec
    related_entity = f"{domain_event.aggregate_type.lower()}:{domain_event.aggregate_id}"
    subject = None
    body = None

    for channel_key in channel_keys:
        channel = channels.get_channel(channel_key)
        if channel is None:
            continue
        recipient = channel.address(patron) if patron is not None else (
            payload.get("recipient", "") if channel_key == "email" else ""
        )
        if not recipient:
            continue
        if subject is None:  # render lazily, once
            template = NotificationTemplate.objects.filter(
                organization=event.organization, key=template_key, active=True
            ).first()
            subject = _render(template.subject if template else default_subject, context)
            body = _render(template.body if template else default_body, context)
            # CAN-SPAM / RFC 8058: body link + List-Unsubscribe headers.
            headers = None
            if category not in ESSENTIAL_CATEGORIES and patron is not None:
                unsub = unsubscribe_url(patron)
                body = f"{body}\n\nTo stop these emails, visit {unsub}"
                headers = {
                    "List-Unsubscribe": f"<{unsub}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                }
            else:
                headers = None
        else:
            headers = None
        _deliver_to_channel(
            event,
            domain_event,
            channel,
            recipient,
            subject,
            body,
            template_key,
            related_entity,
            headers=headers if channel_key == "email" else None,
        )


def _deliver_to_channel(
    event, domain_event, channel, recipient, subject, body, template_key, related_entity, headers=None
):
    # Per (event, channel) idempotency: claim a delivery row before sending so a
    # duplicate attempt collides on the unique constraint instead of re-sending.
    provider_reference = f"{domain_event.pk}:{channel.key}"
    delivery, created = NotificationDelivery.objects.get_or_create(
        organization=event.organization,
        provider_reference=provider_reference,
        defaults={
            "recipient": recipient,
            "channel": channel.key,
            "template_key": template_key,
            "related_entity": related_entity,
            "status": "pending",
            "attempts": 1,
        },
    )
    if not created:
        if delivery.status == "sent":
            return
        delivery.attempts += 1
    try:
        if headers and channel.key == "email":
            channel.send(recipient, subject, body, headers=headers)
        else:
            channel.send(recipient, subject, body)
    except Exception as exc:
        delivery.status = "failed"
        delivery.failed_at = timezone.now()
        delivery.last_error = str(exc)
        delivery.save()
        raise
    delivery.status = "sent"
    delivery.sent_at = timezone.now()
    delivery.save()

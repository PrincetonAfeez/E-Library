"""Events (programs) with waitlists and room reservations with conflict checks."""
 
from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Event,
    EventRegistration,
    RegistrationStatus,
    ReservationStatus,
    Room,
    RoomReservation,
)
from .services import DomainError, audit_action


# --------------------------------------------------------------------------- #
# Room reservations
# --------------------------------------------------------------------------- #
def _has_conflict(room, starts_at, ends_at, *, exclude_pk=None) -> bool:
    qs = RoomReservation.objects.filter(
        room=room, status=ReservationStatus.BOOKED, starts_at__lt=ends_at, ends_at__gt=starts_at
    )
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


def reserve_room(*, patron, room: Room, starts_at, ends_at, purpose: str = "", actor=None) -> RoomReservation:
    if ends_at <= starts_at:
        raise DomainError("End time must be after start time.")
    if starts_at < timezone.now():
        raise DomainError("Cannot reserve a room in the past.")
    with transaction.atomic():
        Room.objects.select_for_update().get(pk=room.pk)  # serialize per room
        if _has_conflict(room, starts_at, ends_at):
            raise DomainError("That room is already booked for the requested time.")
        reservation = RoomReservation.objects.create(
            organization=patron.organization,
            room=room,
            patron=patron,
            starts_at=starts_at,
            ends_at=ends_at,
            purpose=purpose,
        )
        audit_action(action="room.reserve", entity=reservation, actor=actor or patron.user)
        return reservation


def cancel_reservation(*, reservation, actor=None) -> RoomReservation:
    if reservation.status != ReservationStatus.BOOKED:
        raise DomainError("Only booked reservations can be cancelled.")
    reservation.status = ReservationStatus.CANCELLED
    reservation.save(update_fields=["status", "updated_at"])
    audit_action(action="room.cancel", entity=reservation, actor=actor)
    return reservation


def room_availability(room, *, start=None, end=None):
    start = start or timezone.now()
    qs = RoomReservation.objects.filter(room=room, status=ReservationStatus.BOOKED, ends_at__gte=start)
    if end:
        qs = qs.filter(starts_at__lte=end)
    return qs.order_by("starts_at")


# --------------------------------------------------------------------------- #
# Event registration
# --------------------------------------------------------------------------- #
def _registered_count(event) -> int:
    return event.registrations.filter(status=RegistrationStatus.REGISTERED).count()


def register_for_event(*, patron, event: Event, actor=None) -> EventRegistration:
    with transaction.atomic():
        Event.objects.select_for_update().get(pk=event.pk)
        if event.registrations.filter(
            patron=patron, status__in=[RegistrationStatus.REGISTERED, RegistrationStatus.WAITLISTED]
        ).exists():
            raise DomainError("You are already registered for this event.")
        full = event.capacity and _registered_count(event) >= event.capacity
        status = RegistrationStatus.WAITLISTED if full else RegistrationStatus.REGISTERED
        registration = EventRegistration.objects.create(
            organization=patron.organization, event=event, patron=patron, status=status
        )
        audit_action(action="event.register", entity=registration, actor=actor or patron.user)
        return registration


def cancel_registration(*, registration, actor=None) -> EventRegistration:
    with transaction.atomic():
        registration = EventRegistration.objects.select_for_update().get(pk=registration.pk)
        if registration.status not in [RegistrationStatus.REGISTERED, RegistrationStatus.WAITLISTED]:
            raise DomainError("This registration is not active.")
        was_registered = registration.status == RegistrationStatus.REGISTERED
        registration.status = RegistrationStatus.CANCELLED
        registration.save(update_fields=["status", "updated_at"])
        if was_registered:
            _promote_waitlist(registration.event)
        audit_action(action="event.cancel", entity=registration, actor=actor)
        return registration


def _promote_waitlist(event) -> None:
    if event.capacity and _registered_count(event) >= event.capacity:
        return
    nxt = (
        event.registrations.select_for_update(of=("self",))
        .filter(status=RegistrationStatus.WAITLISTED)
        .order_by("created_at", "id")
        .first()
    )
    if nxt is not None:
        nxt.status = RegistrationStatus.REGISTERED
        nxt.save(update_fields=["status", "updated_at"])


def upcoming_events(organization, *, now=None):
    now = now or timezone.now()
    return (
        Event.objects.filter(organization=organization, public=True)
        .filter(Q(ends_at__gte=now))
        .order_by("starts_at")
    )

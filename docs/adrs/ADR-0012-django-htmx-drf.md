# ADR-0012: Django + HTMX + DRF

## Status

Accepted.

## Decision

Django renders the web UI, HTMX handles progressive interactions, and Django REST Framework provides the API surface.

## Consequences

The product avoids a SPA split while still supporting public and authenticated API clients.


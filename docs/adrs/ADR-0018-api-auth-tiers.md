# ADR-0018: API Auth Tiers

## Status

Accepted.

## Decision

The API has anonymous throttled read access, scoped token access for programmatic clients, and session auth for the browser.

## Consequences

CSRF posture follows the auth mode, and token usage can be scoped, rotated, revoked, and audited.


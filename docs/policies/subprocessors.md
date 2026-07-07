# Subprocessors

Third parties that may process customer/personal data on our behalf. All are
**optional** — each is inert unless its credentials are configured; the app runs
fully without any of them.

| Subprocessor | Purpose | Data shared | Activated by |
|--------------|---------|-------------|--------------|
| Payment provider (e.g. Stripe) | Subscription billing | Org billing contact, card brand/last4 (no PAN) | `STRIPE_SECRET_KEY` |
| SMS provider (e.g. Twilio) | Patron SMS notices | Phone number, message body | `TWILIO_ACCOUNT_SID` |
| Push provider (e.g. FCM) | Patron push notices | Device token, message | push config |
| Email provider (SMTP relay) | Transactional email | Recipient email, message | `EMAIL_BACKEND` (console by default) |
| Error monitoring (Sentry) | Crash/error telemetry | Stack traces (PII scrubbed: `send_default_pii=False`) | `SENTRY_DSN` |
| OIDC IdP (customer-chosen) | SSO login | Email, subject id | per-tenant `SsoConnection` |

## Not a subprocessor
- **AI features** run locally (`library/assistant.py`, deterministic embeddings);
  no catalog or patron data is sent to an external model provider.
- Hosting/DB/cache providers (your cloud, Postgres, Redis) are infrastructure
  subprocessors — record your specific vendors here at deploy time.

Keep this list current; changes require customer notice under standard DPA terms.

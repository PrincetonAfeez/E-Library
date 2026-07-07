# SSO / Identity Federation

## Status
- **OIDC** is implemented (`library/sso.py`) with `SsoConnection` / `SsoIdentity`
  models. Discovery, token exchange, and userinfo are done over HTTPS with an
  8-second timeout; an offline stub keeps dev/test runnable without a live IdP.
- **SAML** and **SCIM** provisioning are **not implemented** (out of scope; see
  README accepted limitations).

## Before enabling for a customer (operational, not code)
The OIDC flow must be verified against a real IdP; this cannot be proven by the
offline tests alone:
1. Register the app with the IdP (Okta/Azure AD/Google Workspace); set redirect URI.
2. Configure `SsoConnection` (issuer, client id/secret, scopes) for the tenant.
3. Test: login → consent → callback → identity linked to the tenant user.
4. Verify token/nonce validation and that an unlinked identity cannot access
   another tenant.
5. Document token refresh / re-auth behavior for the specific IdP.

## Roadmap notes
- SCIM user/group provisioning and SAML are candidate future work; until then,
  user lifecycle for SSO tenants is manual or via the standard signup/admin paths.

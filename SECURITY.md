# Security policy

This is a personal demonstration project. Please report a security problem privately:
**Security** tab → **Report a vulnerability**. Please don't open a public issue.
I aim to reply within 7 days.

## What's in place

- HMAC-SHA256 request signing, five-minute freshness window, single-use nonces, IP allow-list
- Allow-listed, read-only functions; SQL guard at load; READ ONLY transactions; SELECT-only database role
- Append-only audit log (database trigger), fail-closed: no audit record, no data
- Rate limits per key, body size limits, statement timeouts, generic error messages
- Pinned dependencies, pip-audit, Dependabot, CodeQL, gitleaks, a non-root container with a read-only filesystem

## Not in scope for this demo

- TLS termination: put a reverse proxy or load balancer with TLS in front, and set
  `TRUST_PROXY_HEADERS=true` only when that proxy is the sole way in.
- Secret storage: keep `CLIENT_KEYS` in a secrets manager (for example AWS Secrets Manager) in production.

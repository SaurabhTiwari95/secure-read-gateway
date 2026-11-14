# Changelog

## 0.1.0 (2026-11-09)

- Signed read API for AI agents: HMAC + nonce + timestamp, IP allow-list, rate limits.
- Function allow-list stored in the database, SQL guard at load, typed parameters, paging.
- PostgreSQL with a least-privilege role, READ ONLY transactions, append-only audit log.
- Redis-backed nonces and rate limits for multiple instances; in-memory fallback for one.
- Docker image (multi-stage, non-root), docker compose stack, sample data generator.
- Python client, example agent, load test, CI (lint, tests, integration, CodeQL, pip-audit, gitleaks).

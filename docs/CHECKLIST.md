# Release checklist

The same checklist every one of my projects meets before publishing. Ticked only when verified;
anything checked only by CI (not yet run) says so.

Last checked: 2026-11-09

## A. Installation and usage

- [x] What it is, with a diagram
- [x] Prerequisites with versions and links
- [x] Install steps for Windows (PowerShell) and macOS/Linux
- [x] Usage with expected output: example agent, Python client, HTTP contract, error table
- [x] One command for tests (`pytest`); integration test steps documented
- [x] Configuration table for every environment variable
- [x] Troubleshooting
- [x] Design decisions, how it scales, performance, what's next
- [x] AI-assisted line
- [x] Relative links and in-page anchors checked (0 broken); external links checked in CI by lychee
- [ ] The Docker install steps run end to end: **not run on the build machine (no Docker there)**. CI runs the full `docker compose` stack plus the example agent on every push

## B. No private data

- [x] Sample data only: made-up "Acme Traders", company names from Faker, emails on `example.com`
- [x] No employer, product or people names; no real hosts, IPs, account ids or keys
- [x] No work code, names, counts or costs
- [x] Pre-publish check: 0 hits locally against the private block-list; it also runs in CI
- [ ] gitleaks: CI only (not installed on the build machine)

## C. Secure by default

- [x] Secrets only from the environment; `.env` git-ignored; `scripts/make_env.py` generates random ones
- [x] Weak or placeholder secrets refused at start-up (tested)
- [x] Input validated at the edge: Pydantic models, typed params with bounds, body size limit
- [x] Parameterised SQL only (`$1..$n`); no string-built queries with user input
- [x] Least privilege: SELECT-only role, READ ONLY transactions, statement timeout
- [x] Rate limits per key; secure headers; no CORS (server-to-server API); docs off by default
- [x] Errors never echo input or internals (tested); logs carry no secrets or parameter values
- [x] Dependencies pinned to current releases; **pip-audit: no known vulnerabilities** (run 2026-11-09, after upgrading off an older Starlette with 7 advisories)
- [x] Dependabot (pip, Actions, Docker); CodeQL workflow; gitleaks in CI
- [x] Docker: multi-stage, non-root (uid 10001), read-only filesystem, all capabilities dropped, no-new-privileges, ports bound to 127.0.0.1
- [x] `SECURITY.md`
- [ ] CodeQL results: first run happens on GitHub after the push
- [ ] GitHub Actions pinned to commit SHAs: pinned to major versions; Dependabot keeps them current
- [ ] Repo settings (on GitHub after the push): secret scanning + push protection, private vulnerability reporting, branch protection on `main` requiring CI

## D. Scalable

- [x] Stateless API; nonces and rate limits in Redis, shared across instances (integration-tested in CI)
- [x] 12-factor config from the environment
- [x] Connection pooling (capped); plain SQL migrations with a runner and an advisory lock
- [x] Indexes on every filter the shipped functions use
- [x] Paging on every call, capped per function and globally
- [x] `/healthz` and `/readyz`
- [x] "How it scales" section: first bottlenecks at 10× and 100× and their fixes
- N/A Background jobs: requests are short reads; the scaling section names when audit writes should move to a queue

## E. Optimised

- [x] Async I/O end to end (asyncpg, redis.asyncio)
- [x] Function list cached with TTL; single reload under a lock
- [x] No N+1: one query per call; balances use correlated aggregates in one statement
- [x] Multi-stage Docker image
- [x] Profiled; replaced the generic JSON encoder (about 2× throughput in-process)
- [x] Load test with numbers in the README (in-process). End-to-end numbers need the Docker stack

## F. Repo basics

- [x] Tests: 108 passed locally, 94% coverage (gate 80% in CI); 6 integration tests run in CI against PostgreSQL and Redis
- [x] Lint: ruff check and ruff format clean
- [x] Markdown: core rules checked locally (0 issues); full markdownlint runs in CI
- [x] `.gitignore`, `LICENSE` (MIT), `CHANGELOG.md`, `docker-compose.yml`

"""End-to-end tests against real PostgreSQL and Redis.

Run in CI (see .github/workflows/ci.yml), or locally after `docker compose up -d db redis migrate`:
    INTEGRATION=1 ADMIN_DATABASE_URL=... DATABASE_URL=... REDIS_URL=... pytest tests/integration
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from client import AsyncGatewayClient, GatewayError

pytestmark = pytest.mark.skipif(not os.environ.get("INTEGRATION"), reason="set INTEGRATION=1 to run")

KEY_ID, SECRET = "it-agent", "integration-secret-" + "q" * 32


@pytest.fixture
async def client():
    settings = Settings(_env_file=None, client_keys={KEY_ID: SECRET}, allowed_networks=["127.0.0.1/32"])
    app = create_app(settings)  # real PostgresRepository and Redis stores from the environment
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with AsyncGatewayClient("http://gateway.test", KEY_ID, SECRET, transport=transport) as c:
            yield c


@pytest.fixture
async def admin():
    import asyncpg

    conn = await asyncpg.connect(os.environ["ADMIN_DATABASE_URL"])
    yield conn
    await conn.close()


async def test_refused_function_is_not_listed(client):
    names = {f["name"] for f in await client.functions()}
    assert names == {"customer_balance", "list_customers", "list_overdue_invoices", "sales_by_month", "top_products"}


async def test_real_queries_page_and_type_correctly(client):
    first = await client.call("list_customers", page_size=10)
    second = await client.call("list_customers", page=2, page_size=10)
    assert len(first["rows"]) == 10 and first["has_more"]
    assert first["rows"][-1]["id"] < second["rows"][0]["id"]

    overdue = await client.call("list_overdue_invoices", {"as_of": dt.date.today().isoformat(), "min_amount": "100"})
    for row in overdue["rows"]:
        assert row["days_overdue"] > 0 and float(row["total"]) >= 100

    year = dt.date.today().year
    months = await client.call("sales_by_month", {"year": year})
    assert 1 <= len(months["rows"]) <= 12

    balance = await client.call("customer_balance", {"customer_id": 1})
    assert balance["rows"][0]["id"] == 1


async def test_calls_are_audited_and_the_log_is_append_only(client, admin):
    result = await client.call("top_products", {"since": "2020-01-01"})
    row = await admin.fetchrow("SELECT * FROM gateway.audit_log WHERE request_id = $1", result["request_id"])
    assert row["status"] == "ok" and row["key_id"] == KEY_ID
    import asyncpg

    with pytest.raises(asyncpg.exceptions.RaiseError):
        await admin.execute("DELETE FROM gateway.audit_log WHERE id = $1", row["id"])


async def test_app_role_cannot_write_even_if_the_guard_were_bypassed():
    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute("UPDATE acme.invoices SET total = 0 WHERE id = 1")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute("UPDATE gateway.functions SET enabled = true")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("SELECT * FROM gateway.audit_log")
    finally:
        await conn.close()


async def test_redis_stores_share_state_across_instances():
    from redis.asyncio import Redis

    from app.stores import RedisNonceStore, RedisRateLimiter

    redis = Redis.from_url(os.environ["REDIS_URL"])
    try:
        a, b = RedisNonceStore(redis), RedisNonceStore(redis)
        key = uuid.uuid4().hex
        assert await a.claim(key, 30) is True
        assert await b.claim(key, 30) is False  # a second gateway instance sees the same nonce
        limiter = RedisRateLimiter(redis)
        rk = uuid.uuid4().hex
        assert [await limiter.hit(rk, 2, 60) for _ in range(3)] == [True, True, False]
        assert await limiter.ping()
    finally:
        await redis.aclose()


async def test_validation_error_from_real_stack(client):
    with pytest.raises(GatewayError) as err:
        await client.call("sales_by_month", {"year": 1900})
    assert err.value.status == 422

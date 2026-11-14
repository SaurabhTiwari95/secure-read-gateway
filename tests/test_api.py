from __future__ import annotations

import json
import time

import httpx
import pytest

from app import signing
from app.db import QueryTimeout
from app.main import SECURITY_HEADERS, create_app
from app.stores import MemoryNonceStore, MemoryRateLimiter
from client import AsyncGatewayClient, GatewayError
from tests.conftest import KEY_ID, SECRET
from tests.fakes import FakeRepository


def signed(method: str, path: str, body: bytes = b"", **overrides) -> dict[str, str]:
    headers = signing.signed_headers(KEY_ID, SECRET, method, path, body)
    headers.update(overrides)
    return headers


# --- health -------------------------------------------------------------------------------------


async def test_healthz_is_public_and_has_security_headers(raw):
    r = await raw.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    for name, value in SECURITY_HEADERS.items():
        assert r.headers[name] == value
    assert len(r.headers["X-Request-Id"]) == 32


async def test_readyz_reports_dependencies(raw, repo):
    assert (await raw.get("/readyz")).json() == {"status": "ready", "database": True, "store": True}
    repo.healthy = False
    r = await raw.get("/readyz")
    assert r.status_code == 503 and r.json()["database"] is False


async def test_docs_are_off_by_default(raw):
    assert (await raw.get("/docs")).status_code == 404
    assert (await raw.get("/openapi.json")).status_code == 404


# --- happy path ---------------------------------------------------------------------------------


async def test_list_functions_hides_refused_ones(client):
    names = [f["name"] for f in await client.functions()]
    assert names == ["list_customers", "list_overdue_invoices"]  # purge_old_invoices refused


async def test_call_returns_first_page_and_has_more(client, repo):
    result = await client.call("list_customers", {"city": "Pune"})
    assert result["page_size"] == 3  # capped by the function's max_rows
    assert [r["id"] for r in result["rows"]] == [1, 2, 3]
    assert result["has_more"] is True
    sql, args = repo.queries[-1]
    assert sql.endswith("LIMIT $2 OFFSET $3")
    assert args == ["Pune", 4, 0]  # one extra row fetched to detect has_more


async def test_last_page_has_no_more(client):
    result = await client.call("list_customers", page=3)
    assert [r["id"] for r in result["rows"]] == [7]
    assert result["has_more"] is False


async def test_decimals_are_returned_as_strings_not_floats(client, repo):
    from decimal import Decimal

    repo.rows = [{"id": 1, "total": Decimal("1234.50")}]
    result = await client.call("list_overdue_invoices", {"as_of": "2026-01-31"})
    assert result["rows"] == [{"id": 1, "total": "1234.50"}]


async def test_row_values_are_converted_to_json(client, repo):
    import datetime as dt
    import uuid

    uid = uuid.UUID(int=1)
    repo.rows = [{"d": dt.date(2026, 1, 2), "ts": dt.datetime(2026, 1, 2, 3, 4), "gap": dt.timedelta(days=1), "u": uid}]
    row = (await client.call("list_customers"))["rows"][0]
    assert row == {"d": "2026-01-02", "ts": "2026-01-02T03:04:00", "gap": 86400.0, "u": str(uid)}


async def test_every_call_is_audited(client, repo):
    await client.call("list_customers")
    entry = repo.audit[-1]
    assert (entry.key_id, entry.function_name, entry.status, entry.row_count) == (KEY_ID, "list_customers", "ok", 3)
    assert entry.client_ip == "127.0.0.1"


# --- refusals -----------------------------------------------------------------------------------


async def test_unsigned_request_is_refused(raw):
    r = await raw.post("/v1/call/list_customers", json={})
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"


async def test_unknown_key_is_refused(raw):
    r = await raw.get("/v1/functions", headers=signed("GET", "/v1/functions", **{signing.HEADER_KEY_ID: "nobody"}))
    assert r.status_code == 401


async def test_tampered_body_is_refused(raw):
    path = "/v1/call/list_customers"
    headers = signed("POST", path, b'{"params":{}}')
    r = await raw.post(path, content=b'{"params":{"city":"X"}}', headers=headers)
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"


async def test_stale_request_is_refused(raw):
    path = "/v1/functions"
    headers = signing.signed_headers(KEY_ID, SECRET, "GET", path, b"", now=time.time() - 3_600)
    r = await raw.get(path, headers=headers)
    assert r.status_code == 401 and r.json()["error"] == "stale_request"


async def test_replayed_request_is_refused(raw):
    path = "/v1/functions"
    headers = signed("GET", path)
    assert (await raw.get(path, headers=headers)).status_code == 200
    r = await raw.get(path, headers=headers)
    assert r.status_code == 401 and r.json()["error"] == "replayed_request"


async def test_bad_nonce_format_is_refused(raw):
    path = "/v1/functions"
    r = await raw.get(path, headers=signed("GET", path, **{signing.HEADER_NONCE: "short"}))
    assert r.status_code == 401


async def test_caller_outside_allowed_networks_is_refused(app):
    transport = httpx.ASGITransport(app=app, client=("192.0.2.50", 4000))
    async with AsyncGatewayClient("http://gateway.test", KEY_ID, SECRET, transport=transport) as c:
        with pytest.raises(GatewayError) as err:
            await c.functions()
    assert err.value.status == 403


async def test_proxy_header_used_only_when_trusted(settings, repo):
    settings.trust_proxy_headers = True
    settings.allowed_networks = ["198.51.100.0/24"]
    app = create_app(settings, repo=repo, nonces=MemoryNonceStore(), limiter=MemoryRateLimiter())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as raw:
            ok = signed("GET", "/v1/functions")
            ok["X-Forwarded-For"] = "203.0.113.9, 198.51.100.7"  # right-most = added by our proxy
            assert (await raw.get("/v1/functions", headers=ok)).status_code == 200
            spoof = signed("GET", "/v1/functions")
            spoof["X-Forwarded-For"] = "198.51.100.7, 203.0.113.9"
            assert (await raw.get("/v1/functions", headers=spoof)).status_code == 403
            garbage = signed("GET", "/v1/functions")
            garbage["X-Forwarded-For"] = "not-an-ip"
            assert (await raw.get("/v1/functions", headers=garbage)).status_code == 403


async def test_rate_limit(settings, repo):
    settings.rate_limit_per_minute = 2
    app = create_app(settings, repo=repo, nonces=MemoryNonceStore(), limiter=MemoryRateLimiter())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with AsyncGatewayClient("http://gateway.test", KEY_ID, SECRET, transport=transport) as c:
            await c.functions()
            await c.functions()
            with pytest.raises(GatewayError) as err:
                await c.functions()
    assert err.value.status == 429 and err.value.body["error"] == "rate_limited"


async def test_oversized_body_is_refused(raw, settings):
    path = "/v1/call/list_customers"
    body = json.dumps({"params": {"city": "x" * (settings.max_body_bytes + 10)}}).encode()
    r = await raw.post(path, content=body, headers=signed("POST", path, body))
    assert r.status_code == 413


async def test_chunked_oversized_body_is_refused(raw, settings):
    path = "/v1/call/list_customers"
    body = b"{" + b" " * (settings.max_body_bytes + 10) + b"}"

    async def chunks():
        yield body[:100]
        yield body[100:]

    r = await raw.post(path, content=chunks(), headers=signed("POST", path, body))
    assert r.status_code == 413


async def test_unknown_function_is_404_and_audited(client, repo):
    for name in ["no_such_function", "purge_old_invoices", "Bad-Name!"]:
        with pytest.raises(GatewayError) as err:
            await client.call(name)
        assert err.value.status == 404
    assert [e.status for e in repo.audit] == ["unknown_function"] * 3
    assert repo.queries == []  # nothing reached the database


async def test_invalid_params_never_echo_input(client):
    with pytest.raises(GatewayError) as err:
        await client.call("list_overdue_invoices", {"as_of": "not-a-date-SECRET", "extra": 1})
    assert err.value.status == 422 and err.value.body["error"] == "invalid_params"
    assert "SECRET" not in json.dumps(err.value.body)
    assert {d["param"] for d in err.value.body["details"]} == {"as_of", "extra"}


async def test_invalid_request_body(raw):
    path = "/v1/call/list_customers"
    for body in [b"not json", b'{"page": 0}', b'{"unexpected": true}']:
        r = await raw.post(path, content=body, headers=signed("POST", path, body))
        assert r.status_code == 422 and r.json()["error"] == "invalid_request"


async def test_query_timeout_is_504(client, repo):
    repo.fail_with = QueryTimeout()
    with pytest.raises(GatewayError) as err:
        await client.call("list_customers")
    assert err.value.status == 504 and repo.audit[-1].status == "timeout"


async def test_database_error_is_generic_500(client, repo):
    repo.fail_with = RuntimeError("relation acme.secret_table does not exist")
    with pytest.raises(GatewayError) as err:
        await client.call("list_customers")
    assert err.value.status == 500
    assert err.value.body["error"] == "internal_error"
    assert "secret_table" not in json.dumps(err.value.body)


async def test_no_audit_no_data(client, repo):
    repo.audit_fails = True
    with pytest.raises(GatewayError) as err:
        await client.call("list_customers")
    assert err.value.status == 500 and "rows" not in err.value.body


async def test_error_audit_failure_still_returns_the_error(client, repo):
    repo.audit_fails = True
    with pytest.raises(GatewayError) as err:
        await client.call("no_such_function")
    assert err.value.status == 404


# --- lifecycle ----------------------------------------------------------------------------------


async def test_shutdown_closes_the_pool(settings):
    repo = FakeRepository()
    app = create_app(settings, repo=repo)
    async with app.router.lifespan_context(app):
        pass  # in-memory stores chosen because REDIS_URL is empty
    assert repo.closed


async def test_missing_database_url_fails_fast(settings):
    app = create_app(settings)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        async with app.router.lifespan_context(app):
            pass


async def test_empty_client_keys_starts_but_refuses_everyone(settings):
    settings.client_keys = {}
    repo = FakeRepository()
    app = create_app(settings, repo=repo)
    async with app.router.lifespan_context(app):
        async with AsyncGatewayClient(
            "http://gateway.test", KEY_ID, SECRET, transport=httpx.ASGITransport(app=app)
        ) as c:
            with pytest.raises(GatewayError) as err:
                await c.functions()
    assert err.value.status == 401

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from app import signing, sqlguard
from app.config import Settings
from app.registry import ParamError, Registry
from app.stores import MemoryNonceStore, MemoryRateLimiter
from client import GatewayClient, GatewayError
from tests.fakes import MIGRATIONS, FakeRepository, function_specs

# --- signing ------------------------------------------------------------------------------------


def test_sign_and_verify_round_trip():
    sig = signing.sign("k" * 32, "post", "/v1/call/x", "100", "nonce", b"{}")
    assert signing.verify("k" * 32, sig, "POST", "/v1/call/x", "100", "nonce", b"{}")


@pytest.mark.parametrize(
    "change",
    [
        {"secret": "j" * 32},
        {"method": "GET"},
        {"path": "/v1/call/y"},
        {"timestamp": "101"},
        {"nonce": "other"},
        {"body": b'{"a":1}'},
    ],
)
def test_any_change_breaks_the_signature(change):
    base = {"secret": "k" * 32, "method": "POST", "path": "/v1/call/x", "timestamp": "100", "nonce": "n", "body": b"{}"}
    sig = signing.sign(**base)
    assert not signing.verify(signature=sig, **{**base, **change})


def test_signed_headers_have_fresh_nonces():
    a = signing.signed_headers("id", "k" * 32, "GET", "/", b"")
    b = signing.signed_headers("id", "k" * 32, "GET", "/", b"")
    assert a[signing.HEADER_NONCE] != b[signing.HEADER_NONCE]
    assert set(a) == {signing.HEADER_KEY_ID, signing.HEADER_TIMESTAMP, signing.HEADER_NONCE, signing.HEADER_SIGNATURE}


# --- SQL guard ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM acme.customers WHERE city = $1 ORDER BY id",
        "with x as (select 1 as a) select a from x;",
        "SELECT 'please delete me' AS note",  # keyword inside a string literal is fine
        "SELECT $$drop table$$ AS t",
        "SELECT $tag$insert$tag$ AS t",
        "SELECT 1 -- delete in a comment\n",
        "SELECT /* update */ 1",
        "SELECT substring(name for 3) FROM acme.customers",
        'SELECT "offset_days" FROM acme.t',
    ],
)
def test_guard_allows_reads(sql):
    assert sqlguard.check(sql).ok, sqlguard.check(sql).reason


@pytest.mark.parametrize(
    "sql, reason",
    [
        ("", "empty"),
        ("UPDATE acme.invoices SET total = 0", "must start with SELECT or WITH"),
        ("WITH g AS (DELETE FROM acme.invoices RETURNING id) SELECT count(*) FROM g", "delete"),
        ("SELECT 1; DROP TABLE acme.invoices", "more than one statement"),
        ("SELECT pg_sleep(10)", "pg_sleep"),
        ('SELECT "pg_sleep"(10)', "pg_sleep"),
        ("SELECT pg_catalog.pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT query_to_xml('delete from x', true, true, '')", "query_to_xml"),
        ("SELECT * INTO acme.copy FROM acme.invoices", "into"),
        ("SELECT * FROM acme.invoices FOR UPDATE", "locking"),
        ("SELECT * FROM acme.invoices FOR NO KEY UPDATE", "locking"),
        ("SELECT set_config('role', 'admin', false)", "set_config"),
        ("SELECT 1 /* unclosed", "unbalanced comment"),
        ("SELECT nextval('acme.invoices_id_seq')", "nextval"),
    ],
)
def test_guard_refuses_writes_and_side_effects(sql, reason):
    verdict = sqlguard.check(sql)
    assert not verdict.ok and reason in verdict.reason


def test_shipped_migration_functions_pass_or_fail_as_intended():
    text = (MIGRATIONS / "004_functions.sql").read_text(encoding="utf-8")
    bodies = text.split("$q$")[1::2]
    verdicts = [sqlguard.check(b).ok for b in bodies]
    assert verdicts == [True, True, True, True, True, False]  # the last one is the purge demo


# --- registry -----------------------------------------------------------------------------------


async def test_registry_refuses_unsafe_and_invalid_definitions():
    specs = [
        *function_specs(),
        {"name": "Bad Name", "description": "x", "sql": "SELECT 1"},
        {"name": "paged_already", "description": "x", "sql": "SELECT id FROM t ORDER BY id LIMIT 5"},
    ]
    reg = Registry(FakeRepository(specs=specs))
    await reg.load()
    assert [f.spec.name for f in await reg.all()] == ["list_customers", "list_overdue_invoices"]
    assert reg.rejected["purge_old_invoices"].startswith("forbidden keyword")
    assert reg.rejected["Bad Name"] == "invalid definition"
    assert "LIMIT/OFFSET" in reg.rejected["paged_already"]


async def test_registry_binds_and_validates_params():
    reg = Registry(FakeRepository())
    fn = await reg.get("list_overdue_invoices")
    assert fn.bind({"as_of": "2026-03-01"}) == [dt.date(2026, 3, 1), Decimal("0")]
    assert fn.bind({"as_of": "2026-03-01", "min_amount": "10.5"}) == [dt.date(2026, 3, 1), Decimal("10.5")]
    for bad in [{}, {"as_of": "x"}, {"as_of": "2026-03-01", "min_amount": -1}, {"as_of": "2026-03-01", "x": 1}]:
        with pytest.raises(ParamError):
            fn.bind(bad)
    customers = await reg.get("list_customers")
    assert customers.bind({}) == [None]
    with pytest.raises(ParamError):
        customers.bind({"city": "x" * 81})
    assert customers.describe()["params"][0]["max_length"] == 80
    assert "max_length" not in fn.describe()["params"][0]


async def test_registry_reloads_after_ttl():
    now = [0.0]
    repo = FakeRepository()
    reg = Registry(repo, ttl_seconds=60, clock=lambda: now[0])
    await reg.get("list_customers")
    await reg.get("list_customers")
    assert repo.spec_loads == 1
    now[0] = 61
    repo.specs = repo.specs[1:]  # list_customers removed from the allow-list
    assert await reg.get("list_customers") is None
    assert repo.spec_loads == 2


async def test_int_param_bounds():
    spec = {
        "name": "by_year",
        "description": "x",
        "sql": "SELECT 1 WHERE $1 > 0",
        "params": [{"name": "year", "type": "int", "min": 2000, "max": 2100}, {"name": "flag", "type": "bool"}],
    }
    reg = Registry(FakeRepository(specs=[spec]))
    fn = await reg.get("by_year")
    assert fn.bind({"year": 2026, "flag": True}) == [2026, True]
    with pytest.raises(ParamError):
        fn.bind({"year": 1999, "flag": True})


# --- stores -------------------------------------------------------------------------------------


async def test_memory_nonce_store_single_use_until_expiry():
    now = [0.0]
    store = MemoryNonceStore(clock=lambda: now[0])
    assert await store.claim("a", 10) is True
    assert await store.claim("a", 10) is False
    now[0] = 11
    assert await store.claim("a", 10) is True
    assert await store.ping()


async def test_memory_nonce_store_prunes_expired_entries():
    now = [0.0]
    store = MemoryNonceStore(clock=lambda: now[0])
    for i in range(10_001):
        await store.claim(str(i), 1)
    now[0] = 5
    await store.claim("new", 1)
    assert len(store._seen) == 1


async def test_memory_rate_limiter_window():
    now = [0.0]
    limiter = MemoryRateLimiter(clock=lambda: now[0])
    assert [await limiter.hit("k", 2, 60) for _ in range(3)] == [True, True, False]
    now[0] = 60
    assert await limiter.hit("k", 2, 60) is True
    assert await limiter.ping()


# --- config -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_keys": {"a": "short"}},
        {"client_keys": {"a": "replace-with-a-long-random-secret-of-40-chars"}},
        {"client_keys": {"": "x" * 40}},
        {"allowed_networks": ["not-a-network"]},
    ],
)
def test_settings_reject_weak_or_invalid_values(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_settings_parse_json_from_environment(monkeypatch):
    monkeypatch.setenv("CLIENT_KEYS", json.dumps({"agent": "y" * 40}))
    monkeypatch.setenv("ALLOWED_NETWORKS", '["198.51.100.0/24"]')
    s = Settings(_env_file=None)
    assert s.client_keys == {"agent": "y" * 40}
    assert str(s.networks()[0]) == "198.51.100.0/24"


# --- sync client --------------------------------------------------------------------------------


def test_sync_client_signs_requests_and_raises_on_errors():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        h = request.headers
        seen["valid"] = signing.verify(
            "z" * 40,
            h[signing.HEADER_SIGNATURE],
            request.method,
            request.url.path,
            h[signing.HEADER_TIMESTAMP],
            h[signing.HEADER_NONCE],
            request.content,
        )
        if request.url.path.endswith("missing"):
            return httpx.Response(404, json={"error": "unknown_function"})
        if request.url.path == "/v1/functions":
            return httpx.Response(200, json={"functions": [{"name": "f"}]})
        return httpx.Response(200, json={"rows": [], "body": json.loads(request.content)})

    with GatewayClient("http://gw", "agent", "z" * 40, transport=httpx.MockTransport(handler)) as gw:
        assert gw.functions() == [{"name": "f"}]
        result = gw.call("f", {"d": dt.date(2026, 1, 2)}, page=2, page_size=5)
        assert result["body"] == {"params": {"d": "2026-01-02"}, "page": 2, "page_size": 5}
        assert seen["valid"]
        with pytest.raises(GatewayError) as err:
            gw.call("missing")
        assert err.value.status == 404


def test_client_handles_non_json_errors():
    transport = httpx.MockTransport(lambda r: httpx.Response(502, text="bad gateway"))
    with GatewayClient("http://gw", "agent", "z" * 40, transport=transport) as gw:
        with pytest.raises(GatewayError) as err:
            gw.functions()
    assert err.value.body == {"error": "non_json_response"}

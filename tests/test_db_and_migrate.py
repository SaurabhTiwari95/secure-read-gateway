"""PostgresRepository and the migration runner, against small fakes of asyncpg's pool/connection.
The real database is exercised by tests/integration (run in CI with Postgres and Redis)."""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app import migrate
from app.db import AuditEntry, PostgresRepository


class FakeConn:
    def __init__(self, rows=None, fetchval=None, fail=None):
        self.rows = rows or []
        self._fetchval = fetchval
        self.fail = fail
        self.executed: list[tuple] = []
        self.transactions: list[dict] = []
        self.copies: list[str] = []

    async def fetch(self, sql, *args):
        if self.fail:
            raise self.fail
        self.executed.append((sql, args))
        return self.rows

    async def fetchval(self, sql, *args):
        self.executed.append((sql, args))
        return self._fetchval(sql, args) if callable(self._fetchval) else self._fetchval

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def copy_records_to_table(self, table, **kwargs):
        self.copies.append(table)

    @asynccontextmanager
    async def _tx(self, **kwargs):
        self.transactions.append(kwargs)
        yield

    def transaction(self, **kwargs):
        return self._tx(**kwargs)


class FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.closed = False

    @asynccontextmanager
    async def _acquire(self):
        yield self.conn

    def acquire(self):
        return self._acquire()

    async def close(self):
        self.closed = True


async def test_run_read_uses_a_read_only_transaction():
    conn = FakeConn(rows=[{"id": 1}])
    repo = PostgresRepository(FakePool(conn))
    assert await repo.run_read("SELECT $1", [5]) == [{"id": 1}]
    assert conn.transactions == [{"readonly": True}]
    assert conn.executed[-1] == ("SELECT $1", (5,))


async def test_query_cancel_becomes_query_timeout():
    asyncpg = pytest.importorskip("asyncpg")
    from app.db import QueryTimeout

    repo = PostgresRepository(FakePool(FakeConn(fail=asyncpg.exceptions.QueryCanceledError("timeout"))))
    with pytest.raises(QueryTimeout):
        await repo.run_read("SELECT 1", [])


async def test_function_specs_decode_json_params():
    conn = FakeConn(rows=[{"name": "f", "params": '[{"name": "a", "type": "int"}]'}, {"name": "g", "params": []}])
    specs = await PostgresRepository(FakePool(conn)).fetch_function_specs()
    assert specs[0]["params"] == [{"name": "a", "type": "int"}] and specs[1]["params"] == []


async def test_audit_ping_and_close():
    conn = FakeConn(fetchval=1)
    pool = FakePool(conn)
    repo = PostgresRepository(pool)
    await repo.write_audit(AuditEntry("rid", "key", "fn", "ok", 3, 12, "127.0.0.1"))
    assert conn.executed[-1][1] == ("rid", "key", "fn", "ok", 3, 12, "127.0.0.1")
    assert await repo.ping() is True
    await repo.close()
    assert pool.closed


async def test_ping_is_false_when_the_database_is_down():
    class DownPool(FakePool):
        def acquire(self):
            raise OSError("connection refused")

    assert await PostgresRepository(DownPool(FakeConn())).ping() is False


# --- migrate ------------------------------------------------------------------------------------


def test_sample_data_is_deterministic_and_consistent():
    today = dt.date(2026, 6, 30)
    a = migrate.generate_sample_data(customers=20, products=10, invoices=50, seed=7, today=today)
    b = migrate.generate_sample_data(customers=20, products=10, invoices=50, seed=7, today=today)
    assert a == b
    assert len(a.customers) == 20 and len(a.products) == 10 and len(a.invoices) == 50
    assert all(c[2].endswith(".example.com") for c in a.customers)  # no real email domains
    totals = {}
    for _, invoice_id, _, qty, price in a.lines:
        totals[invoice_id] = totals.get(invoice_id, 0) + qty * price
    assert all(totals[i[0]] == i[5] for i in a.invoices)
    paid = {i[0] for i in a.invoices if i[4] == "paid"}
    assert {p[1] for p in a.payments} == paid
    assert all(p[2] <= today for p in a.payments)


async def test_ensure_app_role_creates_then_alters_with_quoted_password():
    conn = FakeConn(fetchval=lambda sql, args: None if "pg_roles" in sql else "CREATE ROLE ... PASSWORD 'p'")
    await migrate.ensure_app_role(conn, "p" * 20)
    format_sql, format_args = conn.executed[1]
    assert "CREATE ROLE gateway_app" in format_sql and "%L" in format_sql and format_args == ("p" * 20,)

    conn = FakeConn(fetchval=lambda sql, args: 1 if "pg_roles" in sql else "ALTER ROLE ...")
    await migrate.ensure_app_role(conn, "p" * 20)
    assert "ALTER ROLE gateway_app" in conn.executed[1][0]

    with pytest.raises(ValueError):
        await migrate.ensure_app_role(FakeConn(), "short")


async def test_apply_migrations_skips_applied_files(tmp_path: Path):
    (tmp_path / "001_a.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "002_b.sql").write_text("SELECT 2", encoding="utf-8")
    conn = FakeConn(rows=[{"version": "001_a.sql"}])
    assert await migrate.apply_migrations(conn, tmp_path) == ["002_b.sql"]
    assert ("SELECT 2", ()) in conn.executed


async def test_seed_skips_when_data_exists_and_loads_otherwise():
    data = migrate.generate_sample_data(customers=3, products=3, invoices=3, seed=1)
    assert await migrate.seed(FakeConn(fetchval=5), data) is False
    conn = FakeConn(fetchval=0)
    assert await migrate.seed(conn, data) is True
    assert conn.copies == ["customers", "products", "invoices", "invoice_lines", "payments"]


def test_main_needs_environment(monkeypatch, capsys):
    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_DB_PASSWORD", raising=False)
    assert migrate.main([]) == 2
    assert "must be set" in capsys.readouterr().err

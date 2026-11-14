"""Database access: a pooled, least-privilege PostgreSQL connection.

Every agent query runs in a READ ONLY transaction with a statement timeout, using positional
parameters ($1..$n) only. The app role can SELECT business tables, read the function registry and
INSERT into the audit log; nothing else (see migrations/003_grants.sql).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol


class QueryTimeout(Exception):
    """The database cancelled a query that ran past the statement timeout."""


@dataclass(frozen=True)
class AuditEntry:
    request_id: str
    key_id: str
    function_name: str
    status: str
    row_count: int | None
    duration_ms: int
    client_ip: str | None


class Repository(Protocol):
    async def fetch_function_specs(self) -> list[dict[str, Any]]: ...
    async def run_read(self, sql: str, args: list[Any]) -> list[dict[str, Any]]: ...
    async def write_audit(self, entry: AuditEntry) -> None: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


_SPECS_SQL = """
SELECT name, description, sql, params, max_rows
FROM gateway.functions
WHERE enabled
ORDER BY name
"""

_AUDIT_SQL = """
INSERT INTO gateway.audit_log
    (request_id, key_id, function_name, status, row_count, duration_ms, client_ip)
VALUES ($1, $2, $3, $4, $5, $6, $7::inet)
"""


class PostgresRepository:
    def __init__(self, pool):
        self._pool = pool

    @classmethod
    async def create(cls, dsn: str, min_size: int, max_size: int, statement_timeout_ms: int):
        import asyncpg  # imported here so unit tests don't need the driver

        pool = await asyncpg.create_pool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            command_timeout=statement_timeout_ms / 1000 + 1,
            server_settings={
                "statement_timeout": str(statement_timeout_ms),
                "application_name": "secure-read-gateway",
            },
        )
        return cls(pool)

    async def fetch_function_specs(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_SPECS_SQL)
        specs = []
        for r in rows:
            spec = dict(r)
            if isinstance(spec.get("params"), str):
                spec["params"] = json.loads(spec["params"])
            specs.append(spec)
        return specs

    async def run_read(self, sql: str, args: list[Any]) -> list[dict[str, Any]]:
        import asyncpg

        try:
            async with self._pool.acquire() as conn, conn.transaction(readonly=True):
                rows = await conn.fetch(sql, *args)
        except asyncpg.exceptions.QueryCanceledError as exc:
            raise QueryTimeout() from exc
        return [dict(r) for r in rows]

    async def write_audit(self, entry: AuditEntry) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                _AUDIT_SQL,
                entry.request_id,
                entry.key_id,
                entry.function_name,
                entry.status,
                entry.row_count,
                entry.duration_ms,
                entry.client_ip,
            )

    async def ping(self) -> bool:
        try:
            async with self._pool.acquire() as conn:
                return await conn.fetchval("SELECT 1") == 1
        except Exception:
            return False

    async def close(self) -> None:
        await self._pool.close()

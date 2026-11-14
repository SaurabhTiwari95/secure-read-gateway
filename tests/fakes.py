"""In-memory stand-ins for the database, used by the unit and API tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.db import AuditEntry, QueryTimeout

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def function_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "list_customers",
            "description": "Customers, optionally by city.",
            "sql": "SELECT id, name, city FROM acme.customers WHERE ($1::text IS NULL OR city = $1) ORDER BY id",
            "params": [{"name": "city", "type": "str", "required": False, "max_length": 80}],
            "max_rows": 3,
        },
        {
            "name": "list_overdue_invoices",
            "description": "Open invoices past due.",
            "sql": "SELECT id, total FROM acme.invoices WHERE due_on < $1::date AND total >= $2 ORDER BY id",
            "params": [
                {"name": "as_of", "type": "date"},
                {"name": "min_amount", "type": "decimal", "required": False, "default": "0", "min": 0},
            ],
            "max_rows": 100,
        },
        {
            "name": "purge_old_invoices",
            "description": "Unsafe; must be refused.",
            "sql": "WITH gone AS (DELETE FROM acme.invoices RETURNING id) SELECT count(*) FROM gone",
            "params": [],
            "max_rows": 1,
        },
    ]


class FakeRepository:
    def __init__(self, specs: list[dict[str, Any]] | None = None, rows: list[dict[str, Any]] | None = None):
        self.specs = function_specs() if specs is None else specs
        self.rows = (
            rows if rows is not None else [{"id": i, "name": f"Customer {i}", "city": "Pune"} for i in range(1, 8)]
        )
        self.queries: list[tuple[str, list[Any]]] = []
        self.audit: list[AuditEntry] = []
        self.fail_with: Exception | None = None
        self.audit_fails = False
        self.healthy = True
        self.closed = False
        self.spec_loads = 0

    async def fetch_function_specs(self) -> list[dict[str, Any]]:
        self.spec_loads += 1
        return self.specs

    async def run_read(self, sql: str, args: list[Any]) -> list[dict[str, Any]]:
        self.queries.append((sql, args))
        if self.fail_with is not None:
            raise self.fail_with
        limit, offset = args[-2], args[-1]
        return self.rows[offset : offset + limit]

    async def write_audit(self, entry: AuditEntry) -> None:
        if self.audit_fails:
            raise RuntimeError("audit table unavailable")
        self.audit.append(entry)

    async def ping(self) -> bool:
        return self.healthy

    async def close(self) -> None:
        self.closed = True


__all__ = ["FakeRepository", "QueryTimeout", "function_specs", "MIGRATIONS"]

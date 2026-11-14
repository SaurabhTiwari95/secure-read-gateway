"""Apply SQL migrations, create the least-privilege app role, and (optionally) seed sample data.

Runs as the database admin, once per deploy, before the API starts:

    python -m app.migrate --seed

Needs ADMIN_DATABASE_URL and APP_DB_PASSWORD in the environment. Safe to run repeatedly: applied
migrations are recorded, and seeding is skipped when data already exists.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import random
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
APP_ROLE = "gateway_app"
_LOCK_ID = 72_734_101  # advisory lock so two runners never migrate at once
_PRODUCT_NOUNS = ["Bolt", "Bracket", "Hinge", "Valve", "Gasket", "Bearing", "Clamp", "Fitting", "Spring", "Panel"]


@dataclass
class SampleData:
    customers: list[tuple] = field(default_factory=list)
    products: list[tuple] = field(default_factory=list)
    invoices: list[tuple] = field(default_factory=list)
    lines: list[tuple] = field(default_factory=list)
    payments: list[tuple] = field(default_factory=list)


def generate_sample_data(
    customers: int = 500, products: int = 120, invoices: int = 6_000, seed: int = 42, today: dt.date | None = None
) -> SampleData:
    """Made-up 'Acme Traders' data. Company names only; emails use example.com."""
    from faker import Faker

    fake = Faker("en_IN")
    Faker.seed(seed)
    rnd = random.Random(seed)
    today = today or dt.date.today()
    data = SampleData()

    for cid in range(1, customers + 1):
        name = fake.company()
        slug = "".join(ch for ch in name.lower() if ch.isalnum())[:20] or f"customer{cid}"
        data.customers.append(
            (cid, name, f"accounts@{slug}.example.com", fake.city(), today - dt.timedelta(days=rnd.randint(30, 1500)))
        )

    prices: dict[int, Decimal] = {}
    for pid in range(1, products + 1):
        price = Decimal(rnd.randint(50, 50_000)) / 100
        prices[pid] = price
        data.products.append((pid, f"SKU-{pid:05d}", f"{fake.color_name()} {rnd.choice(_PRODUCT_NOUNS)}", price))

    line_id = payment_id = 0
    for iid in range(1, invoices + 1):
        issued = today - dt.timedelta(days=rnd.randint(0, 730))
        due = issued + dt.timedelta(days=rnd.choice([15, 30, 45, 60]))
        status = rnd.choices(["paid", "open", "void"], weights=[65, 30, 5])[0]
        total = Decimal("0")
        for pid in rnd.sample(range(1, products + 1), k=min(products, rnd.randint(1, 5))):
            qty = rnd.randint(1, 40)
            line_id += 1
            data.lines.append((line_id, iid, pid, qty, prices[pid]))
            total += prices[pid] * qty
        data.invoices.append((iid, rnd.randint(1, customers), issued, due, status, total))
        if status == "paid":
            payment_id += 1
            paid_on = min(today, issued + dt.timedelta(days=rnd.randint(0, 60)))
            data.payments.append((payment_id, iid, paid_on, total))
    return data


async def ensure_app_role(conn, password: str) -> None:
    if len(password) < 16:
        raise ValueError("APP_DB_PASSWORD must be at least 16 characters")
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", APP_ROLE)
    verb = "ALTER" if exists else "CREATE"
    # format(%L) quotes the password safely; it never touches string concatenation.
    statement = await conn.fetchval(
        f"SELECT format('{verb} ROLE {APP_ROLE} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', $1::text)",
        password,
    )
    await conn.execute(statement)


def read_migration_files(directory: Path) -> list[tuple[str, str]]:
    return [(p.name, p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.sql"))]


async def apply_migrations(conn, directory: Path = MIGRATIONS_DIR) -> list[str]:
    files = await asyncio.to_thread(read_migration_files, directory)  # file I/O off the event loop
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS public.schema_migrations "
        "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    applied = {r["version"] for r in await conn.fetch("SELECT version FROM public.schema_migrations")}
    done = []
    for name, sql in files:
        if name in applied:
            continue
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute("INSERT INTO public.schema_migrations (version) VALUES ($1)", name)
        done.append(name)
        print(f"applied {name}")
    return done


async def seed(conn, data: SampleData) -> bool:
    if await conn.fetchval("SELECT count(*) FROM acme.customers"):
        print("seed skipped: data already present")
        return False
    async with conn.transaction():
        await conn.copy_records_to_table(
            "customers", schema_name="acme", records=data.customers,
            columns=["id", "name", "email", "city", "created_on"],
        )  # fmt: skip
        await conn.copy_records_to_table(
            "products", schema_name="acme", records=data.products, columns=["id", "sku", "name", "unit_price"]
        )
        await conn.copy_records_to_table(
            "invoices", schema_name="acme", records=data.invoices,
            columns=["id", "customer_id", "issued_on", "due_on", "status", "total"],
        )  # fmt: skip
        await conn.copy_records_to_table(
            "invoice_lines", schema_name="acme", records=data.lines,
            columns=["id", "invoice_id", "product_id", "quantity", "unit_price"],
        )  # fmt: skip
        await conn.copy_records_to_table(
            "payments", schema_name="acme", records=data.payments, columns=["id", "invoice_id", "paid_on", "amount"]
        )
        for table in ("customers", "products", "invoices", "invoice_lines", "payments"):
            await conn.execute(
                f"SELECT setval(pg_get_serial_sequence('acme.{table}', 'id'), (SELECT max(id) FROM acme.{table}))"
            )
        await conn.execute("ANALYZE acme.customers, acme.products, acme.invoices, acme.invoice_lines, acme.payments")
    print(
        f"seeded {len(data.customers)} customers, {len(data.products)} products, "
        f"{len(data.invoices)} invoices, {len(data.lines)} lines, {len(data.payments)} payments"
    )
    return True


async def run(admin_dsn: str, app_password: str, with_seed: bool, sizes: dict[str, int]) -> None:
    import asyncpg

    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_ID)
        try:
            await ensure_app_role(conn, app_password)
            await apply_migrations(conn)
            if with_seed:
                await seed(conn, generate_sample_data(**sizes))
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_ID)
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply migrations and seed sample data.")
    parser.add_argument("--seed", action="store_true", help="load made-up sample data if the tables are empty")
    parser.add_argument("--customers", type=int, default=500)
    parser.add_argument("--products", type=int, default=120)
    parser.add_argument("--invoices", type=int, default=6_000)
    args = parser.parse_args(argv)

    admin_dsn, password = os.environ.get("ADMIN_DATABASE_URL"), os.environ.get("APP_DB_PASSWORD")
    if not admin_dsn or not password:
        print("ADMIN_DATABASE_URL and APP_DB_PASSWORD must be set", file=sys.stderr)
        return 2
    sizes = {"customers": args.customers, "products": args.products, "invoices": args.invoices}
    asyncio.run(run(admin_dsn, password, args.seed, sizes))
    return 0


if __name__ == "__main__":
    sys.exit(main())

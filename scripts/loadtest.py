"""Small load test: N concurrent signed calls, then throughput and latency percentiles.

Against a running stack (docker compose up):
    python scripts/loadtest.py --requests 2000 --concurrency 20

Gateway overhead only (no database; fake data, in this process):
    python scripts/loadtest.py --in-process --requests 5000 --concurrency 50
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from client import AsyncGatewayClient, GatewayError  # noqa: E402


async def _worker(client: AsyncGatewayClient, jobs: asyncio.Queue, latencies: list[float], errors: list[str]) -> None:
    while True:
        try:
            jobs.get_nowait()
        except asyncio.QueueEmpty:
            return
        start = time.perf_counter()
        try:
            await client.call("list_customers", page_size=20)
            latencies.append(time.perf_counter() - start)
        except GatewayError as err:
            errors.append(str(err.status))


async def run(args) -> int:
    latencies: list[float] = []
    errors: list[str] = []
    jobs: asyncio.Queue = asyncio.Queue()
    for _ in range(args.requests):
        jobs.put_nowait(1)

    if args.in_process:
        from app.config import Settings
        from app.main import create_app
        from app.stores import MemoryNonceStore, MemoryRateLimiter
        from tests.fakes import FakeRepository

        key_id, secret = "load-agent", "l" * 40
        settings = Settings(_env_file=None, client_keys={key_id: secret}, rate_limit_per_minute=100_000)
        app = create_app(settings, repo=FakeRepository(), nonces=MemoryNonceStore(), limiter=MemoryRateLimiter())
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        kwargs = {"transport": httpx.ASGITransport(app=app)}
        url = "http://gateway.test"
    else:
        key_id, secret = os.environ.get("AGENT_KEY_ID", ""), os.environ.get("AGENT_SECRET", "")
        url = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000")
        kwargs = {"limits": httpx.Limits(max_connections=args.concurrency)}
        lifespan = None
        if not key_id or not secret:
            print("Set AGENT_KEY_ID and AGENT_SECRET", file=sys.stderr)
            return 2

    started = time.perf_counter()
    async with AsyncGatewayClient(url, key_id, secret, **kwargs) as client:
        await asyncio.gather(*[_worker(client, jobs, latencies, errors) for _ in range(args.concurrency)])
    elapsed = time.perf_counter() - started
    if lifespan is not None:
        await lifespan.__aexit__(None, None, None)

    if not latencies:
        print(f"all {len(errors)} requests failed: {sorted(set(errors))}")
        return 1
    q = statistics.quantiles(sorted(latencies), n=100)
    print(f"requests: {len(latencies)} ok, {len(errors)} failed {sorted(set(errors)) if errors else ''}")
    print(f"throughput: {len(latencies) / elapsed:,.0f} req/s over {elapsed:.1f}s, concurrency {args.concurrency}")
    print(f"latency ms: p50 {q[49] * 1000:.1f}  p95 {q[94] * 1000:.1f}  p99 {q[98] * 1000:.1f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requests", type=int, default=1_000)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--in-process", action="store_true", help="no network or database: gateway overhead only")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

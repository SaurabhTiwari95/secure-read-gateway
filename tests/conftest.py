from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.stores import MemoryNonceStore, MemoryRateLimiter
from client import AsyncGatewayClient
from tests.fakes import FakeRepository

KEY_ID = "agent-1"
SECRET = "unit-test-secret-" + "x" * 32


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="",
        redis_url="",
        client_keys={KEY_ID: SECRET},
        allowed_networks=["127.0.0.1/32"],
        rate_limit_per_minute=1_000,
    )


@pytest.fixture
def repo() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
async def app(settings, repo):
    application = create_app(settings, repo=repo, nonces=MemoryNonceStore(), limiter=MemoryRateLimiter())
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
def transport(app) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app)  # requests arrive from 127.0.0.1


@pytest.fixture
async def client(transport):
    async with AsyncGatewayClient("http://gateway.test", KEY_ID, SECRET, transport=transport) as c:
        yield c


@pytest.fixture
async def raw(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as c:
        yield c

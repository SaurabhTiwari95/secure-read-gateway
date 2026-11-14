"""HTTP API. Run with:  uvicorn app.main:create_app --factory

Endpoints
  GET  /healthz             liveness: the process is up
  GET  /readyz              readiness: database and nonce store reachable
  GET  /v1/functions        signed: the functions this caller may use
  POST /v1/call/{function}  signed: run one allow-listed, read-only function
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import __version__, signing
from app.config import Settings
from app.db import AuditEntry, PostgresRepository, QueryTimeout, Repository
from app.registry import ParamError, Registry
from app.stores import (
    MemoryNonceStore,
    MemoryRateLimiter,
    NonceStore,
    RateLimiter,
    RedisNonceStore,
    RedisRateLimiter,
)

log = logging.getLogger("gateway")

NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
FUNCTION_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, details: Any = None, headers: dict[str, str] | None = None):
        super().__init__(code)
        self.status, self.code, self.details, self.headers = status, code, details, headers or {}


class CallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    params: dict[str, Any] = Field(default_factory=dict)
    page: int = Field(default=1, ge=1, le=10_000)
    page_size: int | None = Field(default=None, ge=1, le=1_000)


@dataclass(frozen=True)
class Caller:
    key_id: str
    ip: str
    body: bytes


@dataclass
class Gateway:
    settings: Settings
    networks: list
    repo: Repository
    nonces: NonceStore
    limiter: RateLimiter
    registry: Registry


def _json_value(value: Any) -> Any:
    """Fast JSON conversion for the few types a database row holds. Money stays exact (string)."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.date | dt.datetime | dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    return str(value)  # UUID, inet and anything else: its text form


def _client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # The right-most entry is the one our own proxy added; the rest is client-supplied.
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else ""


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise ApiError(413, "body_too_large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(413, "body_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


async def authenticate(request: Request) -> Caller:
    """IP allow-list → signature → freshness → single-use nonce → rate limit."""
    gw: Gateway = request.app.state.gateway
    s = gw.settings

    ip = _client_ip(request, s)
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        raise ApiError(403, "forbidden") from None
    if not any(address in net for net in gw.networks):
        raise ApiError(403, "forbidden")

    h = request.headers
    key_id = h.get(signing.HEADER_KEY_ID, "")
    timestamp = h.get(signing.HEADER_TIMESTAMP, "")
    nonce = h.get(signing.HEADER_NONCE, "")
    signature = h.get(signing.HEADER_SIGNATURE, "")
    secret = s.client_keys.get(key_id)
    if not (secret and timestamp.isdigit() and NONCE_PATTERN.match(nonce) and signature):
        raise ApiError(401, "unauthorized")
    if abs(time.time() - int(timestamp)) > s.max_clock_skew_seconds:
        raise ApiError(401, "stale_request")

    body = await _read_body(request, s.max_body_bytes)
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if not signing.verify(secret, signature, request.method, path, timestamp, nonce, body):
        raise ApiError(401, "unauthorized")
    # Claimed only after the signature checks out, so strangers can't burn nonces.
    if not await gw.nonces.claim(f"{key_id}:{nonce}", 2 * s.max_clock_skew_seconds):
        raise ApiError(401, "replayed_request")
    if not await gw.limiter.hit(key_id, s.rate_limit_per_minute, 60):
        raise ApiError(429, "rate_limited", headers={"Retry-After": "60"})
    return Caller(key_id=key_id, ip=ip, body=body)


def create_app(
    settings: Settings | None = None,
    repo: Repository | None = None,
    nonces: NonceStore | None = None,
    limiter: RateLimiter | None = None,
) -> FastAPI:
    settings = settings or Settings()
    if not logging.getLogger().handlers:
        logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        redis_client = None
        r = repo
        if r is None:
            if not settings.database_url:
                raise RuntimeError("DATABASE_URL is not set")
            r = await PostgresRepository.create(
                settings.database_url, settings.db_pool_min, settings.db_pool_max, settings.statement_timeout_ms
            )
        n, lim = nonces, limiter
        if n is None or lim is None:
            if settings.redis_url:
                from redis.asyncio import Redis

                redis_client = Redis.from_url(settings.redis_url)
                n, lim = n or RedisNonceStore(redis_client), lim or RedisRateLimiter(redis_client)
            else:
                log.warning("REDIS_URL not set: in-memory nonce store and rate limiter, run ONE instance only")
                n, lim = n or MemoryNonceStore(), lim or MemoryRateLimiter()
        if not settings.client_keys:
            log.warning("CLIENT_KEYS is empty: every signed request will be refused")
        registry = Registry(r, settings.registry_ttl_seconds)
        await registry.load()
        app.state.gateway = Gateway(settings, settings.networks(), r, n, lim, registry)
        try:
            yield
        finally:
            await r.close()
            if redis_client is not None:
                await redis_client.aclose()

    app = FastAPI(
        title="secure-read-gateway",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex  # never trust a client-supplied id
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        body: dict[str, Any] = {"error": exc.code, "request_id": getattr(request.state, "request_id", None)}
        if exc.status in (401, 403, 413, 429):
            log.warning("refused %s %s: %s rid=%s", request.method, request.url.path, exc.code, body["request_id"])
        if exc.details is not None:
            body["details"] = exc.details
        return JSONResponse(body, status_code=exc.status, headers=exc.headers)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):  # pragma: no cover - last resort
        rid = getattr(request.state, "request_id", None)
        log.exception("unhandled error rid=%s", rid)
        return JSONResponse({"error": "internal_error", "request_id": rid}, status_code=500)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request):
        gw: Gateway = request.app.state.gateway
        db_ok = await gw.repo.ping()
        try:
            store_ok = await gw.nonces.ping()
        except Exception:
            store_ok = False
        ready = db_ok and store_ok
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "database": db_ok, "store": store_ok},
            status_code=200 if ready else 503,
        )

    @app.get("/v1/functions")
    async def list_functions(request: Request, caller: Caller = Depends(authenticate)):
        gw: Gateway = request.app.state.gateway
        return {"functions": [f.describe() for f in await gw.registry.all()]}

    @app.post("/v1/call/{name}")
    async def call_function(name: str, request: Request, caller: Caller = Depends(authenticate)):
        gw: Gateway = request.app.state.gateway
        rid = request.state.request_id
        started = time.perf_counter()

        async def audit(status: str, rows: int | None = None, required: bool = False) -> None:
            entry = AuditEntry(
                request_id=rid,
                key_id=caller.key_id,
                function_name=name[:64],
                status=status,
                row_count=rows,
                duration_ms=int((time.perf_counter() - started) * 1000),
                client_ip=caller.ip or None,
            )
            try:
                await gw.repo.write_audit(entry)
            except Exception:
                log.exception("audit write failed rid=%s", rid)
                if required:  # fail closed: no audit record, no data
                    raise ApiError(500, "internal_error") from None
            log.info(
                "call fn=%s key=%s status=%s rows=%s ms=%s rid=%s",
                entry.function_name, entry.key_id, status, rows, entry.duration_ms, rid,
            )  # fmt: skip

        try:
            req = CallRequest.model_validate_json(caller.body or b"{}")
        except ValidationError as exc:
            await audit("invalid_request")
            details = [{"field": ".".join(map(str, e["loc"])) or "body", "error": e["msg"]} for e in exc.errors()]
            raise ApiError(422, "invalid_request", details) from None

        fn = await gw.registry.get(name) if FUNCTION_NAME_PATTERN.match(name) else None
        if fn is None:
            await audit("unknown_function")
            raise ApiError(404, "unknown_function")

        try:
            args = fn.bind(req.params)
        except ParamError as exc:
            await audit("invalid_params")
            raise ApiError(422, "invalid_params", exc.errors) from None

        size = min(req.page_size or fn.spec.max_rows, fn.spec.max_rows, gw.settings.max_page_size)
        offset = (req.page - 1) * size
        try:
            rows = await gw.repo.run_read(fn.paged_sql(), [*args, size + 1, offset])
        except QueryTimeout:
            await audit("timeout")
            raise ApiError(504, "query_timeout") from None
        except Exception:
            log.exception("query failed fn=%s rid=%s", name, rid)
            await audit("error")
            raise ApiError(500, "internal_error") from None

        has_more = len(rows) > size
        rows = rows[:size]
        await audit("ok", len(rows), required=True)
        # A JSONResponse directly: skips FastAPI's generic encoder, which costs more than the call itself.
        return JSONResponse(
            {
                "function": name,
                "page": req.page,
                "page_size": size,
                "has_more": has_more,
                "rows": [{k: _json_value(v) for k, v in row.items()} for row in rows],
                "request_id": rid,
            }
        )

    return app

"""Client an AI agent uses to call the gateway. Signs every request.

from client import GatewayClient
gw = GatewayClient("http://127.0.0.1:8000", key_id="demo-agent", secret=os.environ["AGENT_SECRET"])
gw.call("list_overdue_invoices", {"as_of": "2026-11-01"})
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.signing import signed_headers


class GatewayError(Exception):
    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(f"{status} {body.get('error', 'error')}")
        self.status, self.body = status, body


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), default=str).encode()


def _payload(params: dict[str, Any] | None, page: int, page_size: int | None) -> dict[str, Any]:
    body: dict[str, Any] = {"params": params or {}, "page": page}
    if page_size is not None:
        body["page_size"] = page_size
    return body


def _result(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = {"error": "non_json_response"}
    if response.status_code >= 400:
        raise GatewayError(response.status_code, body)
    return body


class GatewayClient:
    def __init__(self, base_url: str, key_id: str, secret: str, timeout: float = 10.0, **client_kwargs):
        self._key_id, self._secret = key_id, secret
        self._http = httpx.Client(base_url=base_url, timeout=timeout, **client_kwargs)

    def _send(self, method: str, path: str, body: bytes = b"") -> dict[str, Any]:
        headers = signed_headers(self._key_id, self._secret, method, path, body)
        headers["Content-Type"] = "application/json"
        return _result(self._http.request(method, path, content=body, headers=headers))

    def functions(self) -> list[dict[str, Any]]:
        return self._send("GET", "/v1/functions")["functions"]

    def call(
        self, function: str, params: dict[str, Any] | None = None, page: int = 1, page_size: int | None = None
    ) -> dict[str, Any]:
        return self._send("POST", f"/v1/call/{function}", _encode(_payload(params, page, page_size)))

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class AsyncGatewayClient:
    def __init__(self, base_url: str, key_id: str, secret: str, timeout: float = 10.0, **client_kwargs):
        self._key_id, self._secret = key_id, secret
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, **client_kwargs)

    async def _send(self, method: str, path: str, body: bytes = b"") -> dict[str, Any]:
        headers = signed_headers(self._key_id, self._secret, method, path, body)
        headers["Content-Type"] = "application/json"
        return _result(await self._http.request(method, path, content=body, headers=headers))

    async def functions(self) -> list[dict[str, Any]]:
        return (await self._send("GET", "/v1/functions"))["functions"]

    async def call(
        self, function: str, params: dict[str, Any] | None = None, page: int = 1, page_size: int | None = None
    ) -> dict[str, Any]:
        return await self._send("POST", f"/v1/call/{function}", _encode(_payload(params, page, page_size)))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

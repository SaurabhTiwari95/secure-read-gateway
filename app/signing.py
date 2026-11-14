"""HMAC-SHA256 request signing, shared by the server and the client.

The signature covers:  METHOD \n PATH(+query) \n TIMESTAMP \n NONCE \n sha256(body)
so changing any of them, or replaying the request, breaks it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

HEADER_KEY_ID = "X-Key-Id"
HEADER_TIMESTAMP = "X-Timestamp"
HEADER_NONCE = "X-Nonce"
HEADER_SIGNATURE = "X-Signature"


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical_string(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    return "\n".join([method.upper(), path, timestamp, nonce, body_hash(body)]).encode()


def sign(secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = canonical_string(method, path, timestamp, nonce, body)
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify(secret: str, signature: str, method: str, path: str, timestamp: str, nonce: str, body: bytes) -> bool:
    expected = sign(secret, method, path, timestamp, nonce, body)
    return hmac.compare_digest(expected, signature)  # constant time


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def signed_headers(
    key_id: str, secret: str, method: str, path: str, body: bytes, now: float | None = None
) -> dict[str, str]:
    """Headers a client sends with one request."""
    timestamp = str(int(time.time() if now is None else now))
    nonce = new_nonce()
    return {
        HEADER_KEY_ID: key_id,
        HEADER_TIMESTAMP: timestamp,
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: sign(secret, method, path, timestamp, nonce, body),
    }

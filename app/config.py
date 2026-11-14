"""Settings, read from environment variables (12-factor). See README, "Configuration"."""

from __future__ import annotations

import ipaddress

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Connections
    database_url: str = Field(default="", description="DSN for the least-privilege app role")
    redis_url: str = Field(default="", description="Empty = in-memory stores (single instance only)")
    db_pool_min: int = Field(default=1, ge=1, le=50)
    db_pool_max: int = Field(default=10, ge=1, le=200)
    statement_timeout_ms: int = Field(default=5_000, ge=100, le=60_000)

    # Who may call
    client_keys: dict[str, str] = Field(default_factory=dict, description='JSON: {"key-id": "secret"}')
    allowed_networks: list[str] = Field(default_factory=lambda: ["127.0.0.1/32"])
    trust_proxy_headers: bool = False

    # Limits
    max_clock_skew_seconds: int = Field(default=300, ge=5, le=900)
    rate_limit_per_minute: int = Field(default=120, ge=1, le=100_000)
    max_body_bytes: int = Field(default=16_384, ge=256, le=1_048_576)
    max_page_size: int = Field(default=200, ge=1, le=1_000)
    registry_ttl_seconds: int = Field(default=60, ge=1, le=3_600)

    enable_docs: bool = False
    log_level: str = "INFO"

    @field_validator("client_keys")
    @classmethod
    def _strong_secrets(cls, keys: dict[str, str]) -> dict[str, str]:
        for key_id, secret in keys.items():
            if not key_id or len(key_id) > 64:
                raise ValueError("key ids must be 1-64 characters")
            if len(secret) < MIN_SECRET_LENGTH:
                raise ValueError(f"secret for '{key_id}' is shorter than {MIN_SECRET_LENGTH} characters")
            if "replace-with" in secret:
                raise ValueError(f"secret for '{key_id}' is still the placeholder from .env.example")
        return keys

    @field_validator("allowed_networks")
    @classmethod
    def _valid_networks(cls, networks: list[str]) -> list[str]:
        for net in networks:
            ipaddress.ip_network(net, strict=False)  # raises ValueError if invalid
        return networks

    def networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(n, strict=False) for n in self.allowed_networks]

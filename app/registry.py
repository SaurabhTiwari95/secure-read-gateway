"""The function allow-list: what agents may call, with typed, validated parameters.

Functions are stored as data (table gateway.functions), so adding one needs a reviewed insert,
not a deploy. Each is checked by the SQL guard when loaded; anything that fails is refused and
reported, never run. The list is cached in memory and reloaded after a TTL.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from app import sqlguard

log = logging.getLogger("gateway.registry")

ParamType = Literal["int", "str", "date", "bool", "decimal"]
_PAGING = re.compile(r"\b(?:limit|offset|fetch)\b")
_PY_TYPES: dict[str, type] = {"int": int, "str": str, "date": dt.date, "bool": bool, "decimal": Decimal}


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z_][a-z0-9_]{0,40}$")
    type: ParamType
    required: bool = True
    default: Any = None
    min: float | None = None
    max: float | None = None
    max_length: int = Field(default=100, ge=1, le=1_000)
    description: str = ""


class FunctionSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,62}$")
    description: str
    sql: str
    params: list[ParamSpec] = Field(default_factory=list)
    max_rows: int = Field(default=100, ge=1, le=1_000)


class ParamError(ValueError):
    """Invalid call parameters. `errors` is safe to return: it never echoes the input values."""

    def __init__(self, errors: list[dict[str, str]]):
        super().__init__("invalid parameters")
        self.errors = errors


def _field_for(p: ParamSpec) -> tuple[Any, Any]:
    py_type = _PY_TYPES[p.type]
    kwargs: dict[str, Any] = {"description": p.description}
    if p.type in ("int", "decimal"):
        if p.min is not None:
            kwargs["ge"] = p.min
        if p.max is not None:
            kwargs["le"] = p.max
    if p.type == "str":
        kwargs["max_length"] = p.max_length
        kwargs["min_length"] = 1
    if p.required:
        return (py_type, Field(..., **kwargs))
    return (py_type | None, Field(p.default, validate_default=True, **kwargs))


class LoadedFunction:
    def __init__(self, spec: FunctionSpec):
        self.spec = spec
        fields = {p.name: _field_for(p) for p in spec.params}
        self._model = create_model(  # type: ignore[call-overload]
            f"Params_{spec.name}", __config__=ConfigDict(extra="forbid"), **fields
        )

    def bind(self, params: dict[str, Any]) -> list[Any]:
        """Validate params and return them in $1..$n order."""
        try:
            validated = self._model.model_validate(params)
        except ValidationError as exc:
            raise ParamError(
                [{"param": ".".join(map(str, e["loc"])) or "params", "error": e["msg"]} for e in exc.errors()]
            ) from None
        return [getattr(validated, p.name) for p in self.spec.params]

    def paged_sql(self) -> str:
        # Appended, not wrapped, so the function's own ORDER BY is guaranteed to hold.
        n = len(self.spec.params)
        return f"{self.spec.sql.strip().rstrip(';')}\nLIMIT ${n + 1} OFFSET ${n + 2}"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.spec.name,
            "description": self.spec.description,
            "max_rows": self.spec.max_rows,
            "params": [p.model_dump(exclude={"max_length"} if p.type != "str" else set()) for p in self.spec.params],
        }


class SpecSource(Protocol):
    async def fetch_function_specs(self) -> list[dict[str, Any]]: ...


class Registry:
    def __init__(self, source: SpecSource, ttl_seconds: int = 60, clock=time.monotonic):
        self._source = source
        self._ttl = ttl_seconds
        self._clock = clock
        self._functions: dict[str, LoadedFunction] = {}
        self.rejected: dict[str, str] = {}
        self._loaded_at: float | None = None
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        rows = await self._source.fetch_function_specs()
        functions: dict[str, LoadedFunction] = {}
        rejected: dict[str, str] = {}
        for row in rows:
            name = str(row.get("name", "?"))
            try:
                spec = FunctionSpec.model_validate(row)
            except ValidationError:
                rejected[name] = "invalid definition"
                continue
            verdict = sqlguard.check(spec.sql)
            if not verdict.ok:
                rejected[name] = verdict.reason
                continue
            if _PAGING.search(sqlguard.normalise(spec.sql)):
                rejected[name] = "remove LIMIT/OFFSET: the gateway adds paging"
                continue
            functions[spec.name] = LoadedFunction(spec)
        for name, reason in rejected.items():
            log.warning("refused function %s: %s", name, reason)
        self._functions, self.rejected = functions, rejected
        self._loaded_at = self._clock()
        log.info("registry loaded: %d allowed, %d refused", len(functions), len(rejected))

    async def _ensure_fresh(self) -> None:
        if self._loaded_at is not None and self._clock() - self._loaded_at < self._ttl:
            return
        async with self._lock:  # one reload at a time, others wait and reuse it
            if self._loaded_at is None or self._clock() - self._loaded_at >= self._ttl:
                await self.load()

    async def get(self, name: str) -> LoadedFunction | None:
        await self._ensure_fresh()
        return self._functions.get(name)

    async def all(self) -> list[LoadedFunction]:
        await self._ensure_fresh()
        return [self._functions[k] for k in sorted(self._functions)]

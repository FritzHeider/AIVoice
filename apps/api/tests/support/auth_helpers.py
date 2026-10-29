"""API key auth test doubles: an in-memory key store, a fake-Redis authenticator, and auth test routers."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Annotated, Any

import fakeredis
from api_helpers import FakeChecks, make_settings
from fastapi import APIRouter, Depends, FastAPI, Request
from redis.asyncio import Redis

from va_api.auth import ApiKeyAuthenticator, RequestContext, rate_limit, require_scope
from va_api.envelopes import envelope_router, legacy_router
from va_api.rate_limit import RateLimiter
from va_common.auth import ApiKeyHasher, KeyEnvironment, Pepper, api_key_prefix, generate_api_key
from va_common.ids import new_id
from va_common.time import FrozenClock
from va_db.repositories import ApiKeyRecord

__all__ = [
    "AUTH_ENVELOPE_PREFIX",
    "AUTH_LEGACY_PREFIX",
    "AuthContainer",
    "KeyStore",
    "auth_app",
    "make_hasher",
]

AUTH_LEGACY_PREFIX = "/v1/calls/_auth"
AUTH_ENVELOPE_PREFIX = "/v1/knowledge/_auth"


def make_hasher(version: int = 1) -> ApiKeyHasher:
    return ApiKeyHasher(Pepper(secrets.token_bytes(32), version))


@dataclass
class KeyStore:
    """Keys by prefix standing in for ``ApiKeyLookupRepo``; records every lookup and touch."""

    hasher: ApiKeyHasher = field(default_factory=make_hasher)
    records: dict[str, ApiKeyRecord] = field(default_factory=dict)
    lookups: list[str] = field(default_factory=list)
    touches: list[tuple[uuid.UUID, datetime]] = field(default_factory=list)

    def issue(
        self, *, environment: KeyEnvironment = "live", scopes: tuple[str, ...] = ("*",), **overrides: Any
    ) -> tuple[str, ApiKeyRecord]:
        """A new plaintext key and its stored record (``overrides`` replace record fields)."""
        plaintext = generate_api_key(environment)
        record = ApiKeyRecord(
            id=new_id(),
            org_id=new_id(),
            secret_hash=self.hasher.hash(plaintext),
            scopes=scopes,
            environment=environment,
            pepper_version=self.hasher.current_version,
            revoked=False,
            expires_at=None,
            org_status="active",
            plan_id="build",
        )
        record = replace(record, **overrides)
        self.records[api_key_prefix(plaintext)] = record
        return plaintext, record

    async def lookup(self, prefix: str) -> ApiKeyRecord | None:
        self.lookups.append(prefix)
        return self.records.get(prefix)

    async def touch(self, api_key_id: uuid.UUID, now: datetime) -> None:
        self.touches.append((api_key_id, now))

    def authenticator(self, redis: Redis, clock: FrozenClock) -> ApiKeyAuthenticator:
        return ApiKeyAuthenticator(
            redis=redis, hasher=self.hasher, lookup=self.lookup, toucher=self.touch, clock=clock
        )


@dataclass
class AuthContainer(FakeChecks):
    """The fake api container plus an authenticator over a :class:`KeyStore` and fake Redis."""

    store: KeyStore = field(default_factory=KeyStore)
    redis: Redis = field(default_factory=fakeredis.FakeAsyncRedis)
    clock: FrozenClock = field(default_factory=FrozenClock)

    @property
    def authenticator(self) -> ApiKeyAuthenticator:
        return self.store.authenticator(self.redis, self.clock)

    @property
    def rate_limiter(self) -> RateLimiter:
        return RateLimiter(self.redis, self.clock)


def _auth_routes(router: APIRouter, family: str) -> APIRouter:
    @router.get(
        "/read", operation_id=f"auth_{family}_read", dependencies=[Depends(require_scope("calls:read"))]
    )
    async def read(request: Request) -> dict[str, str]:
        ctx: RequestContext = request.state.ctx
        return {"org_id": str(ctx.org_id), "environment": ctx.environment}

    @router.post("/write", operation_id=f"auth_{family}_write")
    async def write(ctx: Annotated[RequestContext, Depends(require_scope("calls:write"))]) -> dict[str, str]:
        return {"org_id": str(ctx.org_id), "principal_id": str(ctx.principal_id)}

    @router.post(
        "/send",
        operation_id=f"auth_{family}_send",
        dependencies=[Depends(require_scope("calls:write")), Depends(rate_limit("calls_send"))],
    )
    async def send(request: Request) -> dict[str, str]:
        ctx: RequestContext = request.state.ctx
        return {"org_id": str(ctx.org_id)}

    return router


auth_legacy = _auth_routes(legacy_router(prefix=AUTH_LEGACY_PREFIX, tags=["Calls"]), "legacy")
auth_envelope = _auth_routes(envelope_router(prefix=AUTH_ENVELOPE_PREFIX, tags=["Knowledge"]), "envelope")


def auth_app(container: AuthContainer) -> FastAPI:
    """The production app plus the auth test routers, served by ``container``."""
    from va_api.main import ROUTERS, create_app  # noqa: PLC0415 - mirrors api_helpers.sample_app

    return create_app(
        make_settings(),
        container_factory=container,
        init_observability=False,
        routers=(*ROUTERS, auth_legacy, auth_envelope),
    )

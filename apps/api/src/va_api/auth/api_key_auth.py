"""API key authentication (specs/04-auth-tenancy-rbac.md §2.2).

``Authorization: Bearer <key>`` (scheme case-insensitive) and the bare key are accepted; ``x-api-key`` is
never read (specs/02-repo-layout-and-conventions.md §10). The key is found by prefix through the
``apikey:{prefix}`` Redis cache (60 s) or ``ApiKeyLookupRepo`` on the system session, then verified against
its peppered hash. Every failure is ``AUTH_FAILURE`` "Invalid API key." and costs exactly one hash, whether
the value was malformed, the prefix unknown, or the secret wrong. ``last_used_at`` is written at most once per
60 s per key, gated by ``SET apikey:{id}:touched NX EX 60``. Redis being unavailable never fails
authentication: the cache is skipped and the database answers.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from va_api.auth.context import RequestContext
from va_common import json
from va_common.auth import ApiKeyHasher, KeyEnvironment, api_key_prefix, looks_like_api_key
from va_common.errors import AppError
from va_common.logging import get_logger
from va_common.time import Clock, SystemClock
from va_db.repositories import ApiKeyLookupRepo, ApiKeyRecord
from va_db.session import Database

__all__ = [
    "CACHE_TTL_S",
    "INVALID_API_KEY",
    "ORG_SUSPENDED_MESSAGE",
    "READ_METHODS",
    "TOUCH_INTERVAL_S",
    "ApiKeyAuthenticator",
    "KeyLookup",
    "KeyToucher",
    "cache_key",
    "db_lookup",
    "db_touch",
    "extract_api_key",
    "invalidate_api_key_cache",
    "touched_key",
]

INVALID_API_KEY: Final = "Invalid API key."
ORG_SUSPENDED_MESSAGE: Final = "Organization is suspended; only read requests are allowed."
CACHE_TTL_S: Final = 60
TOUCH_INTERVAL_S: Final = 60
READ_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_BEARER: Final = "bearer"

type KeyLookup = Callable[[str], Awaitable[ApiKeyRecord | None]]
type KeyToucher = Callable[[uuid.UUID, datetime], Awaitable[None]]

_log = get_logger(__name__)


def cache_key(prefix: str) -> str:
    """``apikey:{prefix}`` with the prefix as the Redis Cluster hash tag (specs/03-data-model.md §11)."""
    return f"apikey:{{{prefix}}}"


def touched_key(api_key_id: uuid.UUID) -> str:
    """``apikey:{id}:touched``, the ``last_used_at`` throttle gate (specs/03-data-model.md §11)."""
    return f"apikey:{{{api_key_id}}}:touched"


def extract_api_key(authorization: str | None) -> str | None:
    """The key from ``Bearer <key>`` or a bare value; ``None`` when the header is absent or blank."""
    if authorization is None:
        return None
    value = authorization.strip()
    scheme, _, rest = value.partition(" ")
    if scheme.lower() == _BEARER and rest.strip():
        return rest.strip()
    return value or None


async def invalidate_api_key_cache(redis: Redis, prefix: str) -> None:
    """Drop the cached lookup of ``prefix`` (``DEL apikey:{prefix}``) after a revoke or change."""
    await redis.delete(cache_key(prefix))


def db_lookup(database: Database) -> KeyLookup:
    """Lookup through ``ApiKeyLookupRepo`` on the system session (specs/03-data-model.md §2)."""

    async def lookup(prefix: str) -> ApiKeyRecord | None:
        async with database.system_session() as session:
            return await ApiKeyLookupRepo(session).get_by_prefix(prefix)

    return lookup


def db_touch(database: Database) -> KeyToucher:
    """``last_used_at`` write through ``ApiKeyLookupRepo`` on the system session (specs/03 §2)."""

    async def touch(api_key_id: uuid.UUID, now: datetime) -> None:
        async with database.system_session() as session:
            await ApiKeyLookupRepo(session).touch(api_key_id, now)

    return touch


def _auth_failure() -> AppError:
    return AppError("AUTH_FAILURE", INVALID_API_KEY)


def _environment(record: ApiKeyRecord) -> KeyEnvironment:
    return "live" if record.environment == "live" else "test"


class ApiKeyAuthenticator:
    """Resolves an ``Authorization`` header to a :class:`RequestContext` for the key's org."""

    def __init__(
        self,
        *,
        redis: Redis,
        hasher: ApiKeyHasher,
        lookup: KeyLookup,
        toucher: KeyToucher,
        clock: Clock | None = None,
    ) -> None:
        self._redis = redis
        self._hasher = hasher
        self._lookup = lookup
        self._toucher = toucher
        self._clock: Clock = clock if clock is not None else SystemClock()

    async def authenticate(
        self,
        authorization: str | None,
        *,
        method: str,
        request_id: str,
        ip: str | None,
        user_agent: str | None,
    ) -> tuple[RequestContext, ApiKeyRecord]:
        """The principal of a valid key; ``AUTH_FAILURE`` or (writes on a suspended org) ``ORG_SUSPENDED``."""
        plaintext = extract_api_key(authorization) or ""
        if not looks_like_api_key(plaintext):
            self._hasher.dummy_verify(plaintext)
            raise _auth_failure()
        record = await self._find(api_key_prefix(plaintext))
        if record is None:
            self._hasher.dummy_verify(plaintext)
            raise _auth_failure()
        if not self._hasher.verify(plaintext, record.secret_hash, record.pepper_version):
            raise _auth_failure()
        self._check_usable(record, method)
        context = RequestContext(
            request_id=request_id,
            org_id=record.org_id,
            principal_type="api_key",
            principal_id=record.id,
            role=None,
            scopes=frozenset(record.scopes),
            environment=_environment(record),
            plan_id=record.plan_id,
            ip=ip,
            user_agent=user_agent,
            acting_parent_org_id=None,
        )
        return context, record

    def _check_usable(self, record: ApiKeyRecord, method: str) -> None:
        expired = record.expires_at is not None and record.expires_at <= self._clock.now()
        if record.revoked or expired or record.org_status == "closed":
            raise _auth_failure()
        if record.org_status == "suspended" and method.upper() not in READ_METHODS:
            raise AppError("ORG_SUSPENDED", ORG_SUSPENDED_MESSAGE)

    async def _find(self, prefix: str) -> ApiKeyRecord | None:
        cached = await self._cached(prefix)
        if cached is not None:
            return cached
        record = await self._lookup(prefix)
        if record is not None:
            await self._store(prefix, record)
        return record

    async def _cached(self, prefix: str) -> ApiKeyRecord | None:
        try:
            raw = await self._redis.get(cache_key(prefix))
        except (RedisError, OSError) as exc:
            _log.warning("api_key_cache_unavailable", key_prefix=prefix, error=type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            return ApiKeyRecord.from_json(json.loads(raw))
        except (KeyError, TypeError, ValueError):
            _log.warning("api_key_cache_invalid", key_prefix=prefix)
            return None

    async def _store(self, prefix: str, record: ApiKeyRecord) -> None:
        try:
            await self._redis.set(cache_key(prefix), json.dumps(record.to_json()), ex=CACHE_TTL_S)
        except (RedisError, OSError) as exc:
            _log.warning("api_key_cache_unavailable", key_prefix=prefix, error=type(exc).__name__)

    async def touch_if_due(self, api_key_id: uuid.UUID) -> None:
        """Write ``last_used_at`` unless written in the last 60 s; failures are logged, never raised."""
        try:
            if await self._redis.set(touched_key(api_key_id), "1", nx=True, ex=TOUCH_INTERVAL_S):
                await self._toucher(api_key_id, self._clock.now())
        except (RedisError, SQLAlchemyError, OSError) as exc:
            _log.warning("api_key_touch_failed", api_key_id=str(api_key_id), error=type(exc).__name__)

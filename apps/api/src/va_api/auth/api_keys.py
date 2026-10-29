"""Minting API keys, naming who created them, and the key-management rules (specs/04 §2.1, §8.2, §9).

A minted key exists in plaintext only in :class:`MintedKey` until it is returned to the caller once; the
database stores its prefix and peppered hash. Sub-account creation and the ``/v1/api_keys`` endpoints both
mint here. The §9 rules: a caller grants only scopes it holds (owner and admin sessions hold every scope for
this purpose, ``*`` and ``mcp`` included), a ``test`` principal manages only ``test`` keys, and a developer
manages only the keys it created. :func:`invalidate_cached_keys` drops ``apikey:{prefix}`` lookups after a key
changes, so a revoked or expired key stops authenticating at once.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from fastapi import Request
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from va_api.auth.api_key_auth import invalidate_api_key_cache
from va_api.auth.context import RequestContext
from va_api.auth.scopes import ALL_SCOPES, SCOPES
from va_api.auth.session_auth import redis_of
from va_common.auth import ApiKeyHasher, KeyEnvironment, api_key_prefix, generate_api_key
from va_common.errors import AppError
from va_common.logging import get_logger
from va_db.models import ApiKey
from va_db.repositories import ApiKeyRepo
from va_db.session import use_org

__all__ = [
    "EXPIRES_AT_PAST_MESSAGE",
    "KEY_MANAGERS",
    "NOT_OWN_KEY_MESSAGE",
    "TEST_KEYS_ONLY_MESSAGE",
    "USER_PRINCIPALS",
    "MintedKey",
    "check_environment",
    "check_expires_at",
    "check_grantable",
    "check_manages",
    "creating_user_id",
    "drop_cached_keys",
    "grantable_scopes",
    "invalidate_cached_keys",
    "mint_api_key",
    "normalize_scopes",
    "ungrantable_message",
    "unknown_scope_message",
]

USER_PRINCIPALS: Final = frozenset({"user", "platform_admin"})
# Roles that manage every key of the org and may grant every scope (§3.2 "API keys" rows, §3.1 full control).
KEY_MANAGERS: Final = frozenset({"owner", "admin"})
TEST_KEYS_ONLY_MESSAGE: Final = "Test keys can only manage test keys."
NOT_OWN_KEY_MESSAGE: Final = "Developers can only manage API keys they created."
EXPIRES_AT_PAST_MESSAGE: Final = "expires_at must be in the future."

_log = get_logger(__name__)


def unknown_scope_message(scope: str) -> str:
    return f"Unknown scope '{scope}'."


def ungrantable_message(scopes: Iterable[str]) -> str:
    return f"Cannot grant scopes you do not hold: {', '.join(sorted(scopes))}."


@dataclass(frozen=True, slots=True)
class MintedKey:
    """A fresh key: the plaintext (never in ``repr``), its prefix, its hash and the pepper version used."""

    plaintext: str = field(repr=False)
    prefix: str
    secret_hash: str
    pepper_version: int


def mint_api_key(hasher: ApiKeyHasher, environment: KeyEnvironment) -> MintedKey:
    """A new ``va_<environment>_…`` key hashed with the current pepper."""
    plaintext = generate_api_key(environment)
    return MintedKey(
        plaintext=plaintext,
        prefix=api_key_prefix(plaintext),
        secret_hash=hasher.hash(plaintext),
        pepper_version=hasher.current_version,
    )


async def creating_user_id(ctx: RequestContext, session: AsyncSession) -> uuid.UUID | None:
    """The session's user, or the user who created the calling API key (``None`` when it has none).

    ``session`` is a ``tenant_session(ctx.org_id)``; the key row is read in the org owning the key, which is
    the parent while acting on a sub-account (specs/04 §8.3, RLS switched for the lookup only).
    """
    if ctx.principal_type in USER_PRINCIPALS:
        return ctx.principal_id
    if ctx.principal_type == "api_key":
        async with use_org(session, ctx.principal_org_id):
            key = await ApiKeyRepo(session, ctx.principal_org_id).get(ctx.principal_id)
        return key.created_by if key is not None else None
    return None


def grantable_scopes(ctx: RequestContext) -> frozenset[str]:
    """The scopes ``ctx`` may put on a key it creates or rolls (specs/04 §9 rule, §3.2 ``W(own)``).

    An API key grants its own scopes (``*`` meaning every scope); an owner or admin session every scope; any
    other user its role's scopes; a widget nothing.
    """
    if ctx.principal_type == "api_key":
        return SCOPES if ALL_SCOPES in ctx.scopes else ctx.scopes
    if ctx.principal_type in USER_PRINCIPALS:
        return SCOPES if ctx.role in KEY_MANAGERS else ctx.scopes
    return frozenset()


def normalize_scopes(scopes: Iterable[str]) -> list[str]:
    """``scopes`` without duplicates, sorted; ``["*"]`` when it holds ``*``; unknown → ``INVALID_INPUT``."""
    unique = set(scopes)
    for scope in sorted(unique):
        if scope not in SCOPES:
            raise AppError("INVALID_INPUT", unknown_scope_message(scope), field="scopes")
    return [ALL_SCOPES] if ALL_SCOPES in unique else sorted(unique)


def check_grantable(ctx: RequestContext, scopes: Iterable[str]) -> None:
    """``FORBIDDEN`` naming every scope of ``scopes`` that ``ctx`` cannot grant."""
    missing = set(scopes) - grantable_scopes(ctx)
    if missing:
        raise AppError("FORBIDDEN", ungrantable_message(missing))


def check_environment(ctx: RequestContext, environment: KeyEnvironment) -> None:
    """A ``test`` principal may create or roll only ``test`` keys (no test → live escalation)."""
    if ctx.environment == "test" and environment != "test":
        raise AppError("FORBIDDEN", TEST_KEYS_ONLY_MESSAGE)


def check_expires_at(expires_at: datetime | None, now: datetime) -> None:
    """A requested expiry must lie strictly after ``now`` (``INVALID_INPUT`` on ``expires_at``)."""
    if expires_at is not None and expires_at <= now:
        raise AppError("INVALID_INPUT", EXPIRES_AT_PAST_MESSAGE, field="expires_at")


def check_manages(ctx: RequestContext, key: ApiKey) -> None:
    """Users outside :data:`KEY_MANAGERS` (developers) manage only keys they created; API keys manage all."""
    if (
        ctx.principal_type in USER_PRINCIPALS
        and ctx.role not in KEY_MANAGERS
        and key.created_by != ctx.principal_id
    ):
        raise AppError("FORBIDDEN", NOT_OWN_KEY_MESSAGE)


async def drop_cached_keys(redis: Redis, prefixes: Iterable[str]) -> list[str]:
    """Drop the cached lookups of ``prefixes``; the prefixes Redis failed on (logged, never raised)."""
    failed: list[str] = []
    for prefix in prefixes:
        try:
            await invalidate_api_key_cache(redis, prefix)
        except (RedisError, OSError) as exc:
            _log.warning("api_key_cache_unavailable", key_prefix=prefix, error=type(exc).__name__)
            failed.append(prefix)
    return failed


async def invalidate_cached_keys(request: Request, prefixes: Iterable[str]) -> None:
    """Drop the cached lookups of ``prefixes``; a Redis failure is logged, never raised."""
    await drop_cached_keys(redis_of(request), prefixes)

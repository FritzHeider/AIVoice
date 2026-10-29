"""The single-use OIDC login state in Redis (specs/04-auth-tenancy-rbac.md §5; specs/03 §11).

``sso:state:{state}`` holds what the callback needs to finish the flow it started: the connection and org, the
``nonce`` and PKCE ``code_verifier``, and the provider's issuer spelling, token endpoint, and JWKS URI. It
lives 10 minutes and is read with ``GETDEL``, so a state completes at most one login. Redis failures raise
:class:`SsoStateUnavailableError`; the login then answers ``provider_unavailable``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Final, Self

from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common import json

__all__ = ["SSO_STATE_TTL_S", "SsoState", "SsoStateStore", "SsoStateUnavailableError", "sso_state_key"]

SSO_STATE_TTL_S: Final = 600


def sso_state_key(state: str) -> str:
    """``sso:state:{state}`` (specs/03-data-model.md §11)."""
    return f"sso:state:{state}"


@dataclass(frozen=True, slots=True)
class SsoState:
    """One started login; ``nonce`` and ``code_verifier`` never appear in ``repr``."""

    connection_id: uuid.UUID
    org_id: uuid.UUID
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)
    issuer: str
    token_endpoint: str
    jwks_uri: str

    def to_json(self) -> str:
        return json.dumps_str(
            {
                "connection_id": str(self.connection_id),
                "org_id": str(self.org_id),
                "nonce": self.nonce,
                "code_verifier": self.code_verifier,
                "issuer": self.issuer,
                "token_endpoint": self.token_endpoint,
                "jwks_uri": self.jwks_uri,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> Self:
        """Inverse of :meth:`to_json`; ``ValueError``/``KeyError``/``TypeError`` on a malformed value."""
        data = json.loads(raw)
        if not isinstance(data, dict):
            msg = "SSO state is not a JSON object"
            raise TypeError(msg)
        strings = {
            name: data[name] for name in ("nonce", "code_verifier", "issuer", "token_endpoint", "jwks_uri")
        }
        if not all(isinstance(value, str) for value in strings.values()):
            msg = "SSO state fields must be strings"
            raise TypeError(msg)
        return cls(
            connection_id=uuid.UUID(data["connection_id"]),
            org_id=uuid.UUID(data["org_id"]),
            **strings,
        )


class SsoStateUnavailableError(Exception):
    """Redis could not store or return a login state."""


class SsoStateStore:
    """Stores login states for 10 minutes and hands each out once."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def put(self, state: str, value: SsoState) -> None:
        """Store ``value`` under ``state`` (``SET NX EX 600``); an existing key is a failure too."""
        try:
            stored = await self._redis.set(sso_state_key(state), value.to_json(), ex=SSO_STATE_TTL_S, nx=True)
        except (RedisError, OSError) as exc:
            raise SsoStateUnavailableError from exc
        if not stored:
            raise SsoStateUnavailableError

    async def take(self, state: str) -> SsoState | None:
        """The state's value, deleting it (``GETDEL``); ``None`` when unknown, expired, used, or malformed."""
        try:
            raw = await self._redis.getdel(sso_state_key(state))
        except (RedisError, OSError) as exc:
            raise SsoStateUnavailableError from exc
        if raw is None:
            return None
        try:
            return SsoState.from_json(raw)
        except (KeyError, TypeError, ValueError):
            return None

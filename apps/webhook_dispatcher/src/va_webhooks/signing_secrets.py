"""Org webhook signing secrets for the dispatcher (specs/28-security.md §3; specs/19 §8).

The secret is ``orgs.webhook_signing_secret_enc`` decrypted with the org's data key and cached in process for
``ttl_s`` seconds of the injected clock. :meth:`SigningSecrets.get` returns the active secrets, current first.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy.exc import SQLAlchemyError

from va_common.crypto import CryptoError, Envelope
from va_common.time import Clock
from va_db.provisioning import webhook_secret_aad
from va_db.repositories import OrgRepo
from va_db.session import Database

__all__ = ["SECRET_TTL_S", "SecretUnavailableError", "SigningSecrets"]

SECRET_TTL_S: Final = 300.0


class SecretUnavailableError(Exception):
    """The org's signing secret could not be read (org gone, database or decryption failure)."""


class SigningSecrets:
    """Decrypts and caches each org's webhook signing secrets."""

    def __init__(
        self, database: Database, envelope: Envelope, *, clock: Clock, ttl_s: float = SECRET_TTL_S
    ) -> None:
        self._database = database
        self._envelope = envelope
        self._clock = clock
        self._ttl_s = ttl_s
        self._cache: dict[uuid.UUID, tuple[float, tuple[str, ...]]] = {}

    async def get(self, org_id: uuid.UUID) -> tuple[str, ...]:
        """The org's active secrets, current first; :class:`SecretUnavailableError` when unreadable."""
        cached = self._cache.get(org_id)
        if cached is not None and cached[0] > self._clock.monotonic():
            return cached[1]
        try:
            async with self._database.global_session() as session:
                org = await OrgRepo(session).get(org_id)
                encrypted = None if org is None else org.webhook_signing_secret_enc
        except (SQLAlchemyError, OSError) as exc:
            raise SecretUnavailableError(type(exc).__name__) from exc
        if encrypted is None:
            msg = "org not found"
            raise SecretUnavailableError(msg)
        try:
            secret = await self._envelope.decrypt_str(org_id, encrypted, webhook_secret_aad(org_id))
        except (CryptoError, SQLAlchemyError, OSError) as exc:
            raise SecretUnavailableError(type(exc).__name__) from exc
        secrets = (secret,)
        self._cache[org_id] = (self._clock.monotonic() + self._ttl_s, secrets)
        return secrets

"""Org webhook signing secrets for the dispatcher (specs/28-security.md §3; specs/19 §8).

The secrets are ``orgs.webhook_signing_secret_enc`` plus, during a rotation window, the previous
secret (:func:`va_db.webhook_secrets.active_signing_secrets`), decrypted with the org's data key and cached
in process for ``ttl_s`` seconds of the injected clock, never past the previous secret's expiry, so an
expired previous secret stops signing promptly. :meth:`SigningSecrets.get` returns the active secrets,
current first.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy.exc import SQLAlchemyError

from va_common.crypto import CryptoError, Envelope
from va_common.time import Clock
from va_db.repositories import OrgRepo
from va_db.session import Database
from va_db.webhook_secrets import active_signing_secrets

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
        now = self._clock.now()
        try:
            async with self._database.global_session() as session:
                org = await OrgRepo(session).get(org_id)
                active = None if org is None else await active_signing_secrets(self._envelope, org, now=now)
        except (CryptoError, SQLAlchemyError, OSError) as exc:
            raise SecretUnavailableError(type(exc).__name__) from exc
        if active is None:
            msg = "org not found"
            raise SecretUnavailableError(msg)
        lifetime = self._ttl_s
        if active.previous_expires_at is not None:
            lifetime = min(lifetime, (active.previous_expires_at - now).total_seconds())
        self._cache[org_id] = (self._clock.monotonic() + lifetime, active.secrets)
        return active.secrets

"""API keys: lookup across orgs for authentication (specs/03-data-model.md §2; specs/04 §2.2), org reads.

A request carrying an API key has no org context yet, so the key is found by its ``prefix`` on the system
session. The record carries what the auth middleware needs to build a request context and size its rate
buckets without another query (the org's ``status``, ``plan_id`` and ``limits.rate_per_sec`` override) and
serialises to the JSON stored in the ``apikey:{prefix}`` cache. :class:`ApiKeyRepo` reads one org's keys on a
``tenant_session`` (RLS plus an explicit ``org_id`` filter).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import ApiKey, Org
from va_db.repositories.base import SystemRepository

__all__ = ["ApiKeyLookupRepo", "ApiKeyRecord", "ApiKeyRepo", "rate_per_sec_override"]

RATE_PER_SEC_KEY = "rate_per_sec"


def rate_per_sec_override(limits: object) -> int | None:
    """``orgs.limits.rate_per_sec`` when it is a positive integer (booleans excluded), else ``None``."""
    if not isinstance(limits, dict):
        return None
    value = limits.get(RATE_PER_SEC_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    """One ``api_keys`` row joined with its org's status and plan; never holds the plaintext key."""

    id: uuid.UUID
    org_id: uuid.UUID
    secret_hash: str
    scopes: tuple[str, ...]
    environment: str
    pepper_version: int
    revoked: bool
    expires_at: datetime | None
    org_status: str
    plan_id: str
    rate_per_sec: int | None = None

    def to_json(self) -> dict[str, Any]:
        """The cache form: UUIDs as strings, ``expires_at`` as ISO 8601 or ``null``."""
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "secret_hash": self.secret_hash,
            "scopes": list(self.scopes),
            "environment": self.environment,
            "pepper_version": self.pepper_version,
            "revoked": self.revoked,
            "expires_at": None if self.expires_at is None else self.expires_at.isoformat(),
            "org_status": self.org_status,
            "plan_id": self.plan_id,
            "rate_per_sec": self.rate_per_sec,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ApiKeyRecord:
        """Inverse of :meth:`to_json`; ``KeyError``/``ValueError``/``TypeError`` on a malformed entry.

        An entry without ``rate_per_sec`` (written before the field existed) reads as no override.
        """
        expires_at = data["expires_at"]
        rate_per_sec = data.get("rate_per_sec")
        return cls(
            id=uuid.UUID(data["id"]),
            org_id=uuid.UUID(data["org_id"]),
            secret_hash=str(data["secret_hash"]),
            scopes=tuple(str(scope) for scope in data["scopes"]),
            environment=str(data["environment"]),
            pepper_version=int(data["pepper_version"]),
            revoked=bool(data["revoked"]),
            expires_at=None if expires_at is None else datetime.fromisoformat(expires_at),
            org_status=str(data["org_status"]),
            plan_id=str(data["plan_id"]),
            rate_per_sec=None if rate_per_sec is None else int(rate_per_sec),
        )


class ApiKeyLookupRepo(SystemRepository):
    """Finds an API key by prefix before any org context exists and records its use (system scope)."""

    async def get_by_prefix(self, prefix: str) -> ApiKeyRecord | None:
        """The key with ``prefix`` plus its org's status/plan/rate override, revoked or not; else ``None``."""
        statement = (
            select(ApiKey, Org.status, Org.plan_id, Org.limits)
            .join(Org, Org.id == ApiKey.org_id)
            .where(ApiKey.prefix == prefix)
        )
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return None
        key, org_status, plan_id, limits = row
        return ApiKeyRecord(
            id=key.id,
            org_id=key.org_id,
            secret_hash=key.secret_hash,
            scopes=tuple(key.scopes),
            environment=key.environment,
            pepper_version=key.pepper_version,
            revoked=key.revoked_at is not None,
            expires_at=key.expires_at,
            org_status=org_status,
            plan_id=plan_id,
            rate_per_sec=rate_per_sec_override(limits),
        )

    async def touch(self, api_key_id: uuid.UUID, now: datetime) -> None:
        """Set ``last_used_at`` of key ``api_key_id`` to ``now``."""
        await self.session.execute(update(ApiKey).where(ApiKey.id == api_key_id).values(last_used_at=now))


class ApiKeyRepo:
    """The API keys of one org; use it inside ``tenant_session(org_id)`` with the same org."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self.session = session
        self.org_id = org_id

    async def get(self, key_id: uuid.UUID) -> ApiKey | None:
        """The org's key ``key_id`` (revoked or not), or ``None`` (also for another org's key)."""
        statement = select(ApiKey).where(ApiKey.org_id == self.org_id, ApiKey.id == key_id)
        return await self.session.scalar(statement)

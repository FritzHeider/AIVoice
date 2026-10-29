"""API key lookup across orgs for authentication (specs/03-data-model.md §2; specs/04 §2.2).

A request carrying an API key has no org context yet, so the key is found by its ``prefix`` on the system
session. The record carries what the auth middleware needs to build a request context without another query
(the org's ``status`` and ``plan_id``) and serialises to the JSON stored in the ``apikey:{prefix}`` cache.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from va_db.models import ApiKey, Org
from va_db.repositories.base import SystemRepository

__all__ = ["ApiKeyLookupRepo", "ApiKeyRecord"]


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
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ApiKeyRecord:
        """Inverse of :meth:`to_json`; ``KeyError``/``ValueError``/``TypeError`` on a malformed entry."""
        expires_at = data["expires_at"]
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
        )


class ApiKeyLookupRepo(SystemRepository):
    """Finds an API key by prefix before any org context exists and records its use (system scope)."""

    async def get_by_prefix(self, prefix: str) -> ApiKeyRecord | None:
        """The key with ``prefix`` and its org's status/plan, revoked or not; ``None`` when unknown."""
        statement = (
            select(ApiKey, Org.status, Org.plan_id)
            .join(Org, Org.id == ApiKey.org_id)
            .where(ApiKey.prefix == prefix)
        )
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return None
        key, org_status, plan_id = row
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
        )

    async def touch(self, api_key_id: uuid.UUID, now: datetime) -> None:
        """Set ``last_used_at`` of key ``api_key_id`` to ``now``."""
        await self.session.execute(update(ApiKey).where(ApiKey.id == api_key_id).values(last_used_at=now))

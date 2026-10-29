"""Dashboard sessions: creation, resolution with sliding expiry, revocation (specs/04 §4.3).

A session lives 14 days after its last extension and at most 90 days after creation. A request extends it to
``min(now + 14 d, created_at + 90 d)`` when that is more than an hour past the current ``expires_at``, so the
row is written at most hourly and the absolute cap needs no separate check. Resolved sessions are cached at
``sess:{token_hash}`` for at most 5 minutes (never past ``expires_at``); revocation through this manager
deletes the entry. Redis being unavailable never fails a request: the database answers. Only the token hash
is stored or cached; the plaintext token exists in the cookie alone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from va_common import json
from va_common.auth import looks_like_token, sha256_hex
from va_common.logging import get_logger
from va_common.time import Clock
from va_db.models import Session
from va_db.repositories import IdentityRepo
from va_db.session import Database

__all__ = [
    "ABSOLUTE_TTL",
    "EXTEND_AFTER",
    "SESSION_CACHE_TTL_S",
    "SESSION_INVALID",
    "SLIDING_TTL",
    "ActiveSession",
    "SessionManager",
    "cache_ttl_s",
    "next_expiry",
    "session_cache_key",
]

SLIDING_TTL: Final = timedelta(days=14)
EXTEND_AFTER: Final = timedelta(hours=1)
ABSOLUTE_TTL: Final = timedelta(days=90)
SESSION_CACHE_TTL_S: Final = 300
SESSION_INVALID: Final = "Session is invalid or expired."

_log = get_logger(__name__)


def session_cache_key(token_hash: str) -> str:
    """``sess:{token_hash}`` with the hash as the Redis Cluster hash tag (specs/03-data-model.md §11)."""
    return f"sess:{{{token_hash}}}"


def next_expiry(*, created_at: datetime, expires_at: datetime, now: datetime) -> datetime | None:
    """The extended ``expires_at`` for a request at ``now``, or ``None`` when no write is due."""
    target = min(now + SLIDING_TTL, created_at + ABSOLUTE_TTL)
    return target if target - expires_at > EXTEND_AFTER else None


def cache_ttl_s(expires_at: datetime, now: datetime) -> int:
    """Seconds to cache a session: at most 300, never past its expiry, at least 1."""
    remaining = int((expires_at - now).total_seconds())
    return max(1, min(SESSION_CACHE_TTL_S, remaining))


@dataclass(frozen=True, slots=True)
class ActiveSession:
    """A valid session of an active user, as resolved for one request."""

    session_id: uuid.UUID
    token_hash: str
    user_id: uuid.UUID
    active_org_id: uuid.UUID | None
    mfa_passed: bool
    mfa_passed_at: datetime | None
    created_at: datetime
    expires_at: datetime

    @classmethod
    def of(cls, row: Session) -> ActiveSession:
        return cls(
            session_id=row.id,
            token_hash=row.token_hash,
            user_id=row.user_id,
            active_org_id=row.active_org_id,
            mfa_passed=row.mfa_passed,
            mfa_passed_at=row.mfa_passed_at,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )

    def to_json(self) -> dict[str, Any]:
        """The cache form (the token hash is the cache key, not part of the value)."""
        return {
            "session_id": str(self.session_id),
            "user_id": str(self.user_id),
            "active_org_id": None if self.active_org_id is None else str(self.active_org_id),
            "mfa_passed": self.mfa_passed,
            "mfa_passed_at": None if self.mfa_passed_at is None else self.mfa_passed_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any], token_hash: str) -> ActiveSession:
        """Inverse of :meth:`to_json`; ``KeyError``/``ValueError``/``TypeError`` on a malformed entry.

        An entry written before ``mfa_passed_at`` existed reads it as ``None``.
        """
        active_org_id = data["active_org_id"]
        mfa_passed_at = data.get("mfa_passed_at")
        return cls(
            session_id=uuid.UUID(data["session_id"]),
            token_hash=token_hash,
            user_id=uuid.UUID(data["user_id"]),
            active_org_id=None if active_org_id is None else uuid.UUID(active_org_id),
            mfa_passed=bool(data["mfa_passed"]),
            mfa_passed_at=None if mfa_passed_at is None else datetime.fromisoformat(mfa_passed_at),
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
        )


class SessionManager:
    """Creates, resolves, and revokes sessions through ``global_session`` plus the ``sess:`` cache."""

    def __init__(self, *, database: Database, redis: Redis, clock: Clock) -> None:
        self._database = database
        self._redis = redis
        self._clock = clock

    async def start(
        self,
        *,
        user_id: uuid.UUID,
        active_org_id: uuid.UUID | None,
        ip: str | None,
        user_agent: str | None,
    ) -> tuple[str, ActiveSession]:
        """Log the user in: a new session expiring in 14 days plus ``last_login_at``; the cookie token too."""
        async with self._database.global_session() as session:
            token, active = await self.start_in(
                session, user_id=user_id, active_org_id=active_org_id, ip=ip, user_agent=user_agent
            )
        await self.remember(active)
        return token, active

    async def start_in(  # noqa: PLR0913 - keyword-only session attributes
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        active_org_id: uuid.UUID | None,
        ip: str | None,
        user_agent: str | None,
        mfa_passed: bool = False,
    ) -> tuple[str, ActiveSession]:
        """:meth:`start` inside the caller's transaction (signup creates the user in the same one).

        ``mfa_passed`` (an SSO login, whose identity provider is trusted for MFA) also sets ``mfa_passed_at``.
        Nothing is cached: the caller calls :meth:`remember` after its transaction commits.
        """
        now = self._clock.now()
        repo = IdentityRepo(session)
        issued = await repo.create_session(
            user_id=user_id,
            active_org_id=active_org_id,
            expires_at=now + SLIDING_TTL,
            ip=ip,
            user_agent=user_agent,
            mfa_passed=mfa_passed,
            mfa_passed_at=now if mfa_passed else None,
            created_at=now,
        )
        await repo.record_login(user_id, now)
        return issued.token, ActiveSession.of(issued.session)

    async def remember(self, active: ActiveSession) -> None:
        """Cache ``active`` at ``sess:{token_hash}``; a Redis failure is logged, never raised."""
        ttl = cache_ttl_s(active.expires_at, self._clock.now())
        try:
            await self._redis.set(session_cache_key(active.token_hash), json.dumps(active.to_json()), ex=ttl)
        except (RedisError, OSError) as exc:
            _log.warning("session_cache_unavailable", error=type(exc).__name__)

    async def resolve(self, token: str) -> ActiveSession | None:
        """The session of cookie ``token`` (extended when due), or ``None`` when invalid or expired.

        A disabled user's session is rejected when it is loaded from the database.
        """
        if not looks_like_token(token):
            return None
        token_hash = sha256_hex(token)
        now = self._clock.now()
        active = await self._cached(token_hash)
        loaded = active is None
        if active is None:
            active = await self._load(token, now)
        if active is None or active.expires_at <= now:
            return None
        expiry = next_expiry(created_at=active.created_at, expires_at=active.expires_at, now=now)
        if expiry is not None:
            async with self._database.global_session() as session:
                await IdentityRepo(session).extend_session(active.session_id, expiry)
            active = replace(active, expires_at=expiry)
        if loaded or expiry is not None:
            await self.remember(active)
        return active

    async def set_active_org(self, active: ActiveSession, org_id: uuid.UUID) -> ActiveSession:
        """Persist ``org_id`` as the session's active org."""
        async with self._database.global_session() as session:
            await IdentityRepo(session).set_active_org(active.session_id, org_id)
        updated = replace(active, active_org_id=org_id)
        await self.remember(updated)
        return updated

    async def mark_mfa_passed(self, active: ActiveSession) -> ActiveSession:
        """Record that the session passed MFA now (recent auth counts from this time too)."""
        now = self._clock.now()
        async with self._database.global_session() as session:
            await IdentityRepo(session).set_mfa_passed(active.session_id, now)
        updated = replace(active, mfa_passed=True, mfa_passed_at=now)
        await self.remember(updated)
        return updated

    async def revoke(self, active: ActiveSession) -> int:
        """Revoke one session; how many rows were revoked (0 or 1)."""
        async with self._database.global_session() as session:
            revoked = await IdentityRepo(session).revoke_session(active.session_id, self._clock.now())
        await self.forget([active.token_hash])
        return int(revoked)

    async def revoke_all(self, user_id: uuid.UUID) -> int:
        """Revoke every session of the user; how many were revoked."""
        async with self._database.global_session() as session:
            hashes = await IdentityRepo(session).revoke_user_sessions(user_id, self._clock.now())
        await self.forget(hashes)
        return len(hashes)

    async def _load(self, token: str, now: datetime) -> ActiveSession | None:
        async with self._database.global_session() as session:
            found = await IdentityRepo(session).get_active_session_with_user(token, now)
        if found is None:
            return None
        row, user = found
        if user.status != "active":
            return None
        return ActiveSession.of(row)

    async def _cached(self, token_hash: str) -> ActiveSession | None:
        try:
            raw = await self._redis.get(session_cache_key(token_hash))
        except (RedisError, OSError) as exc:
            _log.warning("session_cache_unavailable", error=type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            return ActiveSession.from_json(json.loads(raw), token_hash)
        except (KeyError, TypeError, ValueError):
            _log.warning("session_cache_invalid")
            return None

    async def forget(self, token_hashes: list[str]) -> None:
        """Drop the cache entries of sessions revoked in the caller's own transaction (after it commits)."""
        if not token_hashes:
            return
        try:
            await self._redis.delete(*(session_cache_key(token_hash) for token_hash in token_hashes))
        except (RedisError, OSError) as exc:
            _log.warning("session_cache_unavailable", error=type(exc).__name__)

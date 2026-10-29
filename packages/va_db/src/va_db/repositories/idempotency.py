"""``idempotency_keys`` claims, stored responses, and releases (specs/04-auth-tenancy-rbac.md §12).

Tenant-scoped: use it inside ``tenant_session(org_id)`` with the same org (RLS); every statement still filters
``org_id`` explicitly. A claim inserts the row with ``locked_until = now + LOCK_TTL`` (``ON CONFLICT DO
NOTHING``); an existing row is then inspected under ``FOR UPDATE``. Times come from the caller's clock, so
``created_at`` and ``locked_until`` are written explicitly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol

from sqlalchemy import delete, null, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import IdempotencyKey

__all__ = [
    "LOCK_TTL",
    "REPLAY_WINDOW",
    "ClaimOutcome",
    "IdempotencyClaim",
    "IdempotencyConflict",
    "IdempotencyReplay",
    "IdempotencyRepo",
]

LOCK_TTL: Final = timedelta(seconds=60)
"""How long a claim blocks duplicates before another request may take it over (a crashed worker)."""
REPLAY_WINDOW: Final = timedelta(hours=24)
"""Age after which a row is treated as absent; the retention purge deletes rows past the same age."""
_CLAIM_ATTEMPTS: Final = 2


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    """The caller now owns ``key`` and must ``complete`` or ``release`` it."""

    key: str


@dataclass(frozen=True, slots=True)
class IdempotencyReplay:
    """A response was already stored for the same request: replay it."""

    status: int
    body: Any


class IdempotencyConflict(StrEnum):
    """Why a claim was refused; the values are the specs/31 error codes."""

    IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"


type ClaimOutcome = IdempotencyClaim | IdempotencyReplay | IdempotencyConflict


class Takeover(StrEnum):
    """An existing row the claimant takes over."""

    EXPIRED = "expired"
    """Older than :data:`REPLAY_WINDOW`: rewrite it as a fresh claim."""
    UNLOCKED = "unlocked"
    """No response and the lock expired (a crashed worker): lock it again."""


class StoredRow(Protocol):
    """The ``idempotency_keys`` columns a claim decision reads."""

    @property
    def created_at(self) -> datetime: ...
    @property
    def request_hash(self) -> str: ...
    @property
    def response_status(self) -> int | None: ...
    @property
    def response_body(self) -> Any: ...
    @property
    def locked_until(self) -> datetime | None: ...


def resolve_existing(
    row: StoredRow, request_hash: str, now: datetime
) -> IdempotencyReplay | IdempotencyConflict | Takeover:
    """The outcome of claiming an existing row: expiry first, then a different hash, a response, a lock."""
    if row.created_at < now - REPLAY_WINDOW:
        return Takeover.EXPIRED
    if row.request_hash != request_hash:
        return IdempotencyConflict.KEY_REUSED
    if row.response_status is not None:
        return IdempotencyReplay(row.response_status, row.response_body)
    if row.locked_until is not None and row.locked_until > now:
        return IdempotencyConflict.IN_PROGRESS
    return Takeover.UNLOCKED


class IdempotencyRepo:
    """Idempotency rows of one org."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self.session = session
        self.org_id = org_id

    def _row_filter(self, key: str) -> tuple[Any, Any]:
        return IdempotencyKey.org_id == self.org_id, IdempotencyKey.key == key

    async def claim(
        self, *, key: str, method: str, path: str, request_hash: str, now: datetime
    ) -> ClaimOutcome:
        """Claim ``key`` for a request, or say why not (mismatched hash first, then stored, then locked).

        A row older than :data:`REPLAY_WINDOW` is re-claimed in place as if absent; a row without a response
        whose lock has expired is re-locked for this caller.
        """
        fresh = {
            "method": method,
            "path": path,
            "request_hash": request_hash,
            "response_status": None,
            "response_body": null(),  # SQL NULL, not the JSON null a plain None would store
            "locked_until": now + LOCK_TTL,
            "created_at": now,
            "updated_at": now,
        }
        for _ in range(_CLAIM_ATTEMPTS):
            inserted = await self.session.execute(
                insert(IdempotencyKey)
                .values(org_id=self.org_id, key=key, **fresh)
                .on_conflict_do_nothing(index_elements=[IdempotencyKey.org_id, IdempotencyKey.key])
                .returning(IdempotencyKey.key)
            )
            if inserted.scalar_one_or_none() is not None:
                return IdempotencyClaim(key)
            row = (
                await self.session.execute(
                    select(IdempotencyKey).where(*self._row_filter(key)).with_for_update()
                )
            ).scalar_one_or_none()
            if row is not None:  # None: released between the insert and the select, so insert again
                return await self._resolve(row, request_hash, fresh, now)
        return IdempotencyConflict.IN_PROGRESS

    async def _resolve(
        self, row: IdempotencyKey, request_hash: str, fresh: dict[str, Any], now: datetime
    ) -> ClaimOutcome:
        outcome = resolve_existing(row, request_hash, now)
        if outcome is Takeover.EXPIRED:
            await self._set(row.key, fresh)
        elif outcome is Takeover.UNLOCKED:
            await self._set(row.key, {"locked_until": now + LOCK_TTL, "updated_at": now})
        else:
            return outcome
        return IdempotencyClaim(row.key)

    async def complete(self, key: str, *, status: int, body: Any, now: datetime) -> None:
        """Store the response of the claimed request and clear its lock."""
        await self._set(
            key, {"response_status": status, "response_body": body, "locked_until": None, "updated_at": now}
        )

    async def release(self, key: str) -> None:
        """Delete the claim of a request whose response is not stored (a stored row is never deleted)."""
        await self.session.execute(
            delete(IdempotencyKey).where(*self._row_filter(key), IdempotencyKey.response_status.is_(None))
        )

    async def _set(self, key: str, values: dict[str, Any]) -> None:
        await self.session.execute(update(IdempotencyKey).where(*self._row_filter(key)).values(**values))

"""An in-memory ``IdempotencyStore`` deciding claims with ``IdempotencyRepo``'s ``resolve_existing``."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from va_common.time import FrozenClock
from va_db.repositories import LOCK_TTL, ClaimOutcome, IdempotencyClaim, Takeover, resolve_existing

__all__ = ["MemoryIdempotencyStore", "StoredKey"]


@dataclass(frozen=True, slots=True)
class StoredKey:
    """One ``idempotency_keys`` row."""

    method: str
    path: str
    request_hash: str
    created_at: datetime
    locked_until: datetime | None
    response_status: int | None = None
    response_body: Any = None


@dataclass
class MemoryIdempotencyStore:
    """Rows keyed by ``(org_id, key)``; ``fail_complete`` makes ``complete`` raise ``OSError``."""

    clock: FrozenClock
    rows: dict[tuple[uuid.UUID, str], StoredKey] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    fail_complete: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def claim(
        self, org_id: uuid.UUID, *, key: str, method: str, path: str, request_hash: str
    ) -> ClaimOutcome:
        self.calls.append("claim")
        now = self.clock.now()
        async with self._lock:
            row = self.rows.get((org_id, key))
            if row is not None:
                outcome = resolve_existing(row, request_hash, now)
                if outcome is Takeover.UNLOCKED:
                    self.rows[org_id, key] = replace(row, locked_until=now + LOCK_TTL)
                    return IdempotencyClaim(key)
                if outcome is not Takeover.EXPIRED:
                    return outcome
            self.rows[org_id, key] = StoredKey(method, path, request_hash, now, now + LOCK_TTL)
            return IdempotencyClaim(key)

    async def complete(self, org_id: uuid.UUID, key: str, *, status: int, body: Any) -> None:
        self.calls.append("complete")
        if self.fail_complete:
            msg = "idempotency store unavailable"
            raise OSError(msg)
        async with self._lock:
            row = self.rows[org_id, key]
            self.rows[org_id, key] = replace(
                row, response_status=status, response_body=body, locked_until=None
            )

    async def release(self, org_id: uuid.UUID, key: str) -> None:
        self.calls.append("release")
        async with self._lock:
            row = self.rows.get((org_id, key))
            if row is not None and row.response_status is None:
                del self.rows[org_id, key]

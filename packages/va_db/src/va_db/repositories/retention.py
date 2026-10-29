"""Retention inputs and the expired-row purge (specs/03-data-model.md §2; specs/28 §8.1 step 5).

Per-org deletions (call events, turns, messages, recordings) belong to the RetentionWorkflow activities of a
later phase; this repository lists the per-org policies and purges the fixed-window system tables.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import ColumnClause, delete, literal_column, select

from va_db.base import Base
from va_db.models import Org
from va_db.repositories.base import SystemRepository, require_positive
from va_db.repositories.idempotency import REPLAY_WINDOW

__all__ = ["PURGE_RULES", "PurgeRule", "RetentionPolicy", "RetentionRepo"]


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """One org's ``retention_days`` and ``recording_retention_days``."""

    org_id: uuid.UUID
    retention_days: int
    recording_retention_days: int


@dataclass(frozen=True, slots=True)
class PurgeRule:
    """Delete rows of ``table`` whose ``column`` is before ``now - age`` (``age`` None: before ``now``)."""

    table: str
    column: str
    age: timedelta | None

    def cutoff(self, now: datetime) -> datetime:
        """The instant before which rows are purged."""
        return now if self.age is None else now - self.age


PURGE_RULES: Final[tuple[PurgeRule, ...]] = (
    PurgeRule("idempotency_keys", "created_at", REPLAY_WINDOW),
    PurgeRule("telnyx_events", "created_at", timedelta(days=30)),
    PurgeRule("stripe_events", "created_at", timedelta(days=90)),
    PurgeRule("webhook_deliveries", "created_at", timedelta(days=30)),
    PurgeRule("sessions", "expires_at", None),
    PurgeRule("email_tokens", "expires_at", None),
    PurgeRule("invitations", "expires_at", None),
)
"""specs/28 §8.1 step 5, in the order the spec lists them."""

_CTID: Final[ColumnClause[Any]] = literal_column("ctid")


class RetentionRepo(SystemRepository):
    """Reads every org's retention settings and purges expired system rows (system scope, specs/03 §2)."""

    async def retention_policies(self) -> list[RetentionPolicy]:
        """Every org's retention settings, ordered by org id."""
        statement = select(Org.id, Org.retention_days, Org.recording_retention_days).order_by(Org.id)
        rows = (await self.session.execute(statement)).all()
        return [
            RetentionPolicy(org_id=row[0], retention_days=row[1], recording_retention_days=row[2])
            for row in rows
        ]

    async def _purge(self, rule: PurgeRule, now: datetime, batch_size: int) -> int:
        table = Base.metadata.tables[rule.table]
        batch = (
            select(_CTID).select_from(table).where(table.c[rule.column] < rule.cutoff(now)).limit(batch_size)
        )
        statement = delete(table).where(_CTID.in_(batch)).returning(_CTID)
        total = 0
        while True:
            deleted = len((await self.session.execute(statement)).all())
            total += deleted
            if deleted < batch_size:
                return total

    async def purge_expired(self, now: datetime, *, batch_size: int = 1000) -> dict[str, int]:
        """Apply every :data:`PURGE_RULES` entry in batches of ``batch_size``; deleted counts per table."""
        require_positive("batch_size", batch_size)
        return {rule.table: await self._purge(rule, now, batch_size) for rule in PURGE_RULES}

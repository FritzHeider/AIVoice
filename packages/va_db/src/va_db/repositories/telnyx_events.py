"""Raw inbound Telnyx webhooks with dedupe (specs/03-data-model.md §2)."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from va_db.models import TelnyxEvent
from va_db.repositories.base import NotFoundError, SystemRepository

__all__ = ["NewTelnyxEvent", "TelnyxEventRepo"]


@dataclass(frozen=True, slots=True)
class NewTelnyxEvent:
    """A verified inbound Telnyx webhook; ``telnyx_event_id`` is the payload's ``data.id``."""

    telnyx_event_id: str
    event_type: str
    occurred_at: datetime
    payload: dict[str, Any]
    org_id: uuid.UUID | None = None
    call_id: uuid.UUID | None = None
    call_control_id: str | None = None


class TelnyxEventRepo(SystemRepository):
    """Stores Telnyx events before any org is known and marks them processed (system scope, specs/03 §2)."""

    async def record(self, event: NewTelnyxEvent) -> TelnyxEvent | None:
        """Insert the event; ``None`` when ``telnyx_event_id`` was already recorded (a redelivery)."""
        statement = (
            insert(TelnyxEvent)
            .values(**asdict(event))
            .on_conflict_do_nothing(index_elements=[TelnyxEvent.telnyx_event_id])
            .returning(TelnyxEvent)
        )
        return await self.session.scalar(statement)

    async def mark_processed(self, event_id: uuid.UUID, *, now: datetime, error: str | None = None) -> None:
        """Set ``processed_at`` and ``process_error`` (``None`` = processed without error)."""
        statement = (
            update(TelnyxEvent)
            .where(TelnyxEvent.id == event_id)
            .values(processed_at=now, process_error=error)
            .returning(TelnyxEvent.id)
        )
        if await self.session.scalar(statement) is None:
            raise NotFoundError(TelnyxEvent.__tablename__)

"""Raw inbound Telnyx webhooks with dedupe and reprocessing (specs/03-data-model.md §2; specs/06 §4.1)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from va_db.models import Org, TelnyxEvent
from va_db.repositories.base import NotFoundError, SystemRepository, require_positive

__all__ = ["RECORDING_SAVED_EVENT", "RETRY_CLAIM_LIMIT", "NewTelnyxEvent", "TelnyxEventRepo"]

RETRY_CLAIM_LIMIT: Final = 200
RECORDING_SAVED_EVENT: Final = "call.recording.saved"


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
        """Insert the event; ``None`` when ``telnyx_event_id`` was already recorded (a redelivery).

        ``org_id`` is attribution only: an org that does not exist (deleted, or a forged ``client_state`` that
        still carried a valid signature) stores NULL instead of failing the insert on the foreign key.
        """
        values: dict[str, Any] = asdict(event)
        if event.org_id is not None:
            values["org_id"] = select(Org.id).where(Org.id == event.org_id).scalar_subquery()
        statement = (
            insert(TelnyxEvent)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[TelnyxEvent.telnyx_event_id])
            .returning(TelnyxEvent)
        )
        return await self.session.scalar(statement)

    async def mark_processed(
        self,
        event_id: uuid.UUID,
        *,
        now: datetime,
        error: str | None = None,
        settled_attempts: int | None = None,
    ) -> None:
        """Set ``processed_at`` and ``process_error`` (``None`` = processed without error) and count the
        attempt in ``process_attempts``.

        ``settled_attempts`` marks an outcome that must never be retried although ``process_error`` records a
        reason (an inbound refusal, specs/06 §7 Decision P05-T12b): ``process_attempts`` is raised to at least
        that value (the reprocessor's ``max_attempts``), so :meth:`claim_retries` skips the row.
        """
        attempts = TelnyxEvent.process_attempts + 1
        if settled_attempts is not None:
            attempts = func.greatest(attempts, require_positive("settled_attempts", settled_attempts))
        statement = (
            update(TelnyxEvent)
            .where(TelnyxEvent.id == event_id)
            .values(processed_at=now, process_error=error, process_attempts=attempts)
            .returning(TelnyxEvent.id)
        )
        if await self.session.scalar(statement) is None:
            raise NotFoundError(TelnyxEvent.__tablename__)

    async def claim_retries(
        self,
        *,
        now: datetime,
        stale_after: timedelta,
        retry_after: timedelta,
        max_attempts: int,
        limit: int = RETRY_CLAIM_LIMIT,
    ) -> list[TelnyxEvent]:
        """Claim up to ``limit`` events that need another processing attempt, oldest first.

        Due are events never processed whose row is older than ``stale_after`` (lost from an in-process queue,
        dropped by backpressure, or left by a crashed process) and failed events with fewer than
        ``max_attempts`` attempts whose last change is older than ``retry_after``. Claiming touches the row
        (``updated_at``), so another edge process skips it for the same windows; rows locked by another claim
        are skipped.
        """
        due = (
            select(TelnyxEvent.id)
            .where(
                or_(
                    and_(TelnyxEvent.processed_at.is_(None), TelnyxEvent.updated_at < now - stale_after),
                    and_(
                        TelnyxEvent.process_error.is_not(None),
                        TelnyxEvent.process_attempts < require_positive("max_attempts", max_attempts),
                        TelnyxEvent.updated_at < now - retry_after,
                    ),
                )
            )
            .order_by(TelnyxEvent.created_at, TelnyxEvent.id)
            .limit(require_positive("limit", limit))
            .with_for_update(skip_locked=True)
        )
        statement = (
            update(TelnyxEvent)
            .where(TelnyxEvent.id.in_(due))
            .values(updated_at=now)
            .returning(TelnyxEvent)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        claimed = list((await self.session.scalars(statement)).all())
        return sorted(claimed, key=lambda row: (row.created_at, row.id))

    async def recording_saved(
        self, call_id: uuid.UUID, call_control_ids: Sequence[str] = ()
    ) -> TelnyxEvent | None:
        """The newest stored ``call.recording.saved`` of the call: attributed to ``call_id`` or sent for one
        of its legs' ``call_control_ids`` (an event stored before the edge resolved its call); ``None`` when
        none was stored (``PostCallWorkflow`` step 2, specs/18 §1 Decision P05-T18b)."""
        belongs = TelnyxEvent.call_id == call_id
        if call_control_ids:
            belongs = or_(belongs, TelnyxEvent.call_control_id.in_(list(call_control_ids)))
        statement = (
            select(TelnyxEvent)
            .where(TelnyxEvent.event_type == RECORDING_SAVED_EVENT, belongs)
            .order_by(TelnyxEvent.occurred_at.desc(), TelnyxEvent.id.desc())
            .limit(1)
        )
        return await self.session.scalar(statement)

"""Read a call's event log page by page (``GET /v1/calls/{call_id}/events``, specs/05-calls-api.md §7.9;
Decision P05-T16b).

Runs in the caller's ``tenant_session``; rows are filtered by ``org_id`` and ``call_id`` and ordered by
``seq`` (index ``call_events_call_idx``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import CallEvent

__all__ = ["load_call_events"]


async def load_call_events(  # noqa: PLR0913 - one keyword per filter of the endpoint
    session: AsyncSession,
    org_id: uuid.UUID,
    call_id: uuid.UUID,
    *,
    after_seq: int,
    limit: int,
    categories: Sequence[str] | None,
) -> tuple[list[CallEvent], int | None]:
    """Up to ``limit`` events with ``seq > after_seq`` (only ``categories`` when given), oldest first, and
    the ``after_seq`` of the next page (``None`` on the last page)."""
    statement = select(CallEvent).where(
        CallEvent.org_id == org_id, CallEvent.call_id == call_id, CallEvent.seq > after_seq
    )
    if categories is not None:
        statement = statement.where(CallEvent.category.in_(categories))
    rows = list(await session.scalars(statement.order_by(CallEvent.seq).limit(limit + 1)))
    page = rows[:limit]
    return page, (page[-1].seq if len(rows) > limit else None)

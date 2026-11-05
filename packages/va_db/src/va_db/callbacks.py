"""Callback calls of the ``schedule_callback`` system tool (specs/13 §6; Decision P08-T02b2).

:func:`schedule_callback` creates the callback child of a call in the caller's tenant session: the parent row
is locked (``FOR UPDATE``) and a row with the deterministic ``child_id`` that already exists is returned with
``created=False``, so a repeated tool call (same call, same tool call id) schedules one callback only. The
child is a scheduled outbound call to the other party (:func:`va_db.call_children.child_call`) with the
parent's configuration (:func:`va_domain.call_callback.callback_config`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.call_children import child_call
from va_db.models import Call
from va_db.repositories import NotFoundError
from va_domain.call_callback import callback_config

__all__ = ["ScheduledCallback", "schedule_callback"]


@dataclass(frozen=True, slots=True)
class ScheduledCallback:
    """The callback child: its id, when it is due, and whether this request created it."""

    call_id: uuid.UUID
    scheduled_for: datetime
    created: bool


async def schedule_callback(  # noqa: PLR0913 - the parent, the child's identity and keyword-only content
    session: AsyncSession,
    org_id: uuid.UUID,
    parent_id: uuid.UUID,
    *,
    child_id: uuid.UUID,
    scheduled_for: datetime,
    note: str | None,
) -> ScheduledCallback:
    """Create (or find) the callback child ``child_id`` of call ``parent_id``; :class:`NotFoundError` when the
    parent is not a call of ``org_id``."""
    parent = (
        await session.execute(
            select(Call).where(Call.id == parent_id, Call.org_id == org_id).with_for_update()
        )
    ).scalar_one_or_none()
    if parent is None:
        raise NotFoundError("Call")
    existing = (
        await session.execute(
            select(Call.start_time).where(
                Call.id == child_id, Call.org_id == org_id, Call.parent_call_id == parent_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return ScheduledCallback(child_id, existing, created=False)
    stored: dict[str, JsonValue] = dict(parent.config)
    child = child_call(
        parent,
        child_id=child_id,
        config=callback_config(stored, scheduled_for, note),
        start_time=scheduled_for,
        outbound=True,
    )
    session.add(child)
    await session.flush()
    return ScheduledCallback(child_id, scheduled_for, created=True)

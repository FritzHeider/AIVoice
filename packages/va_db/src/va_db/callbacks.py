"""Callback calls of the ``schedule_callback`` system tool (specs/13 §6; Decisions P08-T02b2, P08-FIX-13).

:func:`schedule_callback` creates the callback child of a call in the caller's tenant session. The parent row
is locked (``FOR UPDATE``) first, so concurrent runs count the parent's ``scheduled`` children one after the
other: at :data:`MAX_PENDING_CALLBACKS` the request raises :class:`CallbackLimitError` (during a live call
the only children are callbacks; retry children are created after the call ended). The child is a scheduled
outbound call to the other party (:func:`va_db.call_children.child_call`) with the parent's configuration
(:func:`va_domain.call_callback.callback_config`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.call_children import child_call
from va_db.models import Call
from va_db.repositories import NotFoundError
from va_domain.call_callback import callback_config

__all__ = ["MAX_PENDING_CALLBACKS", "CallbackLimitError", "ScheduledCallback", "schedule_callback"]

MAX_PENDING_CALLBACKS: Final = 1
"""Callbacks of one call that may wait (status ``scheduled``) at the same time."""


class CallbackLimitError(Exception):
    """The call already has :data:`MAX_PENDING_CALLBACKS` scheduled callbacks."""


@dataclass(frozen=True, slots=True)
class ScheduledCallback:
    """The created callback child: its id and when it is due."""

    call_id: uuid.UUID
    scheduled_for: datetime


async def schedule_callback(  # noqa: PLR0913 - the parent, the child's identity and keyword-only content
    session: AsyncSession,
    org_id: uuid.UUID,
    parent_id: uuid.UUID,
    *,
    child_id: uuid.UUID,
    scheduled_for: datetime,
    note: str | None,
) -> ScheduledCallback:
    """Create the callback child ``child_id`` of call ``parent_id``; :class:`NotFoundError` when the parent is
    not a call of ``org_id``, :class:`CallbackLimitError` when it has no callback left."""
    parent = (
        await session.execute(
            select(Call).where(Call.id == parent_id, Call.org_id == org_id).with_for_update()
        )
    ).scalar_one_or_none()
    if parent is None:
        raise NotFoundError("Call")
    pending = (
        await session.execute(
            select(func.count(Call.id)).where(
                Call.org_id == org_id, Call.parent_call_id == parent_id, Call.status == "scheduled"
            )
        )
    ).scalar_one()
    if pending >= MAX_PENDING_CALLBACKS:
        raise CallbackLimitError
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
    return ScheduledCallback(child_id, scheduled_for)

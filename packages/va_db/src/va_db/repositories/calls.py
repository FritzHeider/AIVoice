"""Calls and call legs of one org, with guarded state transitions (specs/05-calls-api.md §8; specs/03 §6).

Use the tenant repositories inside ``tenant_session(org_id)`` with the same org. Every status change is one
``UPDATE ... WHERE status = ANY(:allowed_from) RETURNING *`` (specs/05 §8 Decision P05-T02), so concurrent or
redelivered carrier events can never move a call backwards: the loser gets :class:`CallTransitionError`
(call) or ``None`` (leg). :class:`CallLegLookupRepo` is the system-scope lookup of the edge (specs/03 §2).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.errors import AppError
from va_common.ids import new_id
from va_db.call_states import STOPPABLE_STATUSES, allowed_from, leg_allowed_from
from va_db.models import AnsweredBy, Call, CallLeg, CallLegRole, CallLegState, CallStatus
from va_db.repositories.base import NotFoundError, SystemRepository

__all__ = ["CallLegLookupRepo", "CallLegRepo", "CallRepo", "CallTransitionError", "LegRef"]

_PROTECTED: Final = frozenset({"id", "org_id", "status", "state", "created_at", "updated_at"})


class CallTransitionError(AppError):
    """``CONFLICT``: the call is not in a status the transition may start from; ``current`` is its status."""

    def __init__(self, call_id: uuid.UUID, to: CallStatus, current: CallStatus) -> None:
        super().__init__("CONFLICT", f"Call cannot move from {current} to {to}.")
        self.call_id = call_id
        self.to = to
        self.current = current


def _check_values(values: dict[str, Any]) -> None:
    rejected = sorted(_PROTECTED & values.keys())
    if rejected:
        msg = f"transition values may not set {', '.join(rejected)}"
        raise ValueError(msg)


class CallRepo:
    """The calls of one org."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self.session = session
        self.org_id = org_id

    async def get(self, call_id: uuid.UUID) -> Call:
        """The org's call ``call_id``; :class:`NotFoundError` when absent or another org's."""
        statement = select(Call).where(Call.org_id == self.org_id, Call.id == call_id)
        call = await self.session.scalar(statement.execution_options(populate_existing=True))
        if call is None:
            raise NotFoundError("Call")
        return call

    async def transition(
        self,
        call_id: uuid.UUID,
        to: CallStatus,
        *,
        from_: Collection[CallStatus] | None = None,
        **values: Any,
    ) -> Call:
        """Move the call to ``to`` (setting ``values`` too) when its status is in ``from_`` (default: every
        status :func:`va_db.call_states.allowed_from` allows). ``from_`` must be a subset of the allowed
        sources (``ValueError``). A call in another status raises :class:`CallTransitionError`."""
        allowed = allowed_from(to)
        sources = allowed if from_ is None else frozenset(from_)
        if not sources <= allowed:
            msg = f"{to} cannot be entered from {', '.join(sorted(sources - allowed))}"
            raise ValueError(msg)
        _check_values(values)
        statement = (
            update(Call)
            .where(Call.org_id == self.org_id, Call.id == call_id, Call.status.in_(sorted(sources)))
            .values(status=to, **values)
            .returning(Call)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        call = await self.session.scalar(statement)
        if call is None:
            current = await self.get(call_id)
            raise CallTransitionError(call_id, to, current.status)
        return call

    async def active_ids(
        self, call_ids: Collection[uuid.UUID] | None = None
    ) -> list[tuple[uuid.UUID, CallStatus]]:
        """``(id, status)`` of the org's calls in :data:`va_db.call_states.STOPPABLE_STATUSES`, oldest first
        (``created_at``, then ``id``); restricted to ``call_ids`` when given (other orgs' ids never match)."""
        statement = select(Call.id, Call.status).where(
            Call.org_id == self.org_id, Call.status.in_(sorted(STOPPABLE_STATUSES))
        )
        if call_ids is not None:
            statement = statement.where(Call.id.in_(list(call_ids)))
        rows = await self.session.execute(statement.order_by(Call.created_at, Call.id))
        return [(call_id, status) for call_id, status in rows]

    async def set_answered_by(
        self, call_id: uuid.UUID, value: AnsweredBy, *, statuses: Collection[CallStatus]
    ) -> bool:
        """Set ``answered_by`` when the call is in one of ``statuses`` (the answered ones, for a late
        classification); ``False`` when the call is absent, another org's, or in another status."""
        statement = (
            update(Call)
            .where(Call.org_id == self.org_id, Call.id == call_id, Call.status.in_(sorted(statuses)))
            .values(answered_by=value)
            .returning(Call.id)
            .execution_options(synchronize_session=False)
        )
        return await self.session.scalar(statement) is not None

    async def update_fields(self, call_id: uuid.UUID, **values: Any) -> Call:
        """Set ``values`` without a status change (``answered_by``, ...); :class:`NotFoundError` if absent."""
        _check_values(values)
        if not values:
            msg = "update_fields needs at least one value"
            raise ValueError(msg)
        statement = (
            update(Call)
            .where(Call.org_id == self.org_id, Call.id == call_id)
            .values(**values)
            .returning(Call)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        call = await self.session.scalar(statement)
        if call is None:
            raise NotFoundError("Call")
        return call


class CallLegRepo:
    """The call legs of one org, keyed by Telnyx ``call_control_id``."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self.session = session
        self.org_id = org_id

    async def by_control_id(self, call_control_id: str) -> CallLeg | None:
        statement = select(CallLeg).where(
            CallLeg.org_id == self.org_id, CallLeg.call_control_id == call_control_id
        )
        return await self.session.scalar(statement.execution_options(populate_existing=True))

    async def primary(self, call_id: uuid.UUID) -> CallLeg | None:
        """The call's ``primary`` leg (the first recorded when several exist); ``None`` before the dial."""
        statement = (
            select(CallLeg)
            .where(CallLeg.org_id == self.org_id, CallLeg.call_id == call_id, CallLeg.role == "primary")
            .order_by(CallLeg.created_at, CallLeg.id)
            .limit(1)
        )
        return await self.session.scalar(statement.execution_options(populate_existing=True))

    async def record_initiated(  # noqa: PLR0913 - keyword-only leg attributes
        self,
        call_id: uuid.UUID,
        role: CallLegRole,
        call_control_id: str,
        *,
        call_leg_id: str | None = None,
        call_session_id: str | None = None,
        telnyx_account_id: uuid.UUID | None = None,
        started_at: datetime | None = None,
    ) -> CallLeg:
        """Insert the leg in state ``initiated``; when it exists already (redelivery, an event that arrived
        first) only its missing ``call_leg_id`` / ``call_session_id`` are filled in. Returns the row."""
        values: dict[str, Any] = {
            "id": new_id(),
            "org_id": self.org_id,
            "call_id": call_id,
            "role": role,
            "call_control_id": call_control_id,
            "call_leg_id": call_leg_id,
            "call_session_id": call_session_id,
            "telnyx_account_id": telnyx_account_id,
            "state": "initiated",
        }
        if started_at is not None:
            values["started_at"] = started_at
        statement = insert(CallLeg).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[CallLeg.call_control_id],
            set_={
                "call_leg_id": func.coalesce(CallLeg.call_leg_id, statement.excluded.call_leg_id),
                "call_session_id": func.coalesce(CallLeg.call_session_id, statement.excluded.call_session_id),
            },
        )
        returning = statement.returning(CallLeg).execution_options(populate_existing=True)
        return (await self.session.scalars(returning)).one()

    async def advance(self, call_control_id: str, to: CallLegState, **values: Any) -> CallLeg | None:
        """Move the leg to ``to`` when its state allows it (specs/05 §8 Decision P05-T02); ``None`` when the
        leg is absent or the move is illegal (a redelivery or an out-of-order event)."""
        _check_values(values)
        statement = (
            update(CallLeg)
            .where(
                CallLeg.org_id == self.org_id,
                CallLeg.call_control_id == call_control_id,
                CallLeg.state.in_(sorted(leg_allowed_from(to))),
            )
            .values(state=to, **values)
            .returning(CallLeg)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        leg: CallLeg | None = await self.session.scalar(statement)
        return leg

    async def set_amd(self, call_control_id: str, result: str) -> bool:
        """Store the raw AMD ``result`` on the leg; ``False`` when the leg is absent."""
        statement = (
            update(CallLeg)
            .where(CallLeg.org_id == self.org_id, CallLeg.call_control_id == call_control_id)
            .values(amd_result=result)
            .returning(CallLeg.id)
        )
        return await self.session.scalar(statement) is not None


@dataclass(frozen=True, slots=True)
class LegRef:
    """Which org, call, and role a Telnyx ``call_control_id`` belongs to."""

    org_id: uuid.UUID
    call_id: uuid.UUID
    role: CallLegRole


class CallLegLookupRepo(SystemRepository):
    """Attributes a carrier event without a usable ``client_state`` to its call before any org context
    exists (system scope, specs/03 §2)."""

    async def by_call_control_id(self, call_control_id: str) -> LegRef | None:
        statement = select(CallLeg.org_id, CallLeg.call_id, CallLeg.role).where(
            CallLeg.call_control_id == call_control_id
        )
        row = (await self.session.execute(statement)).first()
        if row is None:
            return None
        org_id, call_id, role = row
        return LegRef(org_id=org_id, call_id=call_id, role=role)

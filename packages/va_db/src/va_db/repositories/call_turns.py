"""The transcript turns of one org's calls (specs/03-data-model.md ``call_turns``; specs/07 §8 Decision
P05-T11a).

Rows are keyed by ``(call_id, turn_index, role)``. :meth:`CallTurnRepo.upsert_many` is idempotent: a re-sent
row overwrites the stored values and merges ``latency`` (``stored || sent``, so a latency merged before a
re-sent row survives). Run inside ``tenant_session(org_id)`` (RLS).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from sqlalchemy import BindParameter, bindparam, delete, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.ids import new_id
from va_db.call_turns import CallTurnRow
from va_db.models import CallTurn, CallTurnRole

__all__ = ["CallTurnRepo"]

_UPDATED_COLUMNS = (
    "text",
    "text_spoken",
    "interrupted",
    "start_ms",
    "end_ms",
    "node_id",
    "node_name",
    "language_code",
)


class CallTurnRepo:
    """The ``call_turns`` rows of one org."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self.session = session
        self.org_id = org_id

    async def upsert_many(self, rows: Sequence[CallTurnRow]) -> int:
        """Insert or update ``rows`` in one statement; returns the number of distinct keys written.

        Rows repeating a key within the batch collapse to the last one, with their latencies merged in order.
        """
        unique = _collapse(rows)
        if not unique:
            return 0
        values = [self._values(row) for row in unique]
        statement = insert(CallTurn).values(values)
        excluded = statement.excluded
        updates: dict[str, Any] = {name: excluded[name] for name in _UPDATED_COLUMNS}
        updates["latency"] = CallTurn.latency.op("||")(excluded.latency)
        await self.session.execute(
            statement.on_conflict_do_update(index_elements=["call_id", "turn_index", "role"], set_=updates)
        )
        return len(unique)

    async def merge_latency(
        self, call_id: uuid.UUID, turn_index: int, role: CallTurnRole, latency: dict[str, Any]
    ) -> bool:
        """Merge ``latency`` into the row's ``latency``; ``False`` when the row does not exist."""
        statement = (
            update(CallTurn)
            .where(*self._key(call_id, turn_index, role))
            .values(latency=CallTurn.latency.op("||")(_jsonb(latency)))
            .returning(CallTurn.id)
        )
        return await self.session.scalar(statement) is not None

    async def delete_turn(self, call_id: uuid.UUID, turn_index: int, role: CallTurnRole) -> bool:
        """Delete one row; ``False`` when it did not exist."""
        statement = delete(CallTurn).where(*self._key(call_id, turn_index, role)).returning(CallTurn.id)
        return await self.session.scalar(statement) is not None

    async def list_for_call(self, call_id: uuid.UUID) -> list[CallTurn]:
        """The call's rows ordered by ``(turn_index, role)``."""
        statement = (
            select(CallTurn)
            .where(CallTurn.org_id == self.org_id, CallTurn.call_id == call_id)
            .order_by(CallTurn.turn_index, CallTurn.role)
        )
        return list(await self.session.scalars(statement))

    def _key(self, call_id: uuid.UUID, turn_index: int, role: CallTurnRole) -> tuple[Any, ...]:
        return (
            CallTurn.org_id == self.org_id,
            CallTurn.call_id == call_id,
            CallTurn.turn_index == turn_index,
            CallTurn.role == role,
        )

    def _values(self, row: CallTurnRow) -> dict[str, Any]:
        if row.org_id != self.org_id:
            msg = "a call turn row of another org was given to this repository"
            raise ValueError(msg)
        return {
            "id": new_id(),
            "org_id": row.org_id,
            "call_id": row.call_id,
            "turn_index": row.turn_index,
            "role": row.role,
            "text": row.text,
            "text_spoken": row.text_spoken,
            "interrupted": row.interrupted,
            "start_ms": row.start_ms,
            "end_ms": row.end_ms,
            "node_id": row.node_id,
            "node_name": row.node_name,
            "language_code": row.language_code,
            "latency": row.latency,
        }


def _collapse(rows: Sequence[CallTurnRow]) -> list[CallTurnRow]:
    """One row per key (the last), latencies merged in order; first-seen key order."""
    by_key: dict[tuple[uuid.UUID, int, str], CallTurnRow] = {}
    for row in rows:
        key = (row.call_id, row.turn_index, row.role)
        earlier = by_key.get(key)
        by_key[key] = row if earlier is None else replace(row, latency={**earlier.latency, **row.latency})
    return list(by_key.values())


def _jsonb(value: dict[str, Any]) -> BindParameter[Any]:
    return bindparam("latency_patch", value, type_=JSONB)

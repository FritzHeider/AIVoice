"""The engine's own ``pathway_logs`` (specs/11 §12; Decision P07-T08c under specs/11 §12).

:class:`PathwayLogBook` sits beside a :class:`~va_engine.engine.PathwayEngine` and keeps, per engine turn
index, the conversation records and the decision events the engine emitted. :meth:`PathwayLogBook.entries`
builds the §12 entries with the same pure builder the Call Object projection uses
(:func:`va_domain.call_object.build_pathway_logs`), so a test chat, a simulation and a persisted call render
the same bytes; :func:`dump_pathway_logs` is that canonical serialization.

Records are kept by reference: a barge-in rewrites the interrupted record's ``content`` in place, so the
entry shows what the caller heard.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import orjson

from va_domain.call_object import LOG_EVENT_TYPES, LogEvent, LogTurn, PathwayLog, build_pathway_logs
from va_domain.call_object.pathway_logs import event_turn_index
from va_engine.models.state import TurnRecord

__all__ = ["PathwayLogBook", "dump_pathway_logs"]


@dataclass(frozen=True, slots=True)
class _Recorded:
    turn_index: int
    record: TurnRecord
    node_name: str | None
    at: datetime


class PathwayLogBook:
    """Records and decision events of one conversation, by engine turn index."""

    def __init__(self) -> None:
        self._records: list[_Recorded] = []
        self._events: list[LogEvent] = []

    def on_record(self, turn_index: int, record: TurnRecord, *, node_name: str | None, at: datetime) -> None:
        """A user or assistant record of turn ``turn_index`` (other roles are not part of the logs)."""
        if record.role in ("user", "assistant"):
            self._records.append(_Recorded(turn_index, record, node_name, at))

    def on_event(self, event_type: str, data: Mapping[str, Any]) -> None:
        """An emitted event (only the §12 decision events with a ``turn_index`` are kept)."""
        if event_type in LOG_EVENT_TYPES and event_turn_index(data) is not None:
            self._events.append(LogEvent(event_type=event_type, data=dict(data)))

    def drop_turn(self, turn_index: int) -> None:
        """A superseded turn: its records and events go."""
        self._records = [item for item in self._records if item.turn_index != turn_index]
        self._events = [event for event in self._events if event_turn_index(event.data) != turn_index]

    def entries(self, *, pathway_id: uuid.UUID | None, pathway_version: int | None) -> list[PathwayLog]:
        """The §12 entries so far, ordered by turn index."""
        turns = [
            LogTurn(
                turn_index=item.turn_index,
                role=item.record.role,
                text=item.record.content,
                node_id=item.record.node_id,
                node_name=item.node_name,
                created_at=item.at,
            )
            for item in self._records
        ]
        return build_pathway_logs(turns, self._events, pathway_id=pathway_id, pathway_version=pathway_version)


def dump_pathway_logs(entries: Sequence[PathwayLog]) -> bytes:
    """The canonical bytes of ``entries``: JSON in §12 key order, two-space indent, trailing newline."""
    return orjson.dumps(
        [entry.model_dump(mode="json") for entry in entries],
        option=orjson.OPT_INDENT_2 | orjson.OPT_APPEND_NEWLINE,
    )

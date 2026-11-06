"""A :class:`~va_media.turns.TurnRecorder` over recording sinks, for tests of what a call records."""

from __future__ import annotations

import uuid
from typing import Any, Final

from va_common.time import FrozenClock
from va_db.call_turns import TurnOp
from va_domain.call_context import CallContext
from va_events import DomainEvent
from va_media.turns import TurnRecorder

__all__ = ["ORIGIN_MS", "RecordingEvents", "RecordingTurns", "recorder"]

ORIGIN_MS: Final = 50_000.0


class RecordingTurns:
    def __init__(self, *, fail_flush: bool = False) -> None:
        self.ops: list[TurnOp] = []
        self.flushed: list[uuid.UUID] = []
        self.fail_flush = fail_flush

    def add(self, op: TurnOp) -> None:
        self.ops.append(op)

    async def flush_call(self, call_id: uuid.UUID) -> int:
        if self.fail_flush:
            raise ConnectionError
        self.flushed.append(call_id)
        return len(self.ops)


class RecordingEvents:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[DomainEvent] = []
        self.fail = fail

    async def publish(self, event: DomainEvent) -> DomainEvent:
        if self.fail:
            raise ConnectionError
        self.events.append(event)
        return event


def recorder(
    context: CallContext, *, offset_ms: float = 0.0, events: Any = None
) -> tuple[TurnRecorder, RecordingTurns, RecordingEvents]:
    turns = RecordingTurns()
    sink = events if events is not None else RecordingEvents()
    rec = TurnRecorder(
        context,
        turns=turns,
        events=sink,
        clock=FrozenClock(),
        origin_ms=ORIGIN_MS,
        answered_offset_ms=offset_ms,
    )
    return rec, turns, sink
